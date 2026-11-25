"""
Mode B call flow (TwiML). Twilio calls these webhooks; VIGIL answers with TwiML.

  /twilio/voice    incoming call: create session, start the media stream, ask the caller to talk
  /twilio/menu     choose the request (keypad)
  /twilio/action   record the request, keep the caller talking while evidence builds
  /twilio/hold     poll the policy engine; loop while it says HOLD
  /twilio/outcome  speak the decision (also the target of mid-call REST redirects)
  /twilio/stepup   check the one-time code typed by the caller
  /twilio/status   call status callback (marks the call ended)
  /twilio/media    WebSocket: media stream relay (see relay.py)
"""
import logging
from typing import Dict, Optional
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response

from ..models import ALLOW, ESCALATE, HOLD, STEP_UP_DECISION, Evidence
from .relay import handle_twilio_media

log = logging.getLogger("vigil.twilio")


def _xml(body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                    media_type="application/xml")


def _say(text: str) -> str:
    return f'<Say voice="Polly.Aditi">{escape(text)}</Say>'


def _attr(v: str) -> str:
    return escape(v, {'"': "&quot;"})


def build_router(pipeline, settings) -> APIRouter:
    router = APIRouter(prefix="/twilio", tags=["live"])
    store, cfg = pipeline.store, pipeline.cfg

    async def form(request: Request) -> Dict[str, str]:
        """Parse Twilio's form body and check X-Twilio-Signature when Twilio is configured."""
        data = {k: str(v) for k, v in (await request.form()).items()}
        if settings.twilio_enabled and settings.twilio_validate_signature:
            from twilio.request_validator import RequestValidator
            url = settings.public_base_url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            sig = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(settings.twilio_auth_token).validate(url, data, sig):
                log.warning("Twilio signature check failed for %s. Is PUBLIC_BASE_URL exactly your ngrok URL?", url)
                raise HTTPException(403, "invalid Twilio signature")
        return data

    def path(p: str, session_id: str, **extra) -> str:
        return _attr(f"{p}?{urlencode(dict(session_id=session_id, **extra))}")

    def action_label(action_type: Optional[str]) -> str:
        return (cfg.actions.get(action_type or "", {}) or {}).get("label", action_type or "your request")

    # ------------------------------------------------------------------ incoming call
    @router.post("/voice")
    async def voice(request: Request):
        f = await form(request)
        caller, call_sid = f.get("From", ""), f.get("CallSid")
        speaker = store.speaker_by_phone(caller) if caller else None
        speaker_id = speaker["speaker_id"] if speaker else settings.default_live_speaker
        s = store.create_session("live", speaker_id=speaker_id, call_sid=call_sid,
                                 details={"from": caller}, meta={"live_mode": settings.live_mode})
        sid = s["id"]
        store.add_action(sid, "call_received", "ok", f"from {caller or 'unknown'}; speaker {speaker_id or 'none'}")
        if not speaker_id:
            await pipeline.ingest(sid, [Evidence(
                session_id=sid, source="eidos", verdict="not_enrolled", origin="live",
                message="Caller's number is not linked to an enrolled speaker", dedupe_key=f"{sid}:no_speaker")])

        if settings.live_mode == "direct" and settings.provider == "detectif":
            token = await pipeline.provider.create_stream_token()
            params = {"token": token, "checks": "deepfake,verify" if speaker_id else "deepfake",
                      "reference": sid}
            if speaker_id:
                params["speaker_id"] = speaker_id
            stream_url = settings.detectif_stream_url
        else:  # relay through VIGIL
            ws_base = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
            stream_url = f"{ws_base}/twilio/media"
            params = {"session_id": sid}
        param_xml = "".join(f'<Parameter name="{_attr(k)}" value="{_attr(str(v))}"/>' for k, v in params.items())

        return _xml(
            f'<Start><Stream url="{_attr(stream_url)}" track="inbound_track">{param_xml}</Stream></Start>'
            f'<Gather input="speech" action="{path("/twilio/menu", sid)}" speechTimeout="auto" timeout="6"'
            f' actionOnEmptyResult="true">'
            + _say("Welcome to the VIGIL secure line. To verify your voice, please say your full name "
                   "and the reason for your call.")
            + f'</Gather><Redirect method="POST">{path("/twilio/menu", sid)}</Redirect>')

    # ------------------------------------------------------------------ choose request
    @router.post("/menu")
    async def menu(request: Request, session_id: str):
        await form(request)
        options = [(a.get("ivr_digit"), a.get("label")) for a in cfg.actions.values() if a.get("ivr_digit")]
        options.sort()
        prompt = " ".join(f"Press {d} to {label.lower()}." for d, label in options)
        return _xml(
            f'<Gather input="dtmf" numDigits="1" action="{path("/twilio/action", session_id)}" timeout="8">'
            + _say(prompt) + f'</Gather><Redirect method="POST">{path("/twilio/menu", session_id)}</Redirect>')

    @router.post("/action")
    async def action(request: Request, session_id: str):
        f = await form(request)
        digit = f.get("Digits", "")
        match = [(k, a) for k, a in cfg.actions.items() if a.get("ivr_digit") == digit]
        if not match:
            return _xml(_say("Sorry, that is not a valid option.")
                        + f'<Redirect method="POST">{path("/twilio/menu", session_id)}</Redirect>')
        action_type, a = match[0]
        store.add_action(session_id, "caller_request", "received", f"{action_type} (pressed {digit})")
        await pipeline.set_action(session_id, action_type, a.get("demo_amount"))
        return _xml(
            f'<Gather input="speech" action="{path("/twilio/hold", session_id, n=0)}" speechTimeout="auto"'
            f' timeout="6" actionOnEmptyResult="true">'
            + _say(f"You asked to {action_label(action_type).lower()}. While we verify your voice, please tell us "
                   "in a sentence or two why you need this today.")
            + f'</Gather><Redirect method="POST">{path("/twilio/hold", session_id, n=0)}</Redirect>')

    # ------------------------------------------------------------------ wait for evidence
    @router.post("/hold")
    async def hold(request: Request, session_id: str, n: int = 0):
        await form(request)
        d = await pipeline.redecide_if_changed(session_id)
        if d.decision != HOLD:
            return _xml(await outcome_twiml(session_id))
        if n >= settings.hold_max_loops:
            store.add_action(session_id, "hold_timeout", "escalated",
                             f"No conclusive evidence after {n} checks; handing to a human")
            return _xml(_say("We could not complete voice verification in time.") + escalate_twiml(session_id))
        return _xml(
            f'<Gather input="speech" action="{path("/twilio/hold", session_id, n=n + 1)}" speechTimeout="auto"'
            f' timeout="4" actionOnEmptyResult="true">'
            + _say("Still verifying. Please keep talking: tell us your city and the last thing you bought.")
            + f'</Gather><Redirect method="POST">{path("/twilio/hold", session_id, n=n + 1)}</Redirect>')

    # ------------------------------------------------------------------ decisions -> speech
    def escalate_twiml(session_id: str) -> str:
        s = store.get_session(session_id)
        team = cfg.domain_of(s.get("action_type")).get("escalate_to", "specialist team")
        if settings.human_agent_number:
            return (_say(f"Connecting you to our {team}.")
                    + f"<Dial>{escape(settings.human_agent_number)}</Dial>")
        return _say(f"Your request has been paused and passed to our {team}, who will contact you. Goodbye.") \
            + "<Hangup/>"

    async def outcome_twiml(session_id: str) -> str:
        d = pipeline.evaluate(session_id)
        s = store.get_session(session_id)
        label = action_label(s.get("action_type"))
        if d.decision == ALLOW:
            return _say(f"Thank you. Your identity is verified and your request to {label.lower()} "
                        "has been completed. Goodbye.") + "<Hangup/>"
        if d.decision == STEP_UP_DECISION:
            return (f'<Gather input="dtmf" numDigits="6" action="{path("/twilio/stepup", session_id)}" timeout="20">'
                    + _say("For your security, please enter the six digit code we just sent to your "
                           "registered phone.")
                    + f'</Gather><Redirect method="POST">{path("/twilio/outcome", session_id)}</Redirect>')
        if d.decision == ESCALATE:
            return _say("We could not verify this request.") + escalate_twiml(session_id)
        return f'<Redirect method="POST">{path("/twilio/hold", session_id, n=0)}</Redirect>'

    @router.post("/outcome")
    async def outcome(request: Request, session_id: str):
        await form(request)
        await pipeline.redecide_if_changed(session_id)
        return _xml(await outcome_twiml(session_id))

    @router.post("/stepup")
    async def stepup(request: Request, session_id: str):
        f = await form(request)
        d = await pipeline.submit_step_up(session_id, f.get("Digits", ""), origin="caller")
        prefix = ""
        if d.decision == STEP_UP_DECISION:
            prefix = _say("That code was not correct.")
        return _xml(prefix + await outcome_twiml(session_id))

    @router.post("/status")
    async def status(request: Request):
        f = await form(request)
        s = store.get_session_by_call(f.get("CallSid", ""))
        if s:
            store.add_raw("twilio", {"CallStatus": f.get("CallStatus")}, s["id"], note="status callback")
            if f.get("CallStatus") in ("completed", "failed", "busy", "no-answer", "canceled"):
                store.update_meta(s["id"], call_ended=True)
                store.update_session(s["id"], status="closed")
        return Response(status_code=204)

    @router.websocket("/media")
    async def media(ws: WebSocket):
        await handle_twilio_media(ws, pipeline, settings)

    return router
