"""
Action Orchestration + Audit.

Turns a decision into something that happens, and records it:
  ALLOW     -> execute the (simulated) business action
  STEP_UP   -> issue a one-time code (simulated SMS; shown on the dashboard)
  HOLD      -> pause the action
  ESCALATE  -> block the action, queue for human review, and on a live call
               redirect the caller immediately through Twilio's Calls API
               (Twilio media streams cannot receive commands; call control is separate).
"""
import asyncio
import logging
import secrets
from typing import Any, Dict, Optional

from .models import ALLOW, ESCALATE, HOLD, STEP_UP_DECISION, Decision

log = logging.getLogger("vigil.actions")


class TwilioControl:
    def __init__(self, settings):
        self.settings = settings
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.settings.twilio_enabled

    def _get_client(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        return self._client

    async def redirect(self, call_sid: str, path: str) -> str:
        """Point an in-progress call at new TwiML (Calls API 'update')."""
        url = f"{self.settings.public_base_url}{path}"
        if not self.enabled:
            return f"skipped (Twilio not configured): would redirect to {url}"
        def _do():
            call = self._get_client().calls(call_sid).update(url=url, method="POST")
            return call.status
        try:
            status = await asyncio.to_thread(_do)
            return f"redirected to {path} (call status {status})"
        except Exception as e:  # call may already be over
            log.warning("Twilio redirect failed: %s", e)
            return f"redirect failed: {e}"


class Orchestrator:
    def __init__(self, store, policy_cfg, settings):
        self.store = store
        self.cfg = policy_cfg
        self.twilio = TwilioControl(settings)

    async def on_decision(self, session: Dict[str, Any], d: Decision, previous: Optional[Dict[str, Any]]) -> None:
        sid = session["id"]
        meta = session.get("meta") or {}
        changed = previous is None or previous["decision"] != d.decision
        label = (self.cfg.actions.get(session.get("action_type") or "", {}) or {}).get("label",
                                                                                     session.get("action_type"))

        if d.decision == ALLOW and d.action_allowed and not meta.get("executed"):
            amount = f" of {session.get('currency', 'INR')} {session['amount']:,.0f}" if session.get("amount") else ""
            self.store.add_action(sid, f"execute:{session.get('action_type')}", "completed (simulated)",
                                  f"{label}{amount}")
            self.store.update_meta(sid, executed=True)

        elif d.decision == STEP_UP_DECISION and not meta.get("otp"):
            otp = f"{secrets.randbelow(10**6):06d}"
            self.store.update_meta(sid, otp=otp, otp_attempts=0)
            self.store.add_action(sid, "step_up_challenge", "sent (simulated SMS)",
                                  f"{d.next_step}. Demo code: {otp}")

        elif d.decision == HOLD and changed:
            self.store.add_action(sid, "hold", "action paused", d.reason)

        elif d.decision == ESCALATE and changed:
            self.store.add_action(sid, "block_action", "blocked", f"{label}: {d.reason}")
            self.store.add_action(sid, "escalate", "queued for human review", d.next_step)
            call_sid = session.get("call_sid")
            if call_sid and session.get("mode") == "live" and not meta.get("interrupted") \
                    and not meta.get("call_ended"):
                result = await self.twilio.redirect(call_sid, f"/twilio/outcome?session_id={sid}")
                self.store.update_meta(sid, interrupted=True)
                self.store.add_action(sid, "twilio_call_update", "done", result)
