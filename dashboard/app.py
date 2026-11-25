"""
VIGIL monitoring dashboard (Streamlit). Talks to the FastAPI backend over HTTP.

Run:  streamlit run dashboard/app.py
"""
import json
import os
from typing import Any, Dict, Optional

import altair as alt
import pandas as pd
import requests
import streamlit as st

API = os.getenv("VIGIL_API_URL", "http://127.0.0.1:8000").rstrip("/")

st.set_page_config(page_title="VIGIL", page_icon="", layout="wide")

COLORS = {"ALLOW": "#1f7a4d", "STEP_UP": "#a35f00", "HOLD": "#5b6b82", "ESCALATE": "#b42318"}
ICONS = {"ALLOW": "", "STEP_UP": "", "HOLD": "", "ESCALATE": ""}
_DOT = "<span style='display:inline-block;width:9px;height:9px;border-radius:50%;background:{c};margin-right:7px;'></span>"
VERDICT_ICON = {"real": _DOT.format(c="#1f7a4d"), "match": _DOT.format(c="#1f7a4d"),
                "fake": _DOT.format(c="#b42318"), "no_match": _DOT.format(c="#b42318"),
                "not_enrolled": _DOT.format(c="#94a3b8"), "inconclusive": _DOT.format(c="#a35f00"),
                "error": _DOT.format(c="#a35f00")}

st.markdown("""
<style>
/* ---- professional light theme: navy / slate, one blue accent ---- */
.stApp {background: #f5f7fa;}
section[data-testid="stSidebar"] {background: #0f1b2d;}
section[data-testid="stSidebar"] * {color: #e2e8f0 !important;}
section[data-testid="stSidebar"] code {background: #1c2b44 !important;}
section[data-testid="stSidebar"] button {background: #1c2b44 !important; border-color: #2c3e5c !important;}
.stApp, .stApp p, .stApp li, .stApp label, .stApp span, .stApp div[data-testid="stMarkdownContainer"] {color: #0f172a;}
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {color: #0f172a !important; font-weight: 650; letter-spacing: -0.01em;}
div[data-testid="stVerticalBlockBorderWrapper"] {background: #ffffff; border-radius: 8px;}
[data-testid="stTab"] p, button[data-baseweb="tab"] p {font-size: 15px; font-weight: 600; color: #475569 !important;}
[data-testid="stTab"][aria-selected="true"] p, button[data-baseweb="tab"][aria-selected="true"] p {color: #1d4ed8 !important;}
.react-aria-SelectionIndicator, .stTabs [data-baseweb="tab-highlight"] {background-color: #1d4ed8 !important;}
.vigil-title {text-align: left; margin: 4px 0 0 0; font-size: 26px; font-weight: 700; color: #0f172a;}
.vigil-subtitle {text-align: left; margin: 0 0 14px 0; font-size: 14px; color: #475569 !important;}
.kpi {background: #ffffff; border: 1px solid #e2e8f0; border-top: 3px solid #1d4ed8; border-radius: 6px;
      padding: 12px 16px; margin-bottom: 10px;}
.kpi .l {font-size: 11.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: #64748b !important;}
.kpi .v {font-size: 26px; font-weight: 700; color: #0f172a !important; line-height: 1.25; margin-top: 2px;}
.kpi .s {font-size: 12px; color: #64748b !important;}
.kpi.red {border-top-color: #b42318;} .kpi.green {border-top-color: #1f7a4d;} .kpi.amber {border-top-color: #a35f00;}
.vigil-banner {background: #ffffff; border: 1px solid #e2e8f0; border-left: 6px solid #5b6b82; border-radius: 6px;
               padding: 14px 18px; margin-bottom: 12px;}
.vigil-banner .k {font-size: 11.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: #64748b !important;}
.vigil-banner .dec {margin: 2px 0 4px 0; font-size: 26px; font-weight: 750; letter-spacing: .01em;}
.vigil-card .out {margin: 4px 0 4px 0; font-size: 19px; font-weight: 700;}
.vigil-banner p {margin: 2px 0 0 0; font-size: 14.5px; color: #1e293b !important;}
.vigil-card {background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 14px 16px; height: 100%;}
.vigil-card h4 {margin: 2px 0 6px 0; font-size: 17px;}
.vigil-muted {color: #64748b !important; font-size: 12.5px;}
.vigil-note {background: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid #1d4ed8; border-radius: 6px;
             padding: 10px 14px; margin: 8px 0 12px 0; font-size: 14px; color: #1e293b !important;}
.vigil-note.red {border-left-color: #b42318;}
button[kind="primary"] {background: #1d4ed8 !important; border-color: #1d4ed8 !important;}
button[kind="primary"]:hover {background: #1e40af !important; border-color: #1e40af !important;}
button[kind="primary"] p, button[kind="primary"] span {color: #ffffff !important; font-weight: 600;}
</style>
""", unsafe_allow_html=True)

DARK = "#0f172a"
GRID = "#eef2f6"
NAVY = "#2c4a7c"


