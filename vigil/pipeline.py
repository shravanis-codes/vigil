"""
The shared pipeline. Recorded uploads, live relay messages, webhooks and the
simulator ALL enter here, so there is exactly one decision logic.

    evidence -> validate -> dedupe -> store -> build state -> policy -> audit -> act
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from .actions import Orchestrator
from .evidence import build_state, validate
from .models import (
    REVIEW, STEP_UP, ActionContext, Decision, Evidence, EvidenceState,
)
from .policy import PolicyConfig, evaluate

log = logging.getLogger("vigil.pipeline")

_SNAPSHOT_EXCLUDE = {
    "authenticity": {"raw"}, "identity": {"raw"}, "step_up": {"raw"}, "review": {"raw"},
    "errors": {"__all__": {"raw"}},
}


class Pipeline:
    def __init__(self, store, policy_cfg: PolicyConfig, provider, settings):
        self.store = store
        self.cfg = policy_cfg
        self.provider = provider
        self.settings = settings
        self.orchestrator = Orchestrator(store, policy_cfg, settings)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    # ---------------------------------------------------------------- read side
    def evidence(self, session_id: str) -> List[Evidence]:
        return [Evidence(**e["evidence"]) for e in self.store.list_events(session_id)]

    def state(self, session_id: str) -> EvidenceState:
        return build_state(self.evidence(session_id))

    @staticmethod
    def context(session: Dict[str, Any]) -> ActionContext:
        return ActionContext(action_type=session.get("action_type"), amount=session.get("amount"),
                             currency=session.get("currency") or "INR", details=session.get("details") or {})

    def evaluate(self, session_id: str) -> Decision:
        s = self.store.get_session(session_id)
        if s is None:
            raise KeyError(session_id)
        return evaluate(self.state(session_id), self.context(s), self.cfg, live=s["mode"] in ("live", "simulated"))

    # ---------------------------------------------------------------- write side
    async def ingest(self, session_id: str, evidences: List[Evidence]) -> Optional[Decision]:
        """Store new evidence and re-decide. Returns the decision if anything new arrived."""
        async with self._lock(session_id):
            new = 0
            for ev in evidences:
                ev.session_id = session_id
                # Duplicates are per session: a provider replay (same Idempotency-Key ->
                # same operation_id) must still count as evidence for a NEW session.
                if ev.dedupe_key and not ev.dedupe_key.startswith(session_id):
                    ev.dedupe_key = f"{session_id}|{ev.dedupe_key}"
                ev = validate(ev)
                if self.store.add_event(ev.model_dump()):
                    new += 1
            if new == 0:
                return None  # all duplicates
            return await self._decide(session_id)

    async def redecide_if_changed(self, session_id: str) -> Decision:
        """Evaluate now; record + act only if the decision changed."""
        async with self._lock(session_id):
            return await self._decide(session_id)

    async def redecide(self, session_id: str) -> Decision:
        async with self._lock(session_id):
            return await self._decide(session_id, force=True)

    async def _decide(self, session_id: str, force: bool = False) -> Decision:
        s = self.store.get_session(session_id)
        state = self.state(session_id)
        d = evaluate(state, self.context(s), self.cfg, live=s["mode"] in ("live", "simulated"))
        prev = self.store.latest_decision(session_id)
        if force or prev is None or prev["decision"] != d.decision or prev["rule_id"] != d.rule_id:
            self.store.add_decision(session_id, d.model_dump(), state.model_dump(exclude=_SNAPSHOT_EXCLUDE))
            s = self.store.get_session(session_id)
            await self.orchestrator.on_decision(s, d, prev)
        return d

    async def set_action(self, session_id: str, action_type: str, amount: Optional[float] = None,
                         details: Optional[dict] = None) -> Decision:
        fields: Dict[str, Any] = {"action_type": action_type}
        if amount is not None:
            fields["amount"] = amount
        if details is not None:
            fields["details"] = details
        self.store.update_session(session_id, **fields)
        return await self.redecide(session_id)

    async def submit_step_up(self, session_id: str, code: str, origin: str = "operator") -> Decision:
        s = self.store.get_session(session_id)
        meta = s.get("meta") or {}
        expected = meta.get("otp")
        attempts = int(meta.get("otp_attempts", 0)) + 1
        self.store.update_meta(session_id, otp_attempts=attempts)
        ok = bool(expected) and str(code).strip() == expected
        if not ok and attempts < 3:
            self.store.add_action(session_id, "step_up_attempt", "wrong code", f"attempt {attempts} of 3")
            return self.evaluate(session_id)
        ev = Evidence(session_id=session_id, source=STEP_UP, verdict="passed" if ok else "failed",
                      origin=origin, dedupe_key=f"{session_id}:stepup:{attempts}",
                      message="One-time code verified" if ok else "Too many wrong codes")
        return await self.ingest(session_id, [ev]) or self.evaluate(session_id)

    async def submit_review(self, session_id: str, approved: bool, reviewer: str, note: str = "") -> Decision:
        ev = Evidence(session_id=session_id, source=REVIEW, verdict="approved" if approved else "denied",
                      origin="operator", message=f"{reviewer}: {note}".strip(": "),
                      dedupe_key=f"{session_id}:review:{len(self.store.list_events(session_id))}")
        self.store.add_action(session_id, "human_review", "approved" if approved else "denied",
                              f"{reviewer}: {note}")
        return await self.ingest(session_id, [ev]) or self.evaluate(session_id)
