"""
Evidence & Event Processor.

  normalize  -> turn any DetectifAI payload (REST, stream message, webhook) into Evidence
  validate   -> make sure verdicts are ones the policy understands
  deduplicate-> via dedupe_key (stored with a UNIQUE constraint)
  finality   -> build_state() prefers final results over early/live updates

Field names follow https://platform.detectif.ai/docs/ . Parsers are deliberately
tolerant (they check several places for each field) because webhook payloads
are less precisely documented than REST responses.
"""
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ALETHEIA, EIDOS, ERROR, FAKE, INCONCLUSIVE, MATCH, NO_MATCH, NOT_ENROLLED, REAL, REVIEW,
    STEP_UP, SYSTEM, VALID_VERDICTS, Evidence, EvidenceState, Segment,
)

INCONCLUSIVE_CODES = {"insufficient_speech", "audio_too_short", "audio_too_long", "too_quiet", "no_speech"}


# ---------------------------------------------------------------- helpers
def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _segments(raw: Any) -> List[Segment]:
    out = []
    for s in raw or []:
        try:
            out.append(Segment(start=float(s.get("start", 0)), end=float(s.get("end", 0)),
                               score=_f(s.get("score")), verdict=s.get("verdict")))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _deepfake_verdict(result: Dict[str, Any]) -> Optional[str]:
    """verdict is the documented field; label/is_synthetic are older wordings."""
    v = result.get("verdict")
    if v in (REAL, FAKE):
        return v
    label = result.get("label")
    if label == "genuine":
        return REAL
    if label == "synthetic":
        return FAKE
    if result.get("is_synthetic") is True:
        return FAKE
    if result.get("is_synthetic") is False:
        return REAL
    return None


def deepfake_evidence(session_id: str, result: Dict[str, Any], *, origin: str, final: bool = True,
                      early: bool = False, provider_ref: Optional[str] = None,
                      dedupe_key: Optional[str] = None, sequence: Optional[int] = None,
                      raw: Optional[dict] = None) -> Evidence:
    if result.get("error"):
        return error_evidence(session_id, ALETHEIA, result["error"], origin=origin,
                              provider_ref=provider_ref, dedupe_key=dedupe_key, raw=raw or result)
    verdict = _deepfake_verdict(result) or INCONCLUSIVE
    speech = _f(result.get("speech_seconds"))
    if speech is None:
        speech = _f(result.get("scored_seconds"))
    return Evidence(
        session_id=session_id, source=ALETHEIA, verdict=verdict, final=final, early=early,
        score=_f(result.get("score")), score_type="synthetic_probability",
        speech_seconds=speech, duration_seconds=_f(result.get("duration_seconds") or result.get("seconds")),
        sequence=sequence, segments=_segments(result.get("segments")),
        provider_ref=provider_ref or result.get("operation_id"), origin=origin,
        dedupe_key=dedupe_key, raw=raw or result,
        message=None if verdict != INCONCLUSIVE else "Provider returned no usable verdict",
    )


def verification_evidence(session_id: str, result: Dict[str, Any], *, origin: str, final: bool = True,
                          early: bool = False, provider_ref: Optional[str] = None,
                          dedupe_key: Optional[str] = None, raw: Optional[dict] = None) -> Evidence:
    decision = result.get("decision")
    verdict = decision if decision in (MATCH, NO_MATCH) else INCONCLUSIVE
    return Evidence(
        session_id=session_id, source=EIDOS, verdict=verdict, final=final, early=early,
        score=_f(result.get("similarity")), score_type="similarity",
        duration_seconds=_f(result.get("duration_seconds")), provider_ref=provider_ref,
        origin=origin, dedupe_key=dedupe_key, raw=raw or result,
    )


def error_evidence(session_id: str, source: str, error: Any, *, origin: str, status: Optional[int] = None,
                   provider_ref: Optional[str] = None, dedupe_key: Optional[str] = None,
                   raw: Optional[dict] = None) -> Evidence:
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        message = error.get("message")
    else:
        code, message = None, str(error)
    if source == EIDOS and status == 404:
        verdict = NOT_ENROLLED
        message = message or "Speaker ID not found (not enrolled)"
    elif (code in INCONCLUSIVE_CODES) or status == 422:
        verdict = INCONCLUSIVE
    else:
        verdict = ERROR
    return Evidence(session_id=session_id, source=source, verdict=verdict, final=True, error_code=code,
                    message=message or (f"HTTP {status}" if status else "error"), provider_ref=provider_ref,
                    origin=origin, dedupe_key=dedupe_key, raw=raw or {"error": error, "status": status})


