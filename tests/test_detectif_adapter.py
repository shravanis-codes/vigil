"""DetectifAI adapter against a fake server that returns the documented response shapes."""
import asyncio

import httpx

from vigil.providers.detectif import DetectifProvider

VERIFY_OK = {
    "request_id": "req_1", "operation_id": "op_v", "object": "speaker.verification", "speaker_id": "alice",
    "result": {"decision": "match", "similarity": 0.74, "duration_seconds": 12.0},
    "deepfake": {"verdict": "real", "label": "genuine", "is_synthetic": False, "score": 0.05,
                 "duration_seconds": 12.0, "scored_seconds": 12.0, "segments": [], "operation_id": "op_vd"},
}
DETECT_OK = {"request_id": "req_2", "operation_id": "op_d", "object": "deepfake.detection",
             "result": {"verdict": "fake", "score": 0.91, "duration_seconds": 70, "scored_seconds": 70,
                        "segments": [{"start": 3, "end": 7, "score": 0.95, "verdict": "fake"}]}}


def run(handler, coro_fn):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request, len(calls))

    p = DetectifProvider("sk_test", transport=httpx.MockTransport(wrapped), retry_sleep=0)
    return asyncio.run(coro_fn(p)), calls


def test_combined_verify(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"RIFF....")
    evs, calls = run(lambda r, n: httpx.Response(200, json=VERIFY_OK),
                     lambda p: p.analyze_recording("s", str(f), "alice", "key1"))
    assert {(e.source, e.verdict) for e in evs} == {("eidos", "match"), ("aletheia", "real")}
    req = calls[0]
    assert req.url.path == "/v1/speakers/verify"
    assert req.headers["Authorization"] == "Bearer sk_test"
    assert "sk_test" not in str(req.url)                       # key never in the URL
    assert req.headers["Idempotency-Key"] == "key1-v"
    assert b'name="detect_deepfake"' in req.content and b"true" in req.content


def test_not_enrolled_falls_back_to_detect(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x")

    def handler(r, n):
        if r.url.path == "/v1/speakers/verify":
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "no speaker"}})
        return httpx.Response(200, json=DETECT_OK)
    evs, calls = run(handler, lambda p: p.analyze_recording("s", str(f), "ghost", "k"))
    assert [c.url.path for c in calls] == ["/v1/speakers/verify", "/v1/deepfake/detect"]
    assert {(e.source, e.verdict) for e in evs} == {("eidos", "not_enrolled"), ("aletheia", "fake")}


def test_retry_keeps_idempotency_key_and_resends_file(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"AUDIODATA")

    def handler(r, n):
        return httpx.Response(503, headers={"Retry-After": "1"}, json={"error": {"code": "busy"}}) if n == 1 \
            else httpx.Response(200, json=DETECT_OK)
    evs, calls = run(handler, lambda p: p.analyze_recording("s", str(f), None, "k2"))
    assert len(calls) == 2
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"] == "k2-d"
    assert b"AUDIODATA" in calls[1].content
    assert evs[0].verdict == "fake"


def test_server_error_becomes_error_evidence(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x")
    evs, _ = run(lambda r, n: httpx.Response(502, json={"error": {"code": "upstream"}}),
                 lambda p: p.analyze_recording("s", str(f), None, "k3"))
    assert evs[0].verdict == "error"
