"""Decision table tests. Run: pytest -q"""
from pathlib import Path

import pytest

from vigil.models import ActionContext, Evidence, EvidenceState
from vigil.policy import PolicyConfig, evaluate

CFG = PolicyConfig.load(Path(__file__).resolve().parent.parent / "config" / "policy.yaml")


def a(verdict, final=True, speech=12.0, early=False):
    return Evidence(session_id="s", source="aletheia", verdict=verdict, final=final, early=early,
                    speech_seconds=speech, score=0.9 if verdict == "fake" else 0.05)


def i(verdict, final=True):
    return Evidence(session_id="s", source="eidos", verdict=verdict, final=final, score=0.7)


def step(v):
    return Evidence(session_id="s", source="step_up", verdict=v)


def decide(auth=None, ident=None, action="check_balance", amount=None, live=False, completed=False, **extra):
    st = EvidenceState(authenticity=auth, identity=ident, stream_completed=completed, **extra)
    return evaluate(st, ActionContext(action_type=action, amount=amount), CFG, live=live)


# ---- the core table -------------------------------------------------------
def test_real_match_low_allows():
    d = decide(a("real"), i("match"), "check_balance")
    assert d.decision == "ALLOW" and d.action_allowed


def test_real_match_high_steps_up():
    assert decide(a("real"), i("match"), "transfer_new_payee").decision == "STEP_UP"


def test_real_no_match_escalates():
    assert decide(a("real"), i("no_match")).rule_id == "R2_speaker_mismatch"


@pytest.mark.parametrize("action", ["check_balance", "transfer_new_payee", "mfa_reset", "exam_check_in"])
def test_fake_never_cleared(action):
    d = decide(a("fake"), i("match"), action, step_up=step("passed"))
    assert d.decision == "ESCALATE" and not d.action_allowed
    assert "clone" in d.reason


def test_fake_no_match_escalates():
    assert decide(a("fake"), i("no_match")).decision == "ESCALATE"


# ---- risk ---------------------------------------------------------------
def test_amount_raises_tier():
    assert decide(a("real"), i("match"), "transfer_existing_payee", amount=1000).decision == "ALLOW"
    assert decide(a("real"), i("match"), "transfer_existing_payee", amount=60000).decision == "STEP_UP"


def test_unknown_action_is_high_risk():
    assert decide(a("real"), i("match"), "something_new").decision == "STEP_UP"


def test_step_up_passed_allows_high():
    d = decide(a("real"), i("match"), "transfer_new_payee", step_up=step("passed"))
    assert d.decision == "ALLOW"


def test_step_up_failed_escalates():
    assert decide(a("real"), i("match"), "transfer_new_payee", step_up=step("failed")).decision == "ESCALATE"


# ---- weak / missing evidence ----------------------------------------------
def test_short_audio_high_risk_steps_up():
    d = decide(a("real", speech=4), i("match"), "transfer_new_payee")
    assert d.rule_id == "R7_weak_evidence"


def test_not_enrolled_steps_up():
    assert decide(a("real"), i("not_enrolled"), "check_balance").decision == "STEP_UP"


def test_weak_on_critical_escalates():
    assert decide(a("inconclusive"), i("match"), "mfa_reset").decision == "ESCALATE"


def test_provider_error_holds():
    assert decide(a("error"), i("match")).decision == "HOLD"


# ---- live finality ----------------------------------------------------------
def test_live_waits_for_final_on_high_risk():
    d = decide(a("real", final=False, early=True, speech=3), i("match"), "transfer_new_payee", live=True)
    assert d.rule_id == "R5_awaiting_final"


def test_live_low_risk_acts_on_early():
    d = decide(a("real", final=False, early=True, speech=3), i("match"), "check_balance", live=True)
    assert d.decision == "ALLOW"


def test_live_early_fake_escalates_immediately():
    d = decide(a("fake", final=False, early=True), None, "check_balance", live=True)
    assert d.decision == "ESCALATE"


def test_live_no_action_yet_holds():
    d = decide(a("real"), i("match"), action=None, live=True)
    assert d.rule_id == "R4_awaiting_request"