# ---------------------------------------------------------------- REST responses
def from_detect_response(session_id: str, body: Dict[str, Any], origin: str = "recorded") -> List[Evidence]:
    """POST /v1/deepfake/detect -> {operation_id, result: {verdict, score, segments...}}"""
    op = body.get("operation_id")
    return [deepfake_evidence(session_id, body.get("result") or {}, origin=origin, provider_ref=op,
                              dedupe_key=f"{op}:aletheia" if op else None, raw=body)]


def from_verify_response(session_id: str, body: Dict[str, Any], origin: str = "recorded") -> List[Evidence]:
    """POST /v1/speakers/verify (detect_deepfake=true) -> result + deepfake sub-object."""
    op = body.get("operation_id")
    out = [verification_evidence(session_id, body.get("result") or {}, origin=origin, provider_ref=op,
                                 dedupe_key=f"{op}:eidos" if op else None, raw=body)]
    df = body.get("deepfake")
    if isinstance(df, dict):
        df_op = df.get("operation_id") or op
        out.append(deepfake_evidence(session_id, df, origin=origin, provider_ref=df_op,
                                     dedupe_key=f"{df_op}:aletheia" if df_op else None, raw=df))
    return out


def from_api_error(session_id: str, source: str, status: int, body: Dict[str, Any],
                   origin: str = "recorded") -> List[Evidence]:
    """An HTTP error. A failed verification may still carry the deepfake result in `partial`."""
    out = [error_evidence(session_id, source, (body or {}).get("error") or body, origin=origin,
                          status=status, raw=body)]
    partial = (body or {}).get("partial")
    if isinstance(partial, dict):
        df = partial.get("deepfake") or partial.get("result") or partial
        if isinstance(df, dict) and _deepfake_verdict(df):
            out.append(deepfake_evidence(session_id, df, origin=origin, raw=partial))
    return out


# ---------------------------------------------------------------- live stream messages
def from_stream_message(session_id: str, msg: Dict[str, Any], stream_id: Optional[str] = None,
                        origin: str = "stream") -> List[Evidence]:
    """Server messages from wss://platform.detectif.ai/v1/stream:
       ready | deepfake | verification | enrollment | completed | error"""
    t = msg.get("type")
    sid = msg.get("stream_id") or stream_id or "stream"
    if t == "ready":
        return [Evidence(session_id=session_id, source=SYSTEM, verdict="info", message="stream.ready",
                         provider_ref=sid, origin=origin, dedupe_key=f"{sid}:ready", raw=msg)]
    if t == "deepfake":
        early = bool(msg.get("early"))
        final = bool(msg.get("final"))
        seq = msg.get("sequence")
        if early:
            tag = "early"
        elif final:
            tag = "final"
        else:
            tag = f"seq{seq}" if seq is not None else f"t{msg.get('seconds')}"
        return [deepfake_evidence(session_id, msg, origin=origin, final=final, early=early, provider_ref=sid,
                                  sequence=seq if isinstance(seq, int) else None,
                                  dedupe_key=f"{sid}:deepfake:{tag}", raw=msg)]
    if t == "verification":
        final = msg.get("final", True) is not False
        tag = "final" if final else "early"
        return [verification_evidence(session_id, msg.get("result") or {}, origin=origin, final=final,
                                      early=bool(msg.get("early")),
                                      provider_ref=msg.get("operation_id") or sid,
                                      dedupe_key=f"{sid}:verification:{tag}", raw=msg)]
    if t == "completed":
        out = []
        results = msg.get("results") or {}
        df = results.get("deepfake")
        if isinstance(df, dict):
            res = df.get("result") if isinstance(df.get("result"), dict) else df
            if df.get("error"):
                res = {"error": df["error"]}
            if res.get("speech_seconds") is None and msg.get("speech_seconds") is not None:
                res = dict(res, speech_seconds=msg.get("speech_seconds"))
            out.append(deepfake_evidence(session_id, res, origin=origin, final=True, provider_ref=sid,
                                         dedupe_key=f"{sid}:deepfake:final", raw=df))
        ver = results.get("verification")
        if isinstance(ver, dict):
            vres = ver.get("result") if isinstance(ver.get("result"), dict) else ver
            if ver.get("error"):
                out.append(error_evidence(session_id, EIDOS, ver["error"], origin=origin,
                                          dedupe_key=f"{sid}:verification:final", raw=ver))
            else:
                out.append(verification_evidence(session_id, vres, origin=origin, final=True,
                                                 provider_ref=ver.get("operation_id") or sid,
                                                 dedupe_key=f"{sid}:verification:final", raw=ver))
        out.append(Evidence(session_id=session_id, source=SYSTEM, verdict="info", message="stream.completed",
                            provider_ref=sid, origin=origin, dedupe_key=f"{sid}:completed",
                            speech_seconds=_f(msg.get("speech_seconds")), raw=msg))
        return out
    if t == "error":
        return [Evidence(session_id=session_id, source=SYSTEM, verdict=ERROR, error_code=msg.get("code"),
                         message=msg.get("message"), provider_ref=sid, origin=origin,
                         dedupe_key=f"{sid}:error:{msg.get('code')}", raw=msg)]
    return []  # enrollment and unknown types carry no decision evidence


