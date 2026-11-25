"""End-to-end API tests in mock mode: recorded mode, operator actions, simulator, webhook, Twilio flow, relay."""
import base64
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.make_demo_audio import main as make_audio
from vigil import webhooks
from vigil.api import app, settings, store

AUDIO = Path(settings.data_dir) / "demo_audio"


@pytest.fixture(scope="module")
def client():
    make_audio(AUDIO)
    with TestClient(app) as c:
        yield c


def analyze(client, name, action="transfer_new_payee", speaker="demo-customer", amount=5000):
    with open(AUDIO / name, "rb") as f:
        data = {"action_type": action, "amount": str(amount)}
        if speaker:
            data["speaker_id"] = speaker
        r = client.post("/api/recorded/analyze", files={"file": (name, f, "audio/wav")}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    r = client.get("/health").json()
    assert r["ok"] and r["config"]["provider"] == "mock"


def test_recorded_genuine_high_risk_steps_up_then_allows(client):
    v = analyze(client, "genuine_customer.wav")
    assert v["decision"]["decision"] == "STEP_UP"
    otp = v["session"]["meta"]["otp"]
    sid = v["session"]["id"]
    v = client.post(f"/api/sessions/{sid}/step-up", json={"code": otp}).json()
    assert v["decision"]["decision"] == "ALLOW"
    assert any(a["action"].startswith("execute:") for a in v["actions"])


def test_recorded_low_risk_allows(client):
    assert analyze(client, "genuine_customer.wav", action="check_balance")["decision"]["decision"] == "ALLOW"


def test_recorded_clone_escalates(client):
    v = analyze(client, "synthetic_clone_of_customer.wav")
    assert v["decision"]["rule_id"] == "R1_synthetic_voice"
    assert "clone" in v["decision"]["reason"]
    assert v["state"]["authenticity"]["segments"]


def test_recorded_impostor_escalates(client):
    assert analyze(client, "impostor_other_person.wav")["decision"]["rule_id"] == "R2_speaker_mismatch"


def test_recorded_short_clip(client):
    v = analyze(client, "short_clip.wav", action="check_balance")
    assert v["decision"]["decision"] == "STEP_UP"
    assert any("2.0s" in w or "only" in w for w in v["session"]["meta"]["warnings"])


def test_not_enrolled_and_no_speaker(client):
    assert analyze(client, "genuine_customer.wav", action="check_balance",
                   speaker="nobody-here")["state"]["identity"]["verdict"] == "not_enrolled"
    v = analyze(client, "genuine_customer.wav", action="check_balance", speaker=None)
    assert v["decision"]["decision"] == "STEP_UP"


def test_wrong_codes_then_escalate(client):
    sid = analyze(client, "genuine_customer.wav")["session"]["id"]
    for _ in range(3):
        v = client.post(f"/api/sessions/{sid}/step-up", json={"code": "000000x"}).json()
    assert v["decision"]["rule_id"] == "R3_step_up_failed"


def test_review_override(client):
    sid = analyze(client, "synthetic_clone_of_customer.wav")["session"]["id"]
    v = client.post(f"/api/sessions/{sid}/review", json={"approved": False, "reviewer": "asha"}).json()
    assert v["decision"]["rule_id"] == "R0_review_denied"


def test_enroll(client):
    with open(AUDIO / "enroll_sample_20s.wav", "rb") as f:
        r = client.post("/api/speakers/enroll", files={"file": ("e.wav", f, "audio/wav")},
                        data={"speaker_id": "bhagwandas", "display_name": "B", "phone": "+911234567890"})
    assert r.status_code == 200, r.text
    ids = [s["speaker_id"] for s in client.get("/api/speakers").json()["speakers"]]
    assert "bhagwandas" in ids


def _wait(client, sid, pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        v = client.get(f"/api/sessions/{sid}").json()
        if pred(v):
            return v
        time.sleep(0.2)
    return v


@pytest.mark.parametrize("scenario,expected", [("genuine", "STEP_UP"), ("clone", "ESCALATE"),
                                               ("impostor", "ESCALATE"), ("short_call", "STEP_UP")])
def test_simulated_live(client, scenario, expected):
    sid = client.post("/api/simulate/live", json={"scenario": scenario, "speed": 6}).json()["session_id"]
    v = _wait(client, sid, lambda v: v["session"]["status"] == "closed")
    assert v["decision"]["decision"] == expected, v["decision"]
    hist = [d["decision"] for d in v["decisions"]]
    assert hist[0] == "HOLD"  # evidence builds up before a decision


def test_webhook_signature_and_routing(client, monkeypatch):
    secret = "whsec_" + base64.b64encode(b"supersecretkey123").decode()
    monkeypatch.setattr(settings, "detectif_webhook_secret", secret)
    sid = store.create_session("live", speaker_id="demo-customer", action_type="check_balance")["id"]
    env = {"id": "msg_1", "type": "stream.deepfake.updated", "created_at": "x",
           "data": {"stream_id": "st_w", "reference": sid, "verdict": "fake", "score": 0.9, "sequence": 1}}
    body = json.dumps(env).encode()
    ts = str(int(time.time()))
    good = {"webhook-id": "msg_1", "webhook-timestamp": ts,
            "webhook-signature": webhooks.sign(secret, "msg_1", ts, body), "content-type": "application/json"}
    assert client.post("/webhooks/detectif", content=body, headers=dict(good, **{"webhook-signature": "v1,bad"})
                       ).status_code == 401
    assert client.post("/webhooks/detectif", content=body, headers=good).status_code == 200
    v = _wait(client, sid, lambda v: v["decision"]["decision"] == "ESCALATE")
    assert v["decision"]["rule_id"] == "R1_synthetic_voice"


def test_twilio_call_flow_and_relay_mock(client):
    store.upsert_speaker("caller-1", "Caller", "+919999999999", 1)
    r = client.post("/twilio/voice", data={"From": "+919999999999", "CallSid": "CA123"})
    assert r.status_code == 200 and "<Stream" in r.text and "/twilio/media" in r.text
    sid = store.get_session_by_call("CA123")["id"]
    assert "Press 2" in client.post(f"/twilio/menu?session_id={sid}", data={}).text
    r = client.post(f"/twilio/action?session_id={sid}", data={"Digits": "1"})
    assert "/twilio/hold" in r.text

    # relay: mock provider replays a genuine stream
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({"event": "start", "start": {"callSid": "CA123", "streamSid": "MZ1",
                                                             "customParameters": {"session_id": sid}}}))
        frame = base64.b64encode(b"\xff" * 160).decode()
        for _ in range(20):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": frame, "track": "inbound"}}))
        v = _wait(client, sid, lambda v: v["state"]["identity"] is not None, timeout=15)
        ws.send_text(json.dumps({"event": "stop"}))
    assert v["state"]["authenticity"]["verdict"] == "real"
    r = client.post(f"/twilio/hold?session_id={sid}&n=0", data={})
    assert "completed" in r.text or "Still verifying" in r.text


def test_twilio_unknown_caller_has_no_identity(client):
    client.post("/twilio/voice", data={"From": "+910000000000", "CallSid": "CA999"})
    sid = store.get_session_by_call("CA999")["id"]
    v = client.get(f"/api/sessions/{sid}").json()
    assert v["state"]["identity"]["verdict"] == "not_enrolled"
