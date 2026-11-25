"""
Internal data structures.

Every provider result (recorded upload, live stream message, webhook, simulator)
is converted into the SAME Evidence shape. The policy engine only ever sees
Evidence + ActionContext, so recorded and live calls share one decision logic.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Evidence sources
ALETHEIA = "aletheia"   # authenticity: real vs synthetic voice
EIDOS = "eidos"         # identity: is it the enrolled speaker
STEP_UP = "step_up"     # result of an extra verification factor
REVIEW = "review"       # human reviewer outcome
SYSTEM = "system"       # stream lifecycle, provider errors

# Normalized verdicts
REAL, FAKE = "real", "fake"
MATCH, NO_MATCH = "match", "no_match"
NOT_ENROLLED = "not_enrolled"
INCONCLUSIVE = "inconclusive"
ERROR = "error"
PASSED, FAILED = "passed", "failed"
INFO = "info"

VALID_VERDICTS = {
    ALETHEIA: {REAL, FAKE, INCONCLUSIVE, ERROR},
    EIDOS: {MATCH, NO_MATCH, NOT_ENROLLED, INCONCLUSIVE, ERROR},
    STEP_UP: {PASSED, FAILED},
    REVIEW: {"approved", "denied"},
    SYSTEM: {INFO, ERROR},
}

# Decisions
ALLOW = "ALLOW"
STEP_UP_DECISION = "STEP_UP"
HOLD = "HOLD"
ESCALATE = "ESCALATE"
DECISIONS = (ALLOW, STEP_UP_DECISION, HOLD, ESCALATE)


class Segment(BaseModel):
    start: float
    end: float
    score: Optional[float] = None
    verdict: Optional[str] = None


class Evidence(BaseModel):
    session_id: str
    source: str                       # aletheia | eidos | step_up | review | system
    verdict: str                      # see VALID_VERDICTS
    final: bool = True                # live streams send non-final updates first
    early: bool = False
    score: Optional[float] = None     # aletheia: P(synthetic) 0..1 ; eidos: similarity -1..1
    score_type: Optional[str] = None  # "synthetic_probability" | "similarity"
    speech_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None
    sequence: Optional[int] = None
    segments: List[Segment] = Field(default_factory=list)
    provider_ref: Optional[str] = None   # operation_id / stream_id
    error_code: Optional[str] = None
    message: Optional[str] = None
    origin: str = "unknown"              # recorded | stream | webhook | simulator | operator
    dedupe_key: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class ActionContext(BaseModel):
    action_type: Optional[str] = None
    amount: Optional[float] = None
    currency: str = "INR"
    details: Dict[str, Any] = Field(default_factory=dict)


class EvidenceState(BaseModel):
    """The best current view of a session, derived from all its evidence."""
    authenticity: Optional[Evidence] = None
    identity: Optional[Evidence] = None
    step_up: Optional[Evidence] = None
    review: Optional[Evidence] = None
    errors: List[Evidence] = Field(default_factory=list)
    stream_completed: bool = False


class Decision(BaseModel):
    decision: str
    rule_id: str
    reason: str
    next_step: str
    risk_tier: Optional[str] = None
    action_allowed: bool = False
    trace: List[str] = Field(default_factory=list)   # human-readable reasoning steps
