"""
VIGIL backend (FastAPI).

Run:  uvicorn vigil.api:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs
"""
import asyncio
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .evidence import from_webhook, webhook_reference
from .live.audio import wav_duration
from .live.simulator import SCENARIOS, run_simulation
from .live.twilio_routes import build_router
from .models import DECISIONS
from .pipeline import Pipeline
from .policy import PolicyConfig, compute_risk
from .providers import make_provider
from .settings import settings
from .store import Store
from .webhooks import WebhookVerificationError, verify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vigil")

ALLOWED_EXT = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".webm", ".m4a", ".aac", ".aiff", ".aif"}
MAX_UPLOAD = 100 * 1024 * 1024
SPEAKER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

store = Store(settings.db_path)
policy_cfg = PolicyConfig.load(settings.policy_file)
provider = make_provider(settings, store)
pipeline = Pipeline(store, policy_cfg, provider, settings)



@asynccontextmanager
async def lifespan(_app):
    # Mock mode: make sure a demo speaker exists so the demo works out of the box.
    if settings.provider == "mock" and not store.get_speaker("demo-customer"):
        store.upsert_speaker("demo-customer", "Demo Customer", None, 1, source="mock")
    log.info("VIGIL ready: provider=%s live_mode=%s twilio=%s", provider.name, settings.live_mode,
             settings.twilio_enabled)
    yield


app = FastAPI(title="VIGIL", version="1.0", lifespan=lifespan,
              description="Voice identity + authenticity decisioning on DetectifAI (Aletheia + Eidos)")
app.include_router(build_router(pipeline, settings))
_background: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ------------------------------------------------------------------ views
def session_view(session_id: str) -> Dict[str, Any]:
    s = store.get_session(session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    events = store.list_events(session_id)
    state = pipeline.state(session_id)
    current = pipeline.evaluate(session_id)
    action = policy_cfg.actions.get(s.get("action_type") or "", {})
    return {
        "session": s,
        "action_label": action.get("label"),
        "state": state.model_dump(exclude={"authenticity": {"raw"}, "identity": {"raw"},
                                           "errors": {"__all__": {"raw"}}}),
        "decision": current.model_dump(),
        "decisions": store.list_decisions(session_id),
        "events": [dict(e["evidence"], stored_at=e["created_at"]) for e in events],
        "actions": store.list_actions(session_id),
    }


# ------------------------------------------------------------------ system
@app.get("/health")
async def health():
    return {"ok": True, "config": settings.public_summary(), "provider": await provider.health()}


@app.get("/api/config")
async def config():
    return {"actions": policy_cfg.actions, "domains": policy_cfg.domains, "risk_tiers": policy_cfg.tiers,
            "decisions": DECISIONS, "simulator_scenarios": {k: v["label"] for k, v in SCENARIOS.items()},
            "settings": settings.public_summary()}


@app.get("/api/risk")
async def risk(action_type: str, amount: Optional[float] = None):
    from .models import ActionContext
    tier, trace = compute_risk(ActionContext(action_type=action_type, amount=amount), policy_cfg)
    return {"risk_tier": tier, "trace": trace}


# ------------------------------------------------------------------ speakers
@app.get("/api/speakers")
async def list_speakers():
    local = {s["speaker_id"]: s for s in store.list_speakers()}
    remote_error = None
    if settings.provider == "detectif":
        try:
            for r in await provider.list_speakers():
                sp = local.setdefault(r["speaker_id"], {"speaker_id": r["speaker_id"], "phone": None,
                                                        "source": "detectif"})
                sp["display_name"] = sp.get("display_name") or r.get("display_name")
                sp["samples"] = r.get("samples")
        except Exception as e:  # show local list even if the API is unreachable
            remote_error = str(e)
    return {"speakers": list(local.values()), "remote_error": remote_error}


@app.post("/api/speakers/enroll")
async def enroll(file: UploadFile = File(...), speaker_id: str = Form(...),
                 display_name: Optional[str] = Form(None), phone: Optional[str] = Form(None)):
    if not SPEAKER_RE.match(speaker_id):
        raise HTTPException(400, "speaker_id: 1-64 characters, letters, digits, _ . : -")
    path, digest, warnings = await _save_upload(file)
    try:
        dur = wav_duration(str(path))
        if not 15 <= dur <= 60:
            warnings.append(f"Enrollment audio is {dur:.1f}s; DetectifAI needs 15-60 s with 10+ s of speech.")
    except Exception:
        pass
    try:
        body = await provider.enroll(speaker_id, str(path), display_name, idempotency_key=f"vigil-enr-{digest[:40]}")
    except Exception as e:
        raise HTTPException(502, f"Enrollment failed: {e}")
    samples = (body.get("speaker") or {}).get("samples")
    store.upsert_speaker(speaker_id, display_name, phone or None, samples, source=provider.name)
    return {"enrollment": body, "warnings": warnings}


class SpeakerLink(BaseModel):
    speaker_id: str
    display_name: Optional[str] = None
    phone: Optional[str] = None


@app.post("/api/speakers/link")
async def link_speaker(body: SpeakerLink):
    """Register an already-enrolled speaker locally (e.g. link a phone number for live calls)."""
    if not SPEAKER_RE.match(body.speaker_id):
        raise HTTPException(400, "invalid speaker_id")
    store.upsert_speaker(body.speaker_id, body.display_name, body.phone, None, source="link")
    return {"ok": True}


# ------------------------------------------------------------------ Mode A: recorded
async def _save_upload(file: UploadFile):
    ext = Path(file.filename or "audio").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported file type {ext}. Use one of: {', '.join(sorted(ALLOWED_EXT))}")
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File larger than 100 MB")
    if not data:
        raise HTTPException(400, "Empty file")
    digest = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(file.filename).stem)[:60]
    path = settings.upload_dir / f"{digest[:12]}_{safe}{ext}"
    path.write_bytes(data)
    return path, digest, []