def styled(chart, title: str, height: int = 230):
    """Template look: centred dark title, dark axis labels, light grid, no frame."""
    return (chart.properties(title=alt.TitleParams(title, anchor="start", color=DARK, fontSize=14,
                                                   fontWeight=600, offset=10), height=height)
            .configure_title(color=DARK, fontSize=14, fontWeight=600, anchor="start")
            .configure_axis(labelColor=DARK, titleColor=DARK, labelFontSize=12, titleFontSize=12,
                            gridColor=GRID, domainColor="#9ca3af", tickColor="#9ca3af")
            .configure_legend(labelColor=DARK, titleColor=DARK, labelFontSize=12)
            .configure_view(strokeWidth=0)
            .configure(background="#ffffff"))


# ------------------------------------------------------------------ backend helpers
def api(method: str, path: str, **kw) -> Optional[Dict[str, Any]]:
    try:
        r = requests.request(method, API + path, timeout=kw.pop("timeout", 150), **kw)
    except requests.RequestException:
        st.error(f"Cannot reach the VIGIL backend at {API}. Start it with: "
                 "`uvicorn vigil.api:app --reload --port 8000`")
        return None
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        st.error(f"{r.status_code}: {detail}")
        return None
    return r.json()


@st.cache_data(ttl=30)
def load_config():
    return api("GET", "/api/config")


def load_speakers():
    data = api("GET", "/api/speakers") or {}
    return data.get("speakers", []), data.get("remote_error")


cfg = load_config()
if not cfg:
    st.stop()
ACTIONS: Dict[str, Dict[str, Any]] = cfg["actions"]
SETTINGS = cfg["settings"]


def action_label(key: Optional[str]) -> str:
    if not key:
        return "—"
    a = ACTIONS.get(key, {})
    return f"{a.get('label', key)}  ·  {a.get('domain', '')}"


# ------------------------------------------------------------------ rendering
def decision_banner(d: Dict[str, Any]):
    dec = d["decision"]
    c = COLORS.get(dec, "#5b6b82")
    tier = f" &nbsp;·&nbsp; Risk: <b>{d['risk_tier']}</b>" if d.get("risk_tier") else ""
    st.markdown(
        f"<div class='vigil-banner' style='border-left-color:{c}'>"
        f"<div class='k'>VIGIL decision</div>"
        f"<div class='dec' style='color:{c}'>{dec.replace('_', ' ')}</div>"
        f"<p>{d['reason']}</p>"
        f"<p><b>Next step:</b> {d['next_step']}{tier} &nbsp;·&nbsp; "
        f"<span class='vigil-muted'>Rule {d['rule_id']}</span></p></div>",
        unsafe_allow_html=True)


def _single_model_outcome(ev: Optional[Dict[str, Any]], good: str, bad: str):
    """What a system using ONLY this one model would do with its verdict."""
    if ev is None:
        return "#94a3b8", "Waiting", "No result yet", "wait"
    v = ev["verdict"]
    if v == good:
        return "#1f7a4d", "Let in", f"Sees '{good.replace('_', ' ')}' only", "allow"
    if v == bad:
        return "#b42318", "Block", f"Sees '{bad.replace('_', ' ')}'", "block"
    return "#a35f00", "Undecided", v.replace("_", " ").capitalize(), "unknown"


def note(text: str, tone: str = ""):
    st.markdown(f"<div class='vigil-note {tone}'>{text}</div>", unsafe_allow_html=True)


def comparison_panel(view: Dict[str, Any]):
    """Why VIGIL is more than Aletheia or Eidos on their own."""
    state, d = view["state"], view["decision"]
    e_icon, e_out, e_why, e_kind = _single_model_outcome(state.get("identity"), "match", "no_match")
    a_icon, a_out, a_why, a_kind = _single_model_outcome(state.get("authenticity"), "real", "fake")
    v_out = d["decision"].replace("_", " ")
    v_sub = (f"{d['risk_tier']} risk · " if d.get("risk_tier") else "") + f"rule {d['rule_id']}"

    st.markdown("**One model alone vs VIGIL**")
    c1, c2, c3 = st.columns(3)
    for col, title, sub, icon, out, why in (
        (c1, "Eidos alone", "speaker check only", e_icon, e_out, e_why),
        (c2, "Aletheia alone", "deepfake check only", a_icon, a_out, a_why),
    ):
        col.markdown(f"<div class='vigil-card'><span class='vigil-muted'>{title} · {sub}</span>"
                     f"<div class='out' style='color:{icon}'>{out}</div><span class='vigil-muted'>{why}</span></div>",
                     unsafe_allow_html=True)
    c3.markdown(f"<div class='vigil-card' style='border:1.5px solid {COLORS.get(d['decision'], '#555')}'>"
                f"<span class='vigil-muted'>VIGIL · both models + request risk</span>"
                f"<div class='out' style='color:{COLORS.get(d['decision'], '#555')}'>{v_out}</div>"
                f"<span class='vigil-muted'>{v_sub}</span></div>",
                unsafe_allow_html=True)

    # Call out the moments where a single-model system would have gone wrong.
    vigil_lets_in = d["decision"] == "ALLOW"
    if e_kind == "allow" and not vigil_lets_in and a_kind == "block":
        note("<b>A speaker-verification-only system would have let this caller in.</b> The voice matches the "
             "customer, but Aletheia flagged it as AI-generated (a voice clone). VIGIL stopped it.", "red")
    elif a_kind == "allow" and not vigil_lets_in and e_kind == "block":
        note("<b>A deepfake-only system would have let this caller in.</b> It is a real human voice, but not "
             "the customer's. VIGIL stopped it.", "red")
    elif e_kind == "allow" and a_kind == "allow" and d["decision"] == "STEP_UP":
        note("Both models pass, but this is a high-risk request, so VIGIL asks for one more factor before "
             "acting. Neither model knows what the caller is asking for; VIGIL does.")


