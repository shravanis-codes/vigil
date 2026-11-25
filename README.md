# VIGIL

**A voice match alone can never clear a flagged voice.**

VIGIL combines DetectifAI's two models into one decision on every sensitive request:

- **Aletheia** asks: *is the voice real or AI-generated?*
- **Eidos** asks: *is it the enrolled person?*
- **Action risk** asks: *how bad is it if we're wrong?* (check balance vs. new payee vs. MFA reset vs. exam check-in)

The result is **ALLOW · STEP UP · HOLD · ESCALATE**. Every decision is saved with the evidence and the rule that produced it.

The two input modes share one evidence format and one policy engine:

| | Mode A: Recorded | Mode B: Live |
|---|---|---|
| Input | Uploaded audio file | Twilio phone call |
| DetectifAI | `POST /v1/speakers/verify` with `detect_deepfake=true` (both models in one call) | `wss://platform.detectif.ai/v1/stream?checks=deepfake,verify` |
| Results | Synchronous | Live: early verdict, updates, verification, completed |
| Acting on the decision | Dashboard + simulated action | Spoken outcome, OTP step-up, **Twilio Calls API redirect** when a fake is flagged mid-call |

---

## 1. Run it (5 minutes, no API keys needed)

```bash
cd vigil
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/make_demo_audio.py     # demo files for mock mode
```

Use two terminals, both with the venv activated:

```bash
# Terminal 1: backend
uvicorn vigil.api:app --reload --port 8000      # API docs: http://127.0.0.1:8000/docs

# Terminal 2: dashboard
streamlit run dashboard/app.py                  # opens http://localhost:8501
```

Without a `DETECTIF_API_KEY`, VIGIL runs in **mock mode**. The mock returns DetectifAI-shaped responses, chosen by file name:

| File name contains | Result |
|---|---|
| `synthetic`, `clone`, `fake`, `tts` | Aletheia says **fake** (flagged segments 4–10 s) |
| `impostor`, `other`, `wrong` | Eidos says **no_match** |
| `short` | Too little speech → **inconclusive** |
| anything else | Real voice and match |

Run the tests with `python -m pytest -q` (49 tests: policy, normalizers, API, webhook signatures, Twilio flow, relay, and the DetectifAI adapter against a fake server).

---

## 2. Demo script (3 minutes)

1. **Recorded, genuine:** upload `genuine_customer.wav` with speaker `demo-customer` and *Transfer to a new payee*. You get **STEP UP**, because the request is high-risk. Enter the code shown and the result becomes **ALLOW**, and the simulated transfer executes.
2. **Recorded, clone:** upload `synthetic_clone_of_customer.wav`. You get **ESCALATE**. Eidos says MATCH, but Aletheia flagged it, and the chart shows *where* in the audio. **This is the pitch.**
3. **Live, simulated:** on the Live calls tab, choose *Voice clone of the customer*. The decision starts at HOLD while evidence builds, then flips to **ESCALATE** mid-call when the first fake window arrives.
4. **Decisions tab:** the audit log, with the rule that fired for each decision. It can be downloaded as CSV.

---

## 3. Switch to real DetectifAI

```bash
cp .env.example .env      # set DETECTIF_API_KEY=...
```

Restart the backend. The sidebar should say **DetectifAI connected**, and the System tab should show `auth_ok: true` and your credits.

1. **Enroll** your own voice on the Speakers tab. The recording must be 15–60 s with at least 10 s of speech. Only enroll your own voice, or a teammate's with consent.
2. **Acceptance test:** upload a *new* genuine recording of yourself, then an **authorized** synthetic recording (a clone of *your own* voice, made with your consent). You should see two different, explained decisions.
3. If something looks off, the **System → Raw provider events** table shows exactly what DetectifAI returned.

---

## 4. Live calls with Twilio (Mode B)

1. Expose the backend: `ngrok http 8000`. Copy the `https://…` URL.
2. In `.env`, set `PUBLIC_BASE_URL=https://xxxx.ngrok-free.app`, `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`. Restart the backend.
3. In the Twilio console, open your phone number and set *A call comes in* to **Webhook**, `https://xxxx.ngrok-free.app/twilio/voice`, **HTTP POST**.
4. Link your phone number to your enrolled speaker (Speakers tab), or set `DEFAULT_LIVE_SPEAKER_ID`.
5. Call the number. Say your name, press a key for the request, and keep talking. Watch the Live calls tab.

