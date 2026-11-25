"""
Live-call simulator. Emits messages in DetectifAI's real stream format
(ready / deepfake / verification / completed) and pushes them through the SAME
normalizer and pipeline as a real call. Lets you demo Mode B without a phone.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..evidence import from_stream_message

log = logging.getLogger("vigil.simulator")

# (seconds after start, message or ("ACTION", None))
Script = List[Tuple[float, Dict[str, Any]]]


def _df(seq: int, verdict: str, score: float, speech: float, early: bool = False) -> Dict[str, Any]:
    return {"type": "deepfake", "early": early, "final": False, "sequence": seq, "seconds": speech + 1,
            "speech_seconds": speech, "window_seconds": 10, "verdict": verdict, "score": score}


def _completed(verdict: str, score: float, speech: float, decision: Optional[str], segments=None,
               ver_error: Optional[dict] = None) -> Dict[str, Any]:
    results: Dict[str, Any] = {"deepfake": {"result": {"verdict": verdict, "score": score,
                                                       "duration_seconds": speech + 2,
                                                       "speech_seconds": speech,
                                                       "segments": segments or []}}}
    if ver_error:
        results["verification"] = {"error": ver_error}
    elif decision:
        results["verification"] = {"operation_id": "op_sim_v",
                                   "result": {"decision": decision, "similarity": 0.76 if decision == "match" else 0.11}}
    return {"type": "completed", "status": "completed", "seconds": speech + 2, "speech_seconds": speech,
            "ended_by": "client_stop", "results": results}


def _ver(decision: str) -> Dict[str, Any]:
    return {"type": "verification", "final": True, "early": False, "operation_id": "op_sim_v",
            "object": "speaker.verification",
            "result": {"decision": decision, "similarity": 0.76 if decision == "match" else 0.11,
                       "duration_seconds": 8.0}}


def _segs(fake_from: float, fake_to: float, total: float):
    out, t = [], 0.0
    while t + 4 <= total:
        bad = fake_from <= t <= fake_to
        out.append({"start": t, "end": t + 4, "score": 0.9 if bad else 0.2, "verdict": "fake" if bad else "real"})
        t += 2
    return out


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "genuine": {
        "label": "Genuine customer",
        "script": [
            (0.0, {"type": "ready", "checks": ["deepfake", "verify"]}),
            (1.5, _df(0, "real", 0.08, 3.2, early=True)),
            (2.5, "ACTION"),
            (4.0, _df(1, "real", 0.06, 8.5)),
            (5.0, _ver("match")),
            (7.0, _df(2, "real", 0.05, 13.0)),
            (9.0, _completed("real", 0.05, 15.0, "match")),
        ],
    },
    "clone": {
        "label": "Voice clone of the customer",
        "script": [
            (0.0, {"type": "ready", "checks": ["deepfake", "verify"]}),
            (1.5, _df(0, "real", 0.41, 3.0, early=True)),
            (2.5, "ACTION"),
            (4.0, _ver("match")),
            (5.0, _df(1, "fake", 0.88, 8.0)),
            (7.0, _df(2, "fake", 0.93, 12.5)),
            (9.0, _completed("fake", 0.92, 14.0, "match", _segs(4, 10, 14))),
        ],
    },
    "impostor": {
        "label": "Real person, wrong identity",
        "script": [
            (0.0, {"type": "ready", "checks": ["deepfake", "verify"]}),
            (1.5, _df(0, "real", 0.07, 3.1, early=True)),
            (2.5, "ACTION"),
            (4.5, _df(1, "real", 0.05, 9.0)),
            (5.5, _ver("no_match")),
            (8.0, _completed("real", 0.05, 13.0, "no_match")),
        ],
    },
    "short_call": {
        "label": "Caller barely speaks",
        "script": [
            (0.0, {"type": "ready", "checks": ["deepfake", "verify"]}),
            (1.5, _df(0, "real", 0.12, 2.1, early=True)),
            (2.5, "ACTION"),
            (6.0, _completed("real", 0.15, 3.4, None,
                             ver_error={"code": "insufficient_speech", "message": "Not enough speech to verify."})),
        ],
    },
}


async def run_simulation(pipeline, session_id: str, scenario: str, action_type: str,
                         amount: Optional[float], speed: float = 1.0) -> None:
    sc = SCENARIOS[scenario]
    stream_id = f"sim_{session_id}"
    store = pipeline.store
    start = asyncio.get_event_loop().time()
    try:
        for at, msg in sc["script"]:
            delay = at / max(speed, 0.1) - (asyncio.get_event_loop().time() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            if msg == "ACTION":
                store.add_action(session_id, "caller_request", "received", f"{action_type} (IVR selection)")
                await pipeline.set_action(session_id, action_type, amount)
                continue
            msg = dict(msg, stream_id=stream_id)
            store.add_raw("simulator", msg, session_id)
            await pipeline.ingest(session_id, from_stream_message(session_id, msg, stream_id, origin="simulator"))
        store.update_session(session_id, status="closed")
    except Exception:  # pragma: no cover
        log.exception("simulation failed")