def evidence_cards(state: Dict[str, Any]):
    a, i = state.get("authenticity"), state.get("identity")
    c1, c2 = st.columns(2)
    with c1:
        if a:
            fin = "final" if a.get("final") else ("early" if a.get("early") else "live update")
            score = f"{a['score']:.2f}" if a.get("score") is not None else "—"
            speech = f"{a['speech_seconds']:.1f}s" if a.get("speech_seconds") is not None else "—"
            msg = f"<br><span class='vigil-muted'>{a['message']}</span>" if a.get("message") else ""
            body = (f"<b>{VERDICT_ICON.get(a['verdict'], '')} {a['verdict'].upper()}</b> ({fin})<br>"
                    f"Synthetic score: <b>{score}</b> <span class='vigil-muted'>(0 = human, 1 = AI)</span><br>"
                    f"Speech analysed: {speech}{msg}")
        else:
            body = "<span class='vigil-muted'>Waiting for a result…</span>"
        st.markdown(f"<div class='vigil-card'><h4>Aletheia · is the voice real?</h4>{body}</div>",
                    unsafe_allow_html=True)
    with c2:
        if i:
            sim = f"{i['score']:.2f}" if i.get("score") is not None else "—"
            msg = f"<br><span class='vigil-muted'>{i['message']}</span>" if i.get("message") else ""
            body = (f"<b>{VERDICT_ICON.get(i['verdict'], '')} {i['verdict'].replace('_', ' ').upper()}</b><br>"
                    f"Similarity: <b>{sim}</b> <span class='vigil-muted'>(-1..1, higher = more alike)</span>{msg}")
        else:
            body = "<span class='vigil-muted'>Waiting for a result…</span>"
        st.markdown(f"<div class='vigil-card'><h4>Eidos · is it the right person?</h4>{body}</div>",
                    unsafe_allow_html=True)


def segment_chart(state: Dict[str, Any]):
    a = state.get("authenticity") or {}
    segs = a.get("segments") or []
    if not segs:
        return
    df = pd.DataFrame(segs)
    df["window"] = df.apply(lambda r: f"{r['start']:.0f}–{r['end']:.0f}s", axis=1)
    df["verdict"] = df["verdict"].fillna("n/a")
    bars = alt.Chart(df).mark_bar().encode(
        x=alt.X("window:N", sort=None, title="Audio window"),
        y=alt.Y("score:Q", scale=alt.Scale(domain=[0, 1]), title="Synthetic score"),
        color=alt.Color("verdict:N", scale=alt.Scale(domain=["real", "fake", "n/a"],
                                                    range=["#1f7a4d", "#b42318", "#94a3b8"])),
        tooltip=["start", "end", "score", "verdict"])
    rule = alt.Chart(pd.DataFrame({"y": [0.5]})).mark_rule(strokeDash=[4, 4]).encode(y="y:Q")
    st.altair_chart(styled(bars + rule, "Where in the audio (flagged windows in red)"), width="stretch")


def score_timeline(events):
    rows = [{"speech seconds": e.get("speech_seconds") or e.get("duration_seconds") or 0,
             "synthetic score": e["score"], "stage": "final" if e.get("final") else ("early" if e.get("early") else "update")}
            for e in events if e["source"] == "aletheia" and e.get("score") is not None]
    if len(rows) < 2:
        return
    df = pd.DataFrame(rows)
    line = alt.Chart(df).mark_line(point=True).encode(
        x=alt.X("speech seconds:Q", title="Seconds of speech"),
        y=alt.Y("synthetic score:Q", scale=alt.Scale(domain=[0, 1]), title="Synthetic score"),
        tooltip=list(df.columns), color=alt.value(NAVY))
    rule = alt.Chart(pd.DataFrame({"y": [0.5]})).mark_rule(strokeDash=[4, 4], color="#b42318").encode(y="y:Q")
    st.altair_chart(styled(line + rule, "Deepfake score as the call goes on"), width="stretch")


def operator_controls(view: Dict[str, Any], key: str):
    s, d = view["session"], view["decision"]
    sid = s["id"]
    meta = s.get("meta") or {}
    if d["decision"] == "STEP_UP":
        with st.container(border=True):
            st.markdown("**Step-up verification**")
            if meta.get("otp"):
                st.caption(f"Demo: the one-time code 'sent' to the customer is **{meta['otp']}** "
                           "(a real deployment sends it by SMS).")
            c1, c2 = st.columns([3, 1])
            code = c1.text_input("Code entered by the customer", key=f"otp_{key}", max_chars=6)
            if c2.button("Verify code", key=f"otpbtn_{key}", width="stretch"):
                if api("POST", f"/api/sessions/{sid}/step-up", json={"code": code}):
                    st.rerun()
    if d["decision"] in ("ESCALATE", "HOLD"):
        with st.container(border=True):
            st.markdown("**Human review**")
            c1, c2 = st.columns(2)
            reviewer = c1.text_input("Reviewer", value="operator", key=f"rev_{key}")
            note = c2.text_input("Note", key=f"note_{key}")
            b1, b2 = st.columns(2)
            if b1.button("Approve request", key=f"ap_{key}", width="stretch"):
                if api("POST", f"/api/sessions/{sid}/review",
                       json={"approved": True, "reviewer": reviewer, "note": note}):
                    st.rerun()
            if b2.button("Deny request", key=f"dn_{key}", type="primary", width="stretch"):
                if api("POST", f"/api/sessions/{sid}/review",
                       json={"approved": False, "reviewer": reviewer, "note": note}):
                    st.rerun()


