"""
DetectifAI adapter (https://platform.detectif.ai/docs/).

Recorded audio:
  POST /v1/speakers/verify  (file, speaker_id, detect_deepfake=true) -> Eidos + Aletheia in one call
  POST /v1/deepfake/detect  (file)                                     -> Aletheia only
Enrollment:  POST /v1/speakers/enroll (file, speaker_id, display_name)  15-60 s, >=10 s speech
Live:        POST /v1/stream/tokens -> short-lived token for telephony platforms

Retries: 429 / 5xx are retried with the SAME Idempotency-Key, so a retry is never charged twice.
"""
import asyncio
import mimetypes
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..evidence import from_api_error, from_detect_response, from_verify_response
from ..models import ALETHEIA, EIDOS, Evidence
from .base import VoiceProvider

RETRY_STATUSES = {429, 500, 502, 503, 504}


class DetectifError(Exception):
    def __init__(self, status: int, body: Dict[str, Any]):
        self.status, self.body = status, body
        err = (body or {}).get("error") or {}
        super().__init__(f"DetectifAI HTTP {status}: {err.get('code')} {err.get('message')}")


class DetectifProvider(VoiceProvider):
    name = "detectif"

    def __init__(self, api_key: str, base_url: str = "https://platform.detectif.ai", timeout: float = 130.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None, retry_sleep: float = 1.0):
        if not api_key:
            raise ValueError("DETECTIF_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}  # never put the key in a URL
        self._timeout = timeout
        self._transport = transport          # injectable for tests
        self._sleep = retry_sleep

    # ------------------------------------------------------------ http
    async def _request(self, method: str, path: str, *, files=None, data=None,
                       idempotency_key: Optional[str] = None, retries: int = 2) -> Tuple[int, Dict[str, Any]]:
        headers = dict(self._headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:128]
        attempt = 0
        while True:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout,
                                         transport=self._transport) as client:
                try:
                    resp = await client.request(method, path, headers=headers, files=files, data=data)
                except httpx.HTTPError as e:
                    if attempt < retries:
                        attempt += 1
                        await asyncio.sleep(1.5 * attempt * self._sleep)
                        continue
                    return 599, {"error": {"code": "network_error", "message": str(e)}}
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"code": "bad_response", "message": resp.text[:300]}}
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                attempt += 1
                wait = resp.headers.get("Retry-After")
                await asyncio.sleep((min(float(wait), 5.0) if wait and wait.isdigit() else 1.5 * attempt) * self._sleep)
                continue
            # 409 operation_in_progress: same key still running -> wait and replay
            if resp.status_code == 409 and (body.get("error") or {}).get("code") == "operation_in_progress" \
                    and attempt < retries + 3:
                attempt += 1
                await asyncio.sleep(2.0 * self._sleep)
                continue
            return resp.status_code, body

    @staticmethod
    def _file_tuple(path: str, content: Optional[bytes] = None, name: Optional[str] = None):
        name = name or os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content is None:  # read into memory so a retry re-sends the whole file
            with open(path, "rb") as fh:
                content = fh.read()
        return ("file", (name, content, mime))

    # ------------------------------------------------------------ recorded
    async def analyze_recording(self, session_id: str, audio_path: str, speaker_id: Optional[str],
                                idempotency_key: str) -> List[Evidence]:
        if speaker_id:
            status, body = await self._request(
                "POST", "/v1/speakers/verify",
                files=[self._file_tuple(audio_path)],
                data={"speaker_id": speaker_id, "detect_deepfake": "true"},
                idempotency_key=f"{idempotency_key}-v")
            if status == 200:
                evs = from_verify_response(session_id, body)
                if any(e.source == ALETHEIA for e in evs):
                    return evs
                # deepfake sub-result missing -> run it separately
                return evs + await self._detect(session_id, audio_path, idempotency_key)
            evs = from_api_error(session_id, EIDOS, status, body)
            if any(e.source == ALETHEIA for e in evs):  # `partial` carried the deepfake result
                return evs
            # Verification failed (e.g. not enrolled, or clip > 60 s): still run Aletheia.
            return evs + await self._detect(session_id, audio_path, idempotency_key)
        return await self._detect(session_id, audio_path, idempotency_key)

    async def _detect(self, session_id: str, audio_path: str, idempotency_key: str) -> List[Evidence]:
        status, body = await self._request("POST", "/v1/deepfake/detect", files=[self._file_tuple(audio_path)],
                                           idempotency_key=f"{idempotency_key}-d")
        if status == 200:
            return from_detect_response(session_id, body)
        return from_api_error(session_id, ALETHEIA, status, body)

    async def verify_audio(self, session_id: str, wav_bytes: bytes, speaker_id: str,
                           idempotency_key: str) -> List[Evidence]:
        status, body = await self._request(
            "POST", "/v1/speakers/verify",
            files=[self._file_tuple("midcall.wav", content=wav_bytes, name="midcall.wav")],
            data={"speaker_id": speaker_id, "detect_deepfake": "false"},
            idempotency_key=idempotency_key)
        if status == 200:
            evs = [e for e in from_verify_response(session_id, body, origin="midcall_rest") if e.source == EIDOS]
            return evs
        return [e for e in from_api_error(session_id, EIDOS, status, body, origin="midcall_rest")
                if e.source == EIDOS]

    # ------------------------------------------------------------ speakers
    async def enroll(self, speaker_id: str, audio_path: str, display_name: Optional[str],
                     idempotency_key: str) -> Dict[str, Any]:
        data = {"speaker_id": speaker_id}
        if display_name:
            data["display_name"] = display_name[:128]
        status, body = await self._request("POST", "/v1/speakers/enroll", files=[self._file_tuple(audio_path)],
                                           data=data, idempotency_key=idempotency_key)
        if status != 200:
            raise DetectifError(status, body)
        return body

    async def list_speakers(self) -> List[Dict[str, Any]]:
        status, body = await self._request("GET", "/v1/speakers?limit=100", retries=1)
        if status != 200:
            raise DetectifError(status, body)
        return body.get("data", [])

    # ------------------------------------------------------------ live
    async def create_stream_token(self) -> str:
        status, body = await self._request("POST", "/v1/stream/tokens", retries=1)
        if status not in (200, 201):
            raise DetectifError(status, body)
        return body["token"]

    async def health(self) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as c:
                r = await c.get("/v1/health")
            health = {"reachable": r.status_code == 200, "status": r.status_code}
        except httpx.HTTPError as e:
            return {"reachable": False, "error": str(e)}
        status, body = await self._request("GET", "/v1/credits", retries=0)
        health["auth_ok"] = status == 200
        if status == 200:
            health["credits"] = body
        else:
            health["auth_error"] = (body.get("error") or {}).get("code")
        return health