# ---------------------------------------------------------------- webhooks
WEBHOOK_TYPE_MAP = {
    "stream.deepfake.updated": "deepfake",
    "stream.completed": "completed",
    "stream.started": "ready",
    "speaker.verified": "verification",
}


def webhook_reference(envelope: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Find (reference, stream_id) in a webhook. We set reference = our session id."""
    data = envelope.get("data") or {}
    candidates = [data, data.get("result") or {}, data.get("stream") or {}]
    ref = next((c.get("reference") for c in candidates if isinstance(c, dict) and c.get("reference")), None)
    sid = next((c.get("stream_id") for c in candidates if isinstance(c, dict) and c.get("stream_id")), None)
    return ref, sid


def from_webhook(session_id: str, envelope: Dict[str, Any]) -> List[Evidence]:
    etype = envelope.get("type", "")
    wid = envelope.get("id") or ""
    data = envelope.get("data") or {}
    if etype == "deepfake.completed":
        evs = from_detect_response(session_id, data, origin="webhook")
    elif etype == "speaker.verified" and "result" in data and "type" not in data:
        evs = from_verify_response(session_id, data, origin="webhook")
    elif etype in WEBHOOK_TYPE_MAP:
        msg = dict(data)
        inner = data.get("result")
        # Some webhook bodies wrap the stream message under data.result.
        if isinstance(inner, dict) and ("type" in inner or "verdict" in inner or "results" in inner):
            msg = dict(inner, **{k: v for k, v in data.items() if k != "result"})
        msg["type"] = WEBHOOK_TYPE_MAP[etype]
        evs = from_stream_message(session_id, msg, stream_id=data.get("stream_id"), origin="webhook")
    else:
        return []
    for e in evs:  # stream-scoped keys already dedupe across relay/webhook; else use webhook id
        if not e.dedupe_key:
            e.dedupe_key = f"wh:{wid}:{e.source}"
    return evs


# ---------------------------------------------------------------- validation & state
def validate(ev: Evidence) -> Evidence:
    allowed = VALID_VERDICTS.get(ev.source)
    if allowed is None:
        raise ValueError(f"unknown evidence source {ev.source}")
    if ev.verdict not in allowed:
        ev.message = f"Unexpected verdict '{ev.verdict}' coerced to inconclusive. " + (ev.message or "")
        ev.verdict = INCONCLUSIVE if INCONCLUSIVE in allowed else ERROR
    return ev


def _best(items: List[Evidence]) -> Optional[Evidence]:
    """Finality tracking: final beats non-final; a real result beats an error; latest wins ties."""
    if not items:
        return None
    def rank(e: Evidence) -> Tuple[int, int]:
        return (1 if e.final else 0, 0 if e.verdict == ERROR else 1)
    best_rank = max(rank(e) for e in items)
    return [e for e in items if rank(e) == best_rank][-1]


def build_state(evidence: List[Evidence]) -> EvidenceState:
    by: Dict[str, List[Evidence]] = {}
    for e in evidence:
        by.setdefault(e.source, []).append(e)
    sysev = by.get(SYSTEM, [])
    return EvidenceState(
        authenticity=_best(by.get(ALETHEIA, [])),
        identity=_best(by.get(EIDOS, [])),
        step_up=(by.get(STEP_UP) or [None])[-1],
        review=(by.get(REVIEW) or [None])[-1],
        errors=[e for e in evidence if e.verdict == ERROR],
        stream_completed=any(e.message == "stream.completed" for e in sysev),
    )