def session_details(view: Dict[str, Any]):
    s = view["session"]
    d = view["decision"]
    with st.expander("Why this decision", expanded=True):
        for line in d.get("trace", []):
            st.markdown(f"- {line}")
    with st.expander("Evidence timeline"):
        ev = pd.DataFrame([{
            "time": e.get("stored_at", "")[11:23], "source": e["source"], "verdict": e["verdict"],
            "final": e.get("final"), "early": e.get("early"), "score": e.get("score"),
            "speech_s": e.get("speech_seconds"), "origin": e.get("origin"), "message": e.get("message") or "",
            "ref": e.get("provider_ref") or ""} for e in view["events"]])
        st.dataframe(ev, hide_index=True, width="stretch")
    with st.expander("Actions & audit trail"):
        st.dataframe(pd.DataFrame([{"time": a["created_at"][11:23], "action": a["action"], "status": a["status"],
                                    "detail": a["detail"]} for a in view["actions"]]),
                     hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame([{"time": x["created_at"][11:23], "decision": x["decision"], "rule": x["rule_id"],
                                    "risk": x["risk_tier"], "reason": x["reason"]} for x in view["decisions"]]),
                     hide_index=True, width="stretch")
    with st.expander("Session record (JSON)"):
        st.json({"session": s, "state": view["state"]}, expanded=False)


def render_session(view: Dict[str, Any], key: str, live: bool = False):
    s = view["session"]
    meta = s.get("meta") or {}
    amount = f" · {s.get('currency', 'INR')} {s['amount']:,.0f}" if s.get("amount") else ""
    st.caption(f"Session `{s['id']}` · mode **{s['mode']}** · speaker **{s.get('speaker_id') or 'none'}** · "
               f"request **{view.get('action_label') or 'not chosen yet'}**{amount} · status {s['status']}")
    for w in meta.get("warnings") or []:
        st.warning(w)
    decision_banner(view["decision"])
    comparison_panel(view)
    evidence_cards(view["state"])
    if live:
        score_timeline(view["events"])
    segment_chart(view["state"])
    operator_controls(view, key)
    session_details(view)


# ------------------------------------------------------------------ overview (analytics)
RULE_GROUP = {
    "R0_review_approved": "Human review", "R0_review_denied": "Human review",
    "R1_synthetic_voice": "Synthetic voice", "R2_speaker_mismatch": "Wrong speaker",
    "R3_step_up_failed": "Step-up failed", "R4_awaiting_request": "Waiting for evidence",
    "R5_awaiting_authenticity": "Waiting for evidence", "R5_awaiting_final": "Waiting for evidence",
    "R5_awaiting_identity": "Waiting for evidence", "R6_authenticity_unavailable": "Check unavailable",
    "R7_weak_evidence": "Weak evidence", "R7_weak_evidence_critical": "Weak evidence",
    "R7_weak_evidence_step_up_passed": "Verified + step-up", "R8_verified_low_risk": "Verified",
    "R8_verified_high_risk": "Verified, high risk", "R8_verified_step_up_passed": "Verified + step-up",
}
GROUP_COLORS = {
    "Verified": "#1f7a4d", "Verified + step-up": "#5c9c7c", "Verified, high risk": "#2c4a7c",
    "Weak evidence": "#a35f00", "Synthetic voice": "#b42318", "Wrong speaker": "#7a2e2e",
    "Step-up failed": "#4a1d1d", "Human review": "#6b7fa6", "Waiting for evidence": "#cbd5e1",
    "Check unavailable": "#94a3b8",
}
DEMO_TRAFFIC = [
    ("genuine", "check_balance", None), ("genuine", "transfer_new_payee", 5000),
    ("genuine", "exam_check_in", None), ("genuine", "password_reset", None),
    ("genuine", "transfer_existing_payee", 2000), ("clone", "transfer_new_payee", 25000),
    ("clone", "mfa_reset", None), ("clone", "exam_check_in", None),
    ("impostor", "password_reset", None), ("impostor", "transfer_new_payee", 8000),
    ("short_call", "check_balance", None), ("short_call", "exam_reentry", None),
]