def _preflight(path: Path, speaker_id: Optional[str]) -> list:
    warnings = []
    try:
        dur = wav_duration(str(path))
    except Exception:
        return warnings  # only WAV durations are checked locally
    if dur > 300:
        warnings.append(f"Clip is {dur:.0f}s; deepfake detection accepts up to 300 s.")
    if speaker_id and dur > 60:
        warnings.append(f"Clip is {dur:.0f}s; speaker verification accepts 3-60 s. Deepfake check will still run.")
    if dur < 3:
        warnings.append(f"Clip is only {dur:.1f}s; results may be inconclusive (need 2+ s of speech).")
    elif dur < 10:
        warnings.append(f"Clip is {dur:.1f}s; 10+ s of speech gives the most reliable verdicts.")
    return warnings


async def _run_recorded(session_id: str) -> None:
    s = store.get_session(session_id)
    meta = s.get("meta") or {}
    evs = await provider.analyze_recording(session_id, s["audio_path"], s.get("speaker_id"),
                                           idempotency_key=meta["idempotency_key"])
    await pipeline.ingest(session_id, evs)
    store.update_session(session_id, status="closed")


@app.post("/api/recorded/analyze")
async def analyze_recorded(file: UploadFile = File(...), action_type: str = Form(...),
                           speaker_id: Optional[str] = Form(None), amount: Optional[float] = Form(None),
                           currency: str = Form("INR"), details: Optional[str] = Form(None)):
    if speaker_id and not SPEAKER_RE.match(speaker_id):
        raise HTTPException(400, "speaker_id: 1-64 characters, letters, digits, _ . : -")
    if action_type not in policy_cfg.actions:
        raise HTTPException(400, f"Unknown action_type. Options: {', '.join(policy_cfg.actions)}")
    try:
        details_obj = json.loads(details) if details else {}
    except ValueError:
        details_obj = {"note": details}
    path, digest, warnings = await _save_upload(file)
    warnings += _preflight(path, speaker_id)
    # Same file + same speaker -> same Idempotency-Key -> DetectifAI never charges twice (24 h).
    idem = f"vigil-{digest[:40]}-{speaker_id or 'nospk'}"
    s = store.create_session("recorded", speaker_id=speaker_id or None, action_type=action_type, amount=amount,
                             currency=currency, details=dict(details_obj, filename=file.filename),
                             audio_path=str(path),
                             meta={"idempotency_key": idem, "warnings": warnings, "provider": provider.name})
    store.add_action(s["id"], "analysis_submitted", provider.name,
                     "verify + deepfake" if speaker_id else "deepfake only")
    await _run_recorded(s["id"])
    return session_view(s["id"])