**Call flow:** greeting (speech) → keypad menu → "tell us why" (more speech) → hold loop until the policy stops saying HOLD → spoken outcome. A STEP UP asks for the 6-digit code shown on the dashboard. An ESCALATE transfers to `HUMAN_AGENT_NUMBER`, or ends politely if none is set.

**Why the relay?** Twilio does not pass stream messages back. So by default (`VIGIL_LIVE_MODE=relay`), Twilio streams to VIGIL, which forwards the audio to DetectifAI (API key in a header, never in a URL) and reads the live verdicts directly. No webhook setup is needed. `VIGIL_LIVE_MODE=direct` streams Twilio straight to DetectifAI with a short-lived stream token, and results arrive through DetectifAI webhooks at `/webhooks/detectif` (signature-checked with `DETECTIF_WEBHOOK_SECRET`).

**Mid-call fallback:** if no identity result has arrived after 10 s of caller audio, VIGIL verifies the buffered audio through the REST API.

---

## 5. Architecture → code

| Blueprint layer | File |
|---|---|
| Mode A: Recorded | `vigil/api.py` → `POST /api/recorded/analyze` |
| Mode B: Live | `vigil/live/twilio_routes.py` (TwiML call flow), `vigil/live/relay.py` (media stream relay) |
| Voice Integration Layer | `vigil/providers/` (`base.py` interface, `detectif.py` real adapter, `mock.py`) |
| DetectifAI Intelligence | Aletheia + Eidos via the adapter |
| Evidence & Event Processor | `vigil/evidence.py`: normalize, validate, dedupe (`dedupe_key`), finality (`build_state`) |
| VIGIL Policy Engine | `vigil/policy.py` (rules) + `config/policy.yaml` (numbers) |
| ALLOW / STEP UP / HOLD / ESCALATE | `vigil/models.py` |
| Action Orchestration + Audit | `vigil/actions.py` (simulated actions, OTP, Twilio Calls API redirect), `vigil/store.py` (SQLite audit) |
| Shared pipeline | `vigil/pipeline.py`: every input mode goes through `Pipeline.ingest()` |
| Streamlit Monitoring Dashboard | `dashboard/app.py` |
| Live simulator | `vigil/live/simulator.py`: DetectifAI-format stream messages through the same pipeline |

### Policy rules (first match wins)

| Rule | When | Decision |
|---|---|---|
| R0 | A human reviewer approved or denied | ALLOW / ESCALATE |
| R1 | Aletheia says **fake** (even mid-call, even with an Eidos match or a passed step-up) | ESCALATE |
| R2 | Eidos says **no_match** | ESCALATE |
| R3 | Step-up failed (3 wrong codes) | ESCALATE |
| R4 | Live call, no request chosen yet | HOLD |
| R5 | Live call, evidence still arriving for a medium+ risk request | HOLD |
| R6 | Deepfake check could not run | HOLD |
| R7 | Weak evidence: inconclusive, not enrolled, or under 10 s of speech on a high-risk request | STEP UP (ESCALATE if critical; ALLOW after a passed step-up) |
| R8 | Real voice + match | ALLOW if low/medium risk; STEP UP if high/critical, then ALLOW after a passed step-up |

Risk per action, amount thresholds and the step-up/escalation wording per domain (banking, helpdesk, exam) are all in `config/policy.yaml`.

---

## 6. Engineering notes

- **Idempotency:** each upload's `Idempotency-Key` is built from the file hash and speaker. Retries and re-runs within 24 h are never charged twice. 429/5xx errors are retried with the same key.
- **Partial failures:** if verification fails (not enrolled, clip over 60 s), Aletheia still runs. A failed verify that returns `partial` keeps the deepfake result.
- **Webhooks:** Standard Webhooks HMAC verification with a 5-minute tolerance. Deduplicated, processed in the background so the reply stays under 10 s, and stored raw for debugging.
- **Twilio:** `X-Twilio-Signature` is validated when credentials are set. `PUBLIC_BASE_URL` must exactly match your ngrok URL.
- **Fields:** the adapter reads `verdict`, then falls back to `label` / `is_synthetic`, because the docs show both.

### Verify at the hackathon (not testable without keys)

- The exact JSON of `stream.*` webhook events. The parser is tolerant, and the raw events table shows what actually arrives.
- Whether the stream sends an identity result mid-call or only at the end. The mid-call REST fallback covers the second case.
- Whether relaying Twilio's `start`/`media` JSON frames to DetectifAI is accepted as-is (the docs say media messages are supported).