def overview_rows():
    sessions = (api("GET", "/api/sessions", params={"limit": 500}) or {}).get("sessions", [])
    decisions = (api("GET", "/api/decisions", params={"limit": 3000}) or {}).get("decisions", [])
    latest: Dict[str, Dict[str, Any]] = {}
    for d in sorted(decisions, key=lambda x: x["id"]):
        latest[d["session_id"]] = d
    rows = []
    for s in sessions:
        d = latest.get(s["id"])
        if not d:
            continue
        snap = d.get("evidence_snapshot") or {}
        a, i = snap.get("authenticity") or {}, snap.get("identity") or {}
        act = ACTIONS.get(s.get("action_type") or "", {})
        rows.append({
            "session": s["id"], "time": pd.to_datetime(s["created_at"]), "mode": s["mode"],
            "request": act.get("label", s.get("action_type") or "not chosen"),
            "domain": act.get("domain", "n/a"), "risk": d.get("risk_tier") or act.get("base_risk") or "n/a",
            "decision": d["decision"], "rule": d["rule_id"], "reason": RULE_GROUP.get(d["rule_id"], d["rule_id"]),
            "aletheia": a.get("verdict"), "synthetic_score": a.get("score"),
            "eidos": i.get("verdict"), "similarity": i.get("score"),
        })
    return pd.DataFrame(rows)


def kpi(col, value: str, label: str, sub: str = "", tone: str = ""):
    col.markdown(f"<div class='kpi {tone}'><div class='l'>{label}</div><div class='v'>{value}</div>"
                 f"<div class='s'>{sub}</div></div>", unsafe_allow_html=True)


