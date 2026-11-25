"""
VIGIL Policy Engine.

Input : EvidenceState (identity + authenticity + step-up + review) and ActionContext
Output: one Decision -> ALLOW | STEP_UP | HOLD | ESCALATE, with a rule id and a trace.

The rules are ordered. The first rule that fires wins. Principles:
  1. A synthetic-voice flag can never be cleared by a voice match or a step-up.
  2. A different speaker is never allowed through.
  3. Evidence that is missing, early or weak never ALLOWs a risky action.
  4. Every decision explains itself (rule_id + trace).
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .models import (
    ALLOW, ESCALATE, FAKE, HOLD, MATCH, NO_MATCH, REAL, STEP_UP_DECISION,
    ActionContext, Decision, EvidenceState,
)


class PolicyConfig:
    def __init__(self, data: Dict[str, Any]):
        self.tiers = data.get("risk_tiers", ["low", "medium", "high", "critical"])
        self.min_reliable_speech = float(data.get("min_reliable_speech_seconds", 10))
        self.wait_for_final_from = data.get("wait_for_final_from_tier", "medium")
        self.actions: Dict[str, Dict[str, Any]] = data.get("actions", {}) or {}
        self.domains: Dict[str, Dict[str, str]] = data.get("domains", {}) or {}

    @classmethod
    def load(cls, path: Path) -> "PolicyConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})

    def rank(self, tier: str) -> int:
        return self.tiers.index(tier) if tier in self.tiers else len(self.tiers) - 1

    def domain_of(self, action_type: Optional[str]) -> Dict[str, str]:
        action = self.actions.get(action_type or "", {})
        return self.domains.get(action.get("domain", ""), {
            "step_up": "Ask for an additional verification factor",
            "escalate_to": "Human review team",
        })


def compute_risk(ctx: ActionContext, cfg: PolicyConfig) -> Tuple[str, List[str]]:
    """Risk tier for the requested action. Unknown actions are treated as high risk."""
    trace = []
    action = cfg.actions.get(ctx.action_type or "")
    if action is None:
        trace.append(f"Unknown action '{ctx.action_type}': treated as high risk.")
        return "high", trace
    tier = action.get("base_risk", "high")
    trace.append(f"Action '{action.get('label', ctx.action_type)}' has base risk {tier}.")
    amount_tiers = action.get("amount_tiers") or {}
    if ctx.amount is not None:
        for t, threshold in amount_tiers.items():
            if ctx.amount >= float(threshold) and cfg.rank(t) > cfg.rank(tier):
                trace.append(f"Amount {ctx.amount:,.0f} >= {float(threshold):,.0f}: raised to {t}.")
                tier = t
    return tier, trace


def evaluate(state: EvidenceState, ctx: ActionContext, cfg: PolicyConfig, live: bool = False) -> Decision:
    a, i = state.authenticity, state.identity
    dom = cfg.domain_of(ctx.action_type)
    escalate_to = dom.get("escalate_to", "Human review team")
    step_up_text = dom.get("step_up", "Ask for an additional verification factor")
    trace = []

    def out(decision, rule, reason, next_step, tier=None, allowed=False):
        trace.append(f"[{rule}] {reason}")
        return Decision(decision=decision, rule_id=rule, reason=reason, next_step=next_step,
                        risk_tier=tier, action_allowed=allowed, trace=list(trace))

    # Describe the evidence we have.
    if a:
        trace.append(f"Aletheia: {a.verdict}" + (f" (score {a.score:.2f})" if a.score is not None else "")
                     + (" [final]" if a.final else " [early]" if a.early else " [update]"))
    else:
        trace.append("Aletheia: no result yet.")
    if i:
        trace.append(f"Eidos: {i.verdict}" + (f" (similarity {i.score:.2f})" if i.score is not None else ""))
    else:
        trace.append("Eidos: no result yet.")

    # R0 Human reviewer is final.
    if state.review is not None:
        if state.review.verdict == "approved":
            return out(ALLOW, "R0_review_approved", "Approved by a human reviewer.",
                       "Proceed with the request", allowed=True)
        return out(ESCALATE, "R0_review_denied", "Denied by a human reviewer.",
                   "Request refused; case closed")

    # R1 Synthetic voice: nothing can clear it.
    if a is not None and a.verdict == FAKE:
        if i is not None and i.verdict == MATCH:
            reason = "Voice flagged as synthetic but it matches the enrolled speaker: likely a voice clone."
        else:
            reason = "Voice flagged as synthetic."
        if not a.final:
            reason += " (Live verdict; escalated immediately rather than waiting.)"
        return out(ESCALATE, "R1_synthetic_voice", reason,
                   f"Block the request and send to {escalate_to}")

    # R2 Real person, wrong identity.
    if i is not None and i.verdict == NO_MATCH:
        return out(ESCALATE, "R2_speaker_mismatch",
                   "Voice does not match the enrolled speaker's voiceprint.",
                   f"Block the request and send to {escalate_to}")

    # R3 Failed step-up.
    if state.step_up is not None and state.step_up.verdict == "failed":
        return out(ESCALATE, "R3_step_up_failed", "Additional verification failed.",
                   f"Block the request and send to {escalate_to}")

    # R4 No request yet (live call before the caller chose an action).
    if not ctx.action_type:
        return out(HOLD, "R4_awaiting_request", "No action requested yet; monitoring the voice.",
                   "Wait for the caller's request")

    tier, risk_trace = compute_risk(ctx, cfg)
    trace.extend(risk_trace)
    high = cfg.rank(tier) >= cfg.rank("high")
    critical = cfg.rank(tier) >= cfg.rank("critical")
    needs_final = cfg.rank(tier) >= cfg.rank(cfg.wait_for_final_from)
    step_passed = state.step_up is not None and state.step_up.verdict == "passed"

    # R5 Live call: evidence not ready yet.
    if live and not state.stream_completed:
        if a is None:
            return out(HOLD, "R5_awaiting_authenticity", "Waiting for the first deepfake verdict.",
                       "Keep the caller talking", tier)
        if needs_final and not a.final and not (a.speech_seconds or 0) >= cfg.min_reliable_speech:
            return out(HOLD, "R5_awaiting_final",
                       f"{tier.capitalize()}-risk request: waiting for more audio before trusting the verdict.",
                       "Keep the caller talking", tier)
        if i is None:
            return out(HOLD, "R5_awaiting_identity", "Waiting for the speaker verification result.",
                       "Keep the caller talking", tier)

    # R6 Provider/technical problems.
    if a is None or a.verdict == "error":
        msg = a.message if a is not None and a.message else "No deepfake result was produced."
        return out(HOLD, "R6_authenticity_unavailable", f"Authenticity check unavailable: {msg}",
                   "Retry the analysis, or send to manual review", tier)

    # Collect weaknesses in the evidence.
    weak = []
    if a.verdict != REAL:
        weak.append("authenticity inconclusive" + (f" ({a.message})" if a.message else ""))
    elif high and a.speech_seconds is not None and a.speech_seconds < cfg.min_reliable_speech:
        weak.append(f"only {a.speech_seconds:.1f}s of speech (< {cfg.min_reliable_speech:.0f}s) for a {tier}-risk action")
    if i is None:
        weak.append("no speaker verification")
    elif i.verdict != MATCH:
        weak.append({"not_enrolled": "speaker not enrolled",
                     "inconclusive": "speaker verification inconclusive",
                     "error": "speaker verification failed to run"}.get(i.verdict, i.verdict)
                    + (f" ({i.message})" if i.message else ""))

    # R7 Weak evidence.
    if weak:
        why = "; ".join(weak)
        if critical:
            return out(ESCALATE, "R7_weak_evidence_critical",
                       f"Cannot verify the caller for a critical action: {why}.",
                       f"Send to {escalate_to}", tier)
        if step_passed:
            return out(ALLOW, "R7_weak_evidence_step_up_passed",
                       f"Voice evidence was weak ({why}) but the extra verification passed.",
                       "Proceed with the request", tier, allowed=True)
        return out(STEP_UP_DECISION, "R7_weak_evidence", f"Voice evidence is weak: {why}.", step_up_text, tier)

    # R8 Real voice + right person.
    if not high:
        return out(ALLOW, "R8_verified_low_risk", "Real voice and right person on a lower-risk request.",
                   "Proceed with the request", tier, allowed=True)
    if step_passed:
        return out(ALLOW, "R8_verified_step_up_passed",
                   "Real voice, right person, and the extra verification passed.",
                   "Proceed with the request", tier, allowed=True)
    return out(STEP_UP_DECISION, "R8_verified_high_risk",
               f"Real voice and right person, but the request is {tier} risk.", step_up_text, tier)
