"""Normalizer tests using payload shapes from the DetectifAI docs."""
from vigil.evidence import (
    build_state, from_api_error, from_detect_response, from_stream_message, from_verify_response, from_webhook,
)

DETECT = {
    "request_id": "req_1", "operation_id": "op_1", "object": "deepfake.detection",
    "result": {"verdict": "real", "label": "genuine", "is_synthetic": False, "score": 0.0731,
               "duration_seconds": 10.4, "scored_seconds": 10.4,
               "segments": [{"start": 0, "end": 4, "score": 0.061, "verdict": "real"}]},
}

VERIFY = {
    "request_id": "req_2", "operation_id": "op_2", "object": "speaker.verification", "speaker_id": "alice",
    "result": {"decision": "match", "similarity": 0.7412, "duration_seconds": 4.1},
    "deepfake": {"verdict": "fake", "label": "synthetic", "is_synthetic": True, "score": 0.93,
                 "duration_seconds": 4.1, "scored_seconds": 4.1, "operation_id": "op_2d",
                 "segments": [{"start": 1, "end": 4, "score": 0.95, "verdict": "fake"}]},
}


def test_detect_response():
    [e] = from_detect_response("s", DETECT)
    assert (e.source, e.verdict, e.speech_seconds, e.dedupe_key) == ("aletheia", "real", 10.4, "op_1:aletheia")
    assert len(e.segments) == 1


def test_verify_with_deepfake():
    evs = from_verify_response("s", VERIFY)
    by = {e.source: e for e in evs}
    assert by["eidos"].verdict == "match" and by["eidos"].score == 0.7412
    assert by["aletheia"].verdict == "fake" and by["aletheia"].segments[0].verdict == "fake"


def test_label_fallback():
    body = {"operation_id": "x", "result": {"label": "synthetic", "score": 0.8}}
    assert from_detect_response("s", body)[0].verdict == "fake"


def test_errors():
    assert from_api_error("s", "eidos", 404, {"error": {"code": "not_found"}})[0].verdict == "not_enrolled"
    assert from_api_error("s", "aletheia", 422, {"error": {"code": "insufficient_speech"}})[0].verdict == "inconclusive"
    assert from_api_error("s", "aletheia", 502, {"error": {"code": "x"}})[0].verdict == "error"


def test_partial_recovers_deepfake():
    body = {"error": {"code": "audio_too_long"}, "partial": {"verdict": "real", "score": 0.1}}
    evs = from_api_error("s", "eidos", 422, body)
    assert {e.source for e in evs} == {"eidos", "aletheia"}


def test_stream_finality():
    msgs = [
        {"type": "ready", "stream_id": "st1", "checks": ["deepfake", "verify"]},
        {"type": "deepfake", "early": True, "final": False, "sequence": 0, "verdict": "real", "score": 0.1,
         "speech_seconds": 3},
        {"type": "deepfake", "early": False, "final": False, "sequence": 1, "verdict": "fake", "score": 0.8,
         "speech_seconds": 10},
        {"type": "completed", "status": "completed", "speech_seconds": 20,
         "results": {"deepfake": {"result": {"verdict": "real", "score": 0.2, "segments": []}},
                     "verification": {"operation_id": "op_v", "result": {"decision": "match", "similarity": 0.7}}}},
    ]
    evs = []
    for m in msgs:
        evs += from_stream_message("s", m, stream_id="st1")
    st = build_state(evs)
    assert st.authenticity.final and st.authenticity.verdict == "real"
    assert st.identity.verdict == "match"
    assert st.stream_completed


def test_build_state_prefers_latest_update_before_final():
    evs = from_stream_message("s", {"type": "deepfake", "early": True, "verdict": "real", "sequence": 0}, "st") + \
        from_stream_message("s", {"type": "deepfake", "verdict": "fake", "sequence": 1}, "st")
    assert build_state(evs).authenticity.verdict == "fake"


def test_webhook_stream_update():
    env = {"id": "msg_1", "type": "stream.deepfake.updated",
           "data": {"stream_id": "st9", "reference": "ses_1", "verdict": "real", "score": 0.1, "sequence": 2}}
    [e] = from_webhook("ses_1", env)
    assert e.verdict == "real" and e.dedupe_key == "st9:deepfake:seq2"