def bar(df: pd.DataFrame, x: str, y: str, color: str, title: str, sort=None, height: int = 210, colors=None):
    enc_color = (alt.Color(f"{x}:N", scale=alt.Scale(domain=list(colors), range=list(colors.values())),
                           legend=None) if colors else alt.value(color))
    base = alt.Chart(df).encode(x=alt.X(f"{x}:N", sort=sort, title=None, axis=alt.Axis(labelAngle=0)),
                                y=alt.Y(f"{y}:Q", title=None))
    chart = base.mark_bar(size=30, color=color, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(color=enc_color, tooltip=[x, y]) + \
        base.mark_text(dy=-9, color=DARK, fontWeight="bold", fontSize=13).encode(text=f"{y}:Q")
    st.altair_chart(styled(chart, title, height), width="stretch")


def render_overview():
    st.markdown("<div class='vigil-title'>Command Center</div>"
                "<div class='vigil-subtitle'>Voice identity and authenticity decisions across all channels</div>", unsafe_allow_html=True)
    df = overview_rows()
    top = st.columns([4, 1])
    with top[1]:
        if st.button("Generate demo traffic", width="stretch",
                     help="Runs 12 simulated calls through the real pipeline. Uses no DetectifAI credits."):
            for scenario, action, amount in DEMO_TRAFFIC:
                api("POST", "/api/simulate/live", json={"scenario": scenario, "speaker_id": "demo-customer",
                                                         "action_type": action, "amount": amount, "speed": 4})
            st.toast("12 simulated calls started. Refresh in a few seconds.")
    if df.empty:
        st.info("No sessions yet. Analyse a recording, start a simulated call, or click **Generate demo traffic**.")
        return
    top[0].caption(f"{len(df)} sessions · last update {df['time'].max():%d %b %H:%M:%S} UTC")

    n = len(df)
    allowed = int((df["decision"] == "ALLOW").sum())
    escalated = int((df["decision"] == "ESCALATE").sum())
    fakes = int((df["aletheia"] == "fake").sum())
    clones = int(((df["aletheia"] == "fake") & (df["eidos"] == "match")).sum())
    impostors = int(((df["eidos"] == "no_match") & (df["aletheia"] == "real")).sum())
    step_ups = int((df["decision"] == "STEP_UP").sum())

    left, right = st.columns([1, 3.3], gap="medium")
    with left:
        kpi(st, f"{n:,}", "Sessions analysed", "Recorded + live")
        kpi(st, f"{fakes:,}", "Synthetic voices flagged", "Aletheia verdict: fake", "red")
        kpi(st, f"{clones:,}", "Clones that fooled Eidos", "Voice matched, but synthetic", "red")
        kpi(st, f"{impostors:,}", "Impostors stopped", "Real voice, wrong person", "red")
        kpi(st, f"{allowed / n:.0%}", "Allowed straight through", f"{allowed} of {n} sessions", "green")
        kpi(st, f"{step_ups:,}", "Step-ups requested", "Extra factor required", "amber")
        kpi(st, f"{escalated / n:.0%}", "Escalation rate", f"{escalated} sent to review")

    with right:
        r1 = st.columns(2)
        with r1[0]:
            t = df.set_index("time").resample("5min").size().reset_index(name="sessions")
            t["window"] = t["time"].dt.strftime("%H:%M")
            bar(t, "window", "sessions", NAVY, "Sessions over time (5-min windows)", sort=None)
        with r1[1]:
            dm = df["decision"].value_counts().reindex(["ALLOW", "STEP_UP", "HOLD", "ESCALATE"], fill_value=0)
            dm = dm.reset_index()
            dm.columns = ["decision", "sessions"]
            bar(dm, "decision", "sessions", NAVY, "Decision mix", sort=None, colors=COLORS)

        r2 = st.columns(3)
        with r2[0]:
            m = df["mode"].value_counts().reset_index()
            m.columns = ["mode", "sessions"]
            bar(m, "mode", "sessions", NAVY, "Sessions by input mode", height=190)
        with r2[1]:
            order = ["low", "medium", "high", "critical", "n/a"]
            rk = df["risk"].value_counts().reindex(order, fill_value=0).reset_index()
            rk.columns = ["risk", "sessions"]
            rk = rk[rk["sessions"] > 0]
            bar(rk, "risk", "sessions", NAVY, "Sessions by request risk", sort=order, height=190)
        with r2[2]:
            esc = df.assign(escalated=(df["decision"] == "ESCALATE").astype(int)) \
                .groupby("domain")["escalated"].sum().reset_index()
            bar(esc, "domain", "escalated", "#8a3b3b", "Escalations by domain", height=190)

        r3 = st.columns([1.15, 1])
        with r3[0]:
            reasons = df["reason"].value_counts().reset_index()
            reasons.columns = ["reason", "sessions"]
            present = [g for g in GROUP_COLORS if g in set(reasons["reason"])]
            donut = alt.Chart(reasons).mark_arc(innerRadius=58, outerRadius=95).encode(
                theta="sessions:Q",
                color=alt.Color("reason:N", title=None,
                                scale=alt.Scale(domain=present, range=[GROUP_COLORS[g] for g in present]),
                                legend=alt.Legend(orient="right", labelLimit=180)),
                tooltip=["reason", "sessions"]).properties(padding={"top": 14, "left": 5, "right": 5, "bottom": 5})
            st.altair_chart(styled(donut, "Why VIGIL decided", 240), width="stretch")
        with r3[1]:
            attacks = pd.DataFrame([
                {"system": "Eidos alone", "attacks let in": clones},
                {"system": "Aletheia alone", "attacks let in": impostors},
                {"system": "VIGIL", "attacks let in": int((((df["aletheia"] == "fake") | (df["eidos"] == "no_match"))
                                                            & (df["decision"] == "ALLOW")
                                                            & ~df["rule"].str.startswith("R0")).sum())},
            ])
            bar(attacks, "system", "attacks let in", "#d64545", "Attacks that would get through",
                colors={"Eidos alone": "#b42318", "Aletheia alone": "#c76b5f", "VIGIL": "#1f7a4d"}, height=230)

    with st.container(border=True):
        st.markdown("**Recent activity**")
        recent = df.sort_values("time", ascending=False).head(12).copy()
        recent["time"] = recent["time"].dt.strftime("%H:%M:%S")
        recent["synthetic_score"] = recent["synthetic_score"].round(2)
        recent["similarity"] = recent["similarity"].round(2)
        st.dataframe(recent[["time", "mode", "request", "risk", "aletheia", "synthetic_score", "eidos",
                             "similarity", "decision", "reason"]], hide_index=True, width="stretch")


# ------------------------------------------------------------------ layout
with st.sidebar:
    st.markdown("<div style='font-size:22px;font-weight:800;letter-spacing:.12em;'>VIGIL</div>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:12.5px;opacity:.75;margin-bottom:18px;'>Voice identity and authenticity "
                "decisions, powered by DetectifAI Aletheia and Eidos</div>", unsafe_allow_html=True)

    def status(label: str, value: str, ok: bool):
        dot = "#34d399" if ok else "#fbbf24"
        st.markdown(f"<div style='display:flex;justify-content:space-between;font-size:13px;padding:7px 0;"
                    f"border-bottom:1px solid #1f3150;'><span>{label}</span><span><span style='display:inline-block;"
                    f"width:8px;height:8px;border-radius:50%;background:{dot};margin-right:6px;'></span>{value}"
                    f"</span></div>", unsafe_allow_html=True)
    real = SETTINGS["provider"] != "mock"
    status("Provider", "DetectifAI" if real else "Mock (no key)", real)
    status("Live mode", SETTINGS["live_mode"], True)
    status("Twilio", "on" if SETTINGS["twilio_enabled"] else "off", SETTINGS["twilio_enabled"])
    st.markdown(f"<div style='font-size:12px;opacity:.7;margin:12px 0 10px 0;'>Backend {API}</div>",
                unsafe_allow_html=True)
    if st.button("Refresh config"):
        load_config.clear()
        st.rerun()

tab_over, tab_rec, tab_live, tab_dec, tab_spk, tab_sys = st.tabs(
    ["Overview", "Recorded analysis", "Live calls", "Decisions", "Speakers", "System"])

with tab_over:
    render_overview()

# ------------------------------------------------------------------ Mode A
with tab_rec:
    st.subheader("Mode A · Analyse a recording")
    left, right = st.columns([1, 2], gap="large")
    with left:
        speakers, remote_err = load_speakers()
        if remote_err:
            st.caption(f"Could not list DetectifAI speakers: {remote_err}")
        spk_opts = ["(none: deepfake check only)"] + [s["speaker_id"] for s in speakers] + ["Other…"]
        audio = st.file_uploader("1. Recording", type=["wav", "mp3", "flac", "ogg", "opus", "webm", "m4a", "aac",
                                                         "aiff"])
        if audio is not None:
            st.audio(audio)
        spk_choice = st.selectbox("2. Claimed speaker (enrolled ID)", spk_opts, index=1 if speakers else 0)
        speaker_id = None
        if spk_choice == "Other…":
            speaker_id = st.text_input("Speaker ID") or None
        elif not spk_choice.startswith("("):
            speaker_id = spk_choice
        action_type = st.selectbox("3. Requested action", list(ACTIONS), format_func=action_label,
                                   index=list(ACTIONS).index("transfer_new_payee") if "transfer_new_payee" in ACTIONS else 0)
        a = ACTIONS[action_type]
        amount = None
        if a.get("domain") == "banking" and action_type != "check_balance":
            amount = st.number_input("Amount (INR)", min_value=0.0, value=float(a.get("demo_amount", 5000)),
                                     step=1000.0)
        details = st.text_input("Details (payee, ticket, exam ID…)", placeholder="e.g. new payee: R. Sharma")
        risk = api("GET", "/api/risk", params={"action_type": action_type, "amount": amount} if amount is not None
                   else {"action_type": action_type})
        if risk:
            st.info(f"Risk tier for this request: **{risk['risk_tier']}**")
        go = st.button("Run VIGIL analysis", type="primary", width="stretch", disabled=audio is None)
        if go and audio is not None:
            with st.spinner("Sending to Aletheia + Eidos…"):
                data = {"action_type": action_type}
                if speaker_id:
                    data["speaker_id"] = speaker_id
                if amount is not None:
                    data["amount"] = str(amount)
                if details:
                    data["details"] = json.dumps({"note": details})
                res = api("POST", "/api/recorded/analyze", files={"file": (audio.name, audio.getvalue())}, data=data)
            if res:
                st.session_state["rec_session"] = res["session"]["id"]
    with right:
        sid = st.session_state.get("rec_session")
        if sid:
            view = api("GET", f"/api/sessions/{sid}")
            if view:
                render_session(view, key="rec")
                if st.button("Re-run analysis (no double charge)", key="rerun"):
                    api("POST", f"/api/sessions/{sid}/rerun")
                    st.rerun()
        else:
            st.markdown("#### How it works")
            st.markdown("Upload a recording, say who the caller **claims** to be and what they want to do. "
                        "VIGIL asks **Aletheia** whether the voice is real and **Eidos** whether it is the right "
                        "person, then decides **ALLOW / STEP UP / HOLD / ESCALATE** based on the risk of the "
                        "request.")
            if SETTINGS["provider"] == "mock":
                st.caption("Mock mode tip: file names decide the result. Try files named with 'synthetic', "
                           "'impostor' or 'short' (see demo_audio/).")
        recent = (api("GET", "/api/sessions", params={"mode": "recorded", "limit": 10}) or {}).get("sessions", [])
        if recent:
            with st.expander("Recent recorded analyses"):
                for r in recent:
                    c1, c2 = st.columns([4, 1])
                    c1.markdown(f"`{r['id']}` · {r.get('current_decision') or '—'} · "
                                f"{(r.get('details') or {}).get('filename', '')} · {r['created_at'][:19]}")
                    if c2.button("Open", key=f"open_{r['id']}"):
                        st.session_state["rec_session"] = r["id"]
                        st.rerun()

# ------------------------------------------------------------------ Mode B
with tab_live:
    st.subheader("Mode B · Live calls")
    c1, c2 = st.columns([1, 2], gap="large")
    with c1:
        with st.container(border=True):
            st.markdown("**Simulate a live call** (same pipeline as a real call)")
            scen = cfg["simulator_scenarios"]
            scenario = st.selectbox("Caller", list(scen), format_func=lambda k: scen[k])
            speakers, _ = load_speakers()
            sim_spk = st.selectbox("Claimed speaker", [s["speaker_id"] for s in speakers] or ["demo-customer"],
                                   key="sim_spk")
            sim_action = st.selectbox("Request", list(ACTIONS), format_func=action_label, key="sim_action",
                                      index=list(ACTIONS).index("transfer_new_payee") if "transfer_new_payee" in ACTIONS else 0)
            sim_amount = st.number_input("Amount", value=5000.0, step=1000.0, key="sim_amt")
            speed = st.slider("Speed", 0.5, 4.0, 1.0, 0.5)
            if st.button("Start simulated call", type="primary", width="stretch"):
                res = api("POST", "/api/simulate/live", json={"scenario": scenario, "speaker_id": sim_spk,
                                                               "action_type": sim_action, "amount": sim_amount,
                                                               "speed": speed})
                if res:
                    st.session_state["live_session"] = res["session_id"]
        with st.container(border=True):
            st.markdown("**Real phone calls (Twilio)**")
            st.markdown(f"Set your Twilio number's *A call comes in* webhook to:\n\n"
                        f"`{SETTINGS['public_base_url']}/twilio/voice` (HTTP POST)")
            if not SETTINGS["twilio_enabled"]:
                st.caption("Twilio credentials not set: calls still work, but mid-call redirects are skipped.")
            if SETTINGS["public_base_url"].startswith("http://127.") or "localhost" in SETTINGS["public_base_url"]:
                st.caption("PUBLIC_BASE_URL is local. Twilio needs a public https URL (e.g. ngrok).")
    with c2:
        sessions = [s for s in (api("GET", "/api/sessions", params={"limit": 50}) or {}).get("sessions", [])
                    if s["mode"] in ("live", "simulated")]
        if not sessions:
            st.info("No calls yet. Start a simulated call, or ring your Twilio number.")
        else:
            ids = [s["id"] for s in sessions]
            default = st.session_state.get("live_session")
            idx = ids.index(default) if default in ids else 0

            def fmt(i):
                s = next(x for x in sessions if x["id"] == i)
                who = (s.get("details") or {}).get("label") or (s.get("details") or {}).get("from") or ""
                return f"{'live · ' if s['status'] == 'open' else ''}{s['mode']} · {who} · {s['created_at'][11:19]} · {i}"
            chosen = st.selectbox("Call", ids, index=idx, format_func=fmt)
            st.session_state["live_session"] = chosen

            @st.fragment(run_every=1.5)
            def live_view():
                v = api("GET", f"/api/sessions/{st.session_state['live_session']}")
                if v:
                    render_session(v, key="live", live=True)
            live_view()

# ------------------------------------------------------------------ decisions
with tab_dec:
    st.subheader("All decisions")
    data = (api("GET", "/api/decisions", params={"limit": 500}) or {}).get("decisions", [])
    sessions = {s["id"]: s for s in (api("GET", "/api/sessions", params={"limit": 500}) or {}).get("sessions", [])}
    if data:
        latest = {}
        for d in sorted(data, key=lambda x: x["id"]):
            latest[d["session_id"]] = d
        cols = st.columns(4)
        for col, dec in zip(cols, ["ALLOW", "STEP_UP", "HOLD", "ESCALATE"]):
            col.metric(f"{ICONS[dec]} {dec.replace('_', ' ')}", sum(1 for d in latest.values() if d["decision"] == dec))
        st.caption("Counts use the latest decision per session.")
        df = pd.DataFrame([{
            "time": d["created_at"][:19].replace("T", " "), "session": d["session_id"],
            "mode": sessions.get(d["session_id"], {}).get("mode"),
            "request": sessions.get(d["session_id"], {}).get("action_type"),
            "decision": d["decision"], "rule": d["rule_id"], "risk": d["risk_tier"], "reason": d["reason"]}
            for d in data])
        f = st.multiselect("Filter decisions", ["ALLOW", "STEP_UP", "HOLD", "ESCALATE"])
        if f:
            df = df[df["decision"].isin(f)]
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("Download audit log (CSV)", df.to_csv(index=False), "vigil_decisions.csv")
    else:
        st.info("No decisions yet.")

# ------------------------------------------------------------------ speakers
with tab_spk:
    st.subheader("Enrolled speakers (Eidos voiceprints)")
    speakers, remote_err = load_speakers()
    if remote_err:
        st.warning(f"DetectifAI speaker list unavailable: {remote_err}")
    if speakers:
        st.dataframe(pd.DataFrame(speakers), hide_index=True, width="stretch")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        with st.container(border=True):
            st.markdown("**Enroll a voice** (15–60 s recording, 10+ s of speech)")
            st.caption("Only enroll your own voice or a teammate's, with consent.")
            ef = st.file_uploader("Enrollment recording", key="enroll_file")
            eid = st.text_input("Speaker ID", placeholder="e.g. bhagwandas")
            ename = st.text_input("Display name")
            ephone = st.text_input("Phone (E.164, e.g. +9198…): links live calls to this speaker")
            if st.button("Enroll", type="primary", disabled=not (ef and eid)):
                res = api("POST", "/api/speakers/enroll", files={"file": (ef.name, ef.getvalue())},
                          data={"speaker_id": eid, "display_name": ename, "phone": ephone})
                if res:
                    for w in res.get("warnings", []):
                        st.warning(w)
                    st.success(f"Enrolled {eid}")
    with c2:
        with st.container(border=True):
            st.markdown("**Link an already-enrolled speaker to a phone number**")
            lid = st.text_input("Speaker ID", key="link_id")
            lphone = st.text_input("Phone (E.164)", key="link_phone")
            if st.button("Save link", disabled=not lid):
                if api("POST", "/api/speakers/link", json={"speaker_id": lid, "phone": lphone or None}):
                    st.success("Saved")

# ------------------------------------------------------------------ system
with tab_sys:
    st.subheader("System")
    h = api("GET", "/health")
    if h:
        c1, c2 = st.columns(2)
        c1.markdown("**Configuration**")
        c1.json(h["config"])
        c2.markdown("**Provider health**")
        c2.json(h["provider"])
    st.markdown("**Raw provider events** (exactly what DetectifAI / Twilio / the simulator sent)")
    raw = (api("GET", "/api/raw-events", params={"limit": 100}) or {}).get("raw_events", [])
    if raw:
        st.dataframe(pd.DataFrame([{"time": r["created_at"][11:23], "channel": r["channel"],
                                    "session": r["session_id"], "note": r["note"],
                                    "payload": json.dumps(r["payload"])[:300]} for r in raw]),
                     hide_index=True, width="stretch")
    st.markdown("**Policy** (edit `config/policy.yaml`, then restart the backend)")
    st.dataframe(pd.DataFrame([{"action": k, "label": v.get("label"), "domain": v.get("domain"),
                                "base risk": v.get("base_risk"), "amount tiers": str(v.get("amount_tiers") or ""),
                                "IVR key": v.get("ivr_digit") or ""} for k, v in ACTIONS.items()]),
                 hide_index=True, width="stretch")