@app.post("/api/sessions/{session_id}/rerun")
async def rerun(session_id: str):
    s = store.get_session(session_id)
    if not s or s["mode"] != "recorded":
        raise HTTPException(404, "recorded session not found")
    store.add_action(session_id, "analysis_rerun", provider.name, "same Idempotency-Key (no double charge)")
    await _run_recorded(session_id)
    return session_view(session_id)


# ------------------------------------------------------------------ sessions & operator actions
@app.get("/api/sessions")
async def list_sessions(mode: Optional[str] = None, limit: int = 100):
    return {"sessions": store.list_sessions(mode, limit)}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    return session_view(session_id)


class ActionUpdate(BaseModel):
    action_type: str
    amount: Optional[float] = None


@app.post("/api/sessions/{session_id}/action")
async def set_action(session_id: str, body: ActionUpdate):
    if not store.get_session(session_id):
        raise HTTPException(404, "session not found")
    await pipeline.set_action(session_id, body.action_type, body.amount)
    return session_view(session_id)


class StepUp(BaseModel):
    code: str


@app.post("/api/sessions/{session_id}/step-up")
async def step_up(session_id: str, body: StepUp):
    if not store.get_session(session_id):
        raise HTTPException(404, "session not found")
    await pipeline.submit_step_up(session_id, body.code)
    return session_view(session_id)


class Review(BaseModel):
    approved: bool
    reviewer: str = "operator"
    note: str = ""


@app.post("/api/sessions/{session_id}/review")
async def review(session_id: str, body: Review):
    if not store.get_session(session_id):
        raise HTTPException(404, "session not found")
    await pipeline.submit_review(session_id, body.approved, body.reviewer, body.note)
    return session_view(session_id)


@app.get("/api/decisions")
async def decisions(limit: int = 200):
    return {"decisions": store.list_decisions(limit=limit)}


@app.get("/api/raw-events")
async def raw_events(limit: int = 100):
    return {"raw_events": store.list_raw(limit)}


# ------------------------------------------------------------------ Mode B helpers
class Simulation(BaseModel):
    scenario: str = "genuine"
    speaker_id: Optional[str] = "demo-customer"
    action_type: str = "transfer_new_payee"
    amount: Optional[float] = 5000
    speed: float = 1.0


@app.post("/api/simulate/live")
async def simulate(body: Simulation):
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"scenario must be one of {list(SCENARIOS)}")
    if body.action_type not in policy_cfg.actions:
        raise HTTPException(400, "unknown action_type")
    s = store.create_session("simulated", speaker_id=body.speaker_id,
                             details={"scenario": body.scenario, "label": SCENARIOS[body.scenario]["label"]},
                             meta={"provider": "simulator"})
    store.add_action(s["id"], "call_received", "simulated", SCENARIOS[body.scenario]["label"])
    _spawn(run_simulation(pipeline, s["id"], body.scenario, body.action_type, body.amount, body.speed))
    return {"session_id": s["id"]}


@app.post("/webhooks/detectif")
async def detectif_webhook(request: Request):
    raw = await request.body()
    if settings.detectif_webhook_secret:
        try:
            verify(settings.detectif_webhook_secret, request.headers, raw)
        except WebhookVerificationError as e:
            log.warning("webhook rejected: %s", e)
            return JSONResponse({"error": str(e)}, status_code=401)
    envelope = json.loads(raw or b"{}")
    ref, stream_id = webhook_reference(envelope)
    s = store.get_session(ref) if ref else None
    if s is None and ref:
        s = store.get_session_by_call(ref)  # reference defaults to Twilio's CallSid
    store.add_raw("webhook", envelope, s["id"] if s else None,
                  note=envelope.get("type", "") + ("" if s else " (unrouted)"))
    if s is not None:
        if stream_id and not s.get("stream_id"):
            store.update_session(s["id"], stream_id=stream_id)
        evs = from_webhook(s["id"], envelope)
        if evs:
            _spawn(pipeline.ingest(s["id"], evs))  # answer within 10 s; process in background
    return {"ok": True}

