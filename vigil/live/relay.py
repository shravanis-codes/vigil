"""
Live relay:  Twilio Media Stream  ->  VIGIL  ->  DetectifAI /v1/stream

Why relay? Twilio does not pass messages from the stream back to you. By sitting
in the middle, VIGIL forwards the caller's audio to DetectifAI (API key in a
header, never in a URL) and reads DetectifAI's live messages directly:
ready -> deepfake (early/updates) -> verification -> completed.

Extras:
  * mid-call verification fallback: if no identity result has arrived after
    MIDCALL_VERIFY_SECONDS of caller audio, buffered audio is verified via REST.
  * mock provider: no DetectifAI connection; plays the "genuine" simulator
    script against the real call, so Twilio wiring can be tested without a key.
"""
import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import WebSocket, WebSocketDisconnect

from ..evidence import from_stream_message
from .audio import mulaw_to_wav

log = logging.getLogger("vigil.relay")

MULAW_BYTES_PER_SECOND = 8000


async def handle_twilio_media(ws: WebSocket, pipeline, settings) -> None:
    store = pipeline.store
    await ws.accept()
    session_id: Optional[str] = None
    dws = None                      # DetectifAI websocket
    reader_task: Optional[asyncio.Task] = None
    buffer = bytearray()
    midcall_started = False
    started_at = time.time()

    async def read_detectif():
        nonlocal dws
        try:
            async for raw in dws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                store.add_raw("stream", msg, session_id)
                if msg.get("type") == "ready" and msg.get("stream_id"):
                    store.update_session(session_id, stream_id=msg["stream_id"])
                evs = from_stream_message(session_id, msg, origin="stream")
                if evs:
                    await pipeline.ingest(session_id, evs)
        except Exception as e:  # connection closed etc.
            log.info("DetectifAI stream reader ended: %s", e)

    async def midcall_verify(audio: bytes, speaker_id: str):
        try:
            evs = await pipeline.provider.verify_audio(session_id, mulaw_to_wav(audio), speaker_id,
                                                       idempotency_key=f"vigil-midcall-{session_id}")
            store.add_action(session_id, "midcall_verify", "done",
                             ", ".join(f"{e.source}={e.verdict}" for e in evs))
            await pipeline.ingest(session_id, evs)
        except Exception as e:
            log.warning("mid-call verify failed: %s", e)
            store.add_action(session_id, "midcall_verify", "failed", str(e))

    try:
        while True:
            text = await ws.receive_text()
            msg = json.loads(text)
            event = msg.get("event")

            if event == "start":
                start = msg.get("start", {})
                params = start.get("customParameters") or {}
                session_id = params.get("session_id")
                s = store.get_session(session_id) if session_id else None
                if s is None and start.get("callSid"):
                    s = store.get_session_by_call(start["callSid"])
                    session_id = s["id"] if s else None
                if s is None:
                    log.warning("media stream for unknown session; closing")
                    await ws.close()
                    return
                store.add_raw("twilio", {"event": "start", "callSid": start.get("callSid"),
                                         "streamSid": start.get("streamSid")}, session_id)
                speaker_id = s.get("speaker_id")
                if settings.provider == "detectif":
                    import websockets  # websockets>=14
                    q = {"checks": "deepfake,verify" if speaker_id else "deepfake",
                         "reference": session_id, "encoding": "mulaw", "sample_rate": "8000"}
                    if speaker_id:
                        q["speaker_id"] = speaker_id
                    url = f"{settings.detectif_stream_url}?{urlencode(q)}"
                    dws = await websockets.connect(
                        url, additional_headers={"Authorization": f"Bearer {settings.detectif_api_key}"},
                        max_size=2 ** 22)
                    reader_task = asyncio.create_task(read_detectif())
                    # Forward Twilio's own connected/start framing so DetectifAI sees the call metadata.
                    await dws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
                    await dws.send(text)
                    store.add_action(session_id, "stream_started", "relay", f"checks={q['checks']}")
                else:
                    store.add_action(session_id, "stream_started", "mock",
                                     "Mock provider: playing the 'genuine' script against this call")
                    reader_task = asyncio.create_task(_mock_stream(pipeline, session_id))

            elif event == "media":
                payload = msg.get("media", {}).get("payload", "")
                if dws is not None:
                    await dws.send(text)
                if settings.midcall_verify and session_id and not midcall_started:
                    buffer.extend(base64.b64decode(payload))
                    if len(buffer) >= settings.midcall_verify_seconds * MULAW_BYTES_PER_SECOND:
                        s = store.get_session(session_id)
                        state = pipeline.state(session_id)
                        if s.get("speaker_id") and (state.identity is None
                                                    or state.identity.verdict not in ("match", "no_match")):
                            midcall_started = True
                            asyncio.create_task(midcall_verify(bytes(buffer), s["speaker_id"]))
                        else:
                            midcall_started = True  # identity already known
                        buffer.clear()

            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if dws is not None:
            try:
                await dws.send(json.dumps({"event": "stop"}))
                if reader_task:
                    await asyncio.wait_for(reader_task, timeout=15)  # wait for "completed"
            except Exception:
                pass
            try:
                await dws.close()
            except Exception:
                pass
        elif reader_task and not reader_task.done():
            reader_task.cancel()
        if session_id:
            store.update_meta(session_id, call_ended=True, call_seconds=round(time.time() - started_at, 1))
            store.update_session(session_id, status="closed")


async def _mock_stream(pipeline, session_id: str) -> None:
    """Mock provider: replay the genuine script's DetectifAI messages (no ACTION step; the caller picks it)."""
    from .simulator import SCENARIOS
    start = time.time()
    for at, msg in SCENARIOS["genuine"]["script"]:
        if msg == "ACTION":
            continue
        await asyncio.sleep(max(0.0, at * 2 - (time.time() - start)))  # slower: real callers take time
        m = dict(msg, stream_id=f"mock_{session_id}")
        pipeline.store.add_raw("stream", m, session_id, note="mock")
        await pipeline.ingest(session_id, from_stream_message(session_id, m, origin="stream"))
