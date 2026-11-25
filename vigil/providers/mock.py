"""
Mock provider: works with no API key, so the whole product can be demoed offline.

It returns payloads in DetectifAI's real response shapes, which then go through the
SAME normalizers as real responses. Outcome is chosen from the file name:

  file name contains   ->  result
  fake / synthetic / clone / tts / deepfake   -> Aletheia: fake (segments 3-8 s flagged)
  impostor / other / wrong                     -> Eidos: no_match
  short                                        -> too little speech (inconclusive)
  anything else                                -> real voice, match
Speaker IDs that were never enrolled return 404 (not enrolled), like the real API.
"""
import asyncio
import re
import uuid
from typing import Any, Dict, List, Optional

from ..evidence import from_api_error, from_detect_response, from_verify_response
from ..models import ALETHEIA, EIDOS, Evidence
from .base import VoiceProvider

FAKE_WORDS = {"fake", "synthetic", "clone", "cloned", "tts", "deepfake", "ai"}
IMPOSTOR_WORDS = {"impostor", "imposter", "other", "wrong"}


def _words(name: str) -> set:
    return set(re.split(r"[^a-z0-9]+", name.lower()))


def mock_deepfake_result(fake: bool, seconds: float = 12.0) -> Dict[str, Any]:
    segs = []
    t = 0.0
    while t + 4 <= seconds:
        flagged = fake and 3 <= t <= 8
        score = 0.91 if flagged else (0.38 if fake else 0.06)
        segs.append({"start": t, "end": t + 4, "score": score, "verdict": "fake" if score >= 0.5 else "real"})
        t += 2
    return {
        "verdict": "fake" if fake else "real",
        "label": "synthetic" if fake else "genuine",
        "is_synthetic": fake,
        "score": 0.93 if fake else 0.04,
        "score_semantics": "Higher means more likely AI-generated.",
        "duration_seconds": seconds,
        "scored_seconds": seconds,
        "segments": segs,
    }


class MockProvider(VoiceProvider):
    name = "mock"

    def __init__(self, store):
        self.store = store

    async def analyze_recording(self, session_id: str, audio_path: str, speaker_id: Optional[str],
                                idempotency_key: str) -> List[Evidence]:
        await asyncio.sleep(0.6)  # feel like a network call
        w = _words(audio_path.rsplit("/", 1)[-1])
        fake = bool(w & FAKE_WORDS)
        impostor = bool(w & IMPOSTOR_WORDS)
        op = "op_mock_" + uuid.uuid4().hex[:16]
        if "short" in w:
            err = {"error": {"type": "invalid_request_error", "code": "insufficient_speech",
                             "message": "Less than 2 seconds of speech detected."}}
            src = EIDOS if speaker_id else ALETHEIA
            evs = from_api_error(session_id, src, 422, err)
            if speaker_id:
                evs += from_api_error(session_id, ALETHEIA, 422, err)
            return evs
        df_body = {"request_id": "req_mock", "operation_id": op + "d", "object": "deepfake.detection",
                   "result": mock_deepfake_result(fake), "model": {"name": "aletheia", "version": "mock"}}
        if not speaker_id:
            return from_detect_response(session_id, df_body)
        if not self.store.get_speaker(speaker_id):
            body = {"error": {"type": "not_found_error", "code": "speaker_not_found",
                              "message": f"No speaker with id '{speaker_id}'."}}
            return from_api_error(session_id, EIDOS, 404, body) + from_detect_response(session_id, df_body)
        # A good clone lands close to the real voiceprint, so it still "matches".
        similarity = 0.12 if impostor else (0.71 if fake else 0.78)
        body = {
            "request_id": "req_mock", "operation_id": op, "object": "speaker.verification",
            "speaker_id": speaker_id,
            "result": {"decision": "no_match" if impostor else "match", "similarity": similarity,
                       "duration_seconds": 12.0},
            "deepfake": dict(mock_deepfake_result(fake), operation_id=op + "d"),
        }
        return from_verify_response(session_id, body)

    async def verify_audio(self, session_id: str, wav_bytes: bytes, speaker_id: str,
                           idempotency_key: str) -> List[Evidence]:
        body = {"operation_id": "op_mock_" + uuid.uuid4().hex[:12],
                "result": {"decision": "match", "similarity": 0.77, "duration_seconds": 10.0}}
        return [e for e in from_verify_response(session_id, body, origin="midcall_rest") if e.source == EIDOS]

    async def enroll(self, speaker_id: str, audio_path: str, display_name: Optional[str],
                     idempotency_key: str) -> Dict[str, Any]:
        existing = self.store.get_speaker(speaker_id)
        samples = (existing or {}).get("samples", 0) + 1
        return {"object": "speaker.enrollment", "operation_id": "op_mock_enroll",
                "speaker": {"speaker_id": speaker_id, "display_name": display_name, "samples": samples},
                "quality": {"speech_seconds": 17.9, "duration_seconds": 19.2}}

    async def list_speakers(self) -> List[Dict[str, Any]]:
        return [{"speaker_id": s["speaker_id"], "display_name": s["display_name"], "samples": s["samples"]}
                for s in self.store.list_speakers()]

    async def create_stream_token(self) -> str:
        return "stk_mock"

    async def health(self) -> Dict[str, Any]:
        return {"reachable": True, "auth_ok": True, "mock": True}
