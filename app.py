# app.py — SCAT6 intake tool for practitioners.
#
# Same OAuth2 flow as before (apps.csipacific.ca via dash-auth-external) and the
# same Juvonno branch → group → athlete cascade, but the app is now a SCAT6
# (Sport Concussion Assessment Tool 6) intake form. Completed assessments are
# stored locally (SQLite) and pushed to the athlete's Juvonno documents as a
# formatted PDF plus an appendable per-athlete SCAT6 history CSV.
import io, os, base64, traceback
from datetime import date

import requests
import pandas as pd
from dash_auth_external import DashAuthExternal
from dash import (Dash, Input, Output, State, html, dcc, dash_table,
                  no_update, ALL, ctx)
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

import scat6 as S
import scat6_store as store
from scat6_pdf import build_scat6_pdf
import juvonno_api as juv

# ───────────────────────── Constants ─────────────────────────
BASE_ROOT_URL = APP_URL  # login entry point (same value the old app hardcoded)

# ───────────────────────── Auth / Server ─────────────────────────
auth = DashAuthExternal(
    AUTH_URL, TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

here = os.path.dirname(os.path.abspath(__file__))
server.static_folder = os.path.join(here, "assets")
server.static_url_path = "/assets"

# ───────────────────────── Signed-in name helpers (unchanged) ─────────────────────────
def _b64url_decode(part: str) -> bytes:
    part = part + '=' * (-len(part) % 4)
    return base64.urlsafe_b64decode(part.encode("utf-8"))

def _name_from_jwt(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = _b64url_decode(parts[1]).decode("utf-8")
        js = __import__("json").loads(payload)
        first = (js.get("given_name") or js.get("first_name") or "").strip()
        last  = (js.get("family_name") or js.get("last_name") or "").strip()
        name  = (f"{first} {last}").strip() or js.get("name") or ""
        if not name:
            name = js.get("preferred_username") or js.get("email") or ""
        return name
    except Exception:
        return ""

def _get_signed_in_name() -> str:
    try:
        token = auth.get_token()
        if not token:
            return ""
        try:
            r = requests.get(f"{SITE_URL}/api/csiauth/me/",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             timeout=5)
            if r.status_code == 200:
                js = r.json()
                name = f"{(js.get('first_name') or '').strip()} {(js.get('last_name') or '').strip()}".strip() \
                       or js.get("email", "")
                if name:
                    return name
        except Exception:
            pass
        try:
            r2 = requests.get(f"{SITE_URL}/api/csiauth/me/", params={"access_token": token}, timeout=5)
            if r2.status_code == 200:
                js = r2.json()
                name = f"{(js.get('first_name') or '').strip()} {(js.get('last_name') or '').strip()}".strip() \
                       or js.get("email", "")
                if name:
                    return name
        except Exception:
            pass
        return _name_from_jwt(token) or ""
    except Exception:
        return ""

# ───────────────────────── Juvonno push helpers ─────────────────────────
def _csv_name(cid: int) -> str:
    return f"SCAT6_History_{int(cid)}.csv"

def _pdf_name(assessment: dict) -> str:
    d = (assessment.get("date_of_examination") or "nodate").replace("/", "-")
    return f"SCAT6_{d}_athlete{assessment.get('athlete_id', '')}.pdf"

def build_history_csv_bytes(cid: int) -> bytes:
    """CSV of all locally-stored assessments for the athlete."""
    rows = []
    for meta in store.list_assessments(int(cid)):
        rec = store.get_assessment(meta["id"])
        if rec:
            rows.append(S.to_flat_record(rec["assessment"], rec["scores"]))
    df = pd.DataFrame(rows, columns=S.CSV_COLUMNS)
    return df.to_csv(index=False).encode("utf-8")

def push_to_juvonno(assessment: dict, scores: dict, upload_pdf: bool, update_csv: bool) -> list:
    """Upload PDF and/or pull-append-reupload the history CSV. The two steps are
    independent — a failure in one is reported but does not block the other.
    Returns status strings; raises only if every requested step failed."""
    msgs, errors = [], []
    cid = int(assessment["athlete_id"])
    exam_date = assessment.get("date_of_examination") or date.today().isoformat()

    if upload_pdf:
        try:
            pdf_bytes = build_scat6_pdf(assessment, scores)
            name = _pdf_name(assessment)
            juv.upload_customer_document(
                cid, pdf_bytes, name,
                description=f"SCAT6 assessment ({assessment.get('assessment_type', '')}) — {exam_date}",
                date=exam_date)
            msgs.append(f"PDF uploaded to Juvonno as {name}")
        except Exception as e:
            traceback.print_exc()
            errors.append(f"PDF upload failed: {e}")

    if update_csv:
        try:
            csv_name = _csv_name(cid)
            new_row = S.to_flat_record(assessment, scores)
            existing = juv.find_document_by_name(cid, csv_name)
            df = pd.DataFrame(columns=S.CSV_COLUMNS)
            if existing is not None:
                try:
                    _, raw = juv.download_customer_document(cid, int(existing.get("id")))
                    df = pd.read_csv(io.BytesIO(raw))
                except Exception as e:
                    msgs.append(f"Could not read existing history CSV ({e}); "
                                f"rebuilding from local records")
            if df.empty:
                # Seed with every local assessment for this athlete
                # (includes the new one if already saved)
                try:
                    df = pd.read_csv(io.BytesIO(build_history_csv_bytes(cid)))
                except Exception:
                    df = pd.DataFrame(columns=S.CSV_COLUMNS)
            # Append if this assessment_id isn't already present
            aid = new_row.get("assessment_id")
            already = (aid and "assessment_id" in df.columns
                       and (df["assessment_id"].astype(str) == str(aid)).any())
            if not already:
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df = df.reindex(columns=S.CSV_COLUMNS)
            juv.upload_customer_document(
                cid, df.to_csv(index=False).encode("utf-8"), csv_name,
                description="SCAT6 longitudinal history (auto-appended)", date=exam_date)
            msgs.append(f"History CSV updated in Juvonno "
                        f"({len(df)} assessment(s) in {csv_name})")
        except Exception as e:
            traceback.print_exc()
            errors.append(f"History CSV update failed: {e}")

    if errors and not msgs:
        raise RuntimeError("; ".join(errors))
    msgs.extend(errors)  # partial failure: surface alongside successes
    return msgs

# ───────────────────────── Small UI builders ─────────────────────────
def yn(id_, options=("Y", "N"), inline=True, value=None):
    return dbc.RadioItems(id=id_, inline=inline, value=value,
                          options=[{"label": o, "value": o} for o in options])

def num_input(id_, placeholder="", minv=0, maxv=None, step=1, width="110px"):
    return dbc.Input(id=id_, type="number", min=minv, max=maxv, step=step,
                     placeholder=placeholder, style={"maxWidth": width})

def section(title, children, color="#2b3a67", optional=False):
    hdr = [html.Span(title)]
    if optional:
        hdr.append(dbc.Badge("Optional", color="warning", text_color="dark", className="ms-2"))
    return dbc.Card([
        dbc.CardHeader(hdr, style={"background": color, "color": "white", "fontWeight": 600}),
        dbc.CardBody(children),
    ], className="mb-3")

def _word_grid(list_key: str, comp_type: str, trials: int):
    """Checkbox grid: words × trials (immediate memory) or words × 1 (delayed recall)."""
    words = S.WORD_LISTS.get(list_key or "A", S.WORD_LISTS["A"])
    header = [html.Th("Word")] + [html.Th(f"Trial {t+1}", className="text-center")
                                  for t in range(trials)]
    rows = []
    for wi, w in enumerate(words):
        cells = [html.Td(w, style={"fontWeight": 500})]
        for t in range(trials):
            cells.append(html.Td(
                dbc.Checkbox(id={"type": comp_type, "index": f"{t}-{wi}"}, value=False),
                className="text-center"))
        rows.append(html.Tr(cells))
    return dbc.Table([html.Thead(html.Tr(header)), html.Tbody(rows)],
                     bordered=False, size="sm", striped=True,
                     style={"maxWidth": "480px"})

def _digit_rows(list_key: str):
    pairs = S.DIGIT_LISTS.get(list_key or "A", S.DIGIT_LISTS["A"])
    rows = []
    for i, (s1, s2) in enumerate(pairs):
        rows.append(html.Tr([
            html.Td(html.Div([html.Div(s1, style={"fontFamily": "monospace"}),
                              html.Div(s2, style={"fontFamily": "monospace", "color": "#888"})])),
            html.Td(dbc.RadioItems(
                id={"type": "f-dig", "index": i}, inline=True,
                options=[{"label": "0", "value": 0}, {"label": "1", "value": 1}], value=None),
                className="text-center", style={"verticalAlign": "middle"}),
        ]))
    return dbc.Table(
        [html.Thead(html.Tr([html.Th("String (alternate below)"),
                             html.Th("Score", className="text-center")])),
         html.Tbody(rows)],
        bordered=False, size="sm", striped=True, style={"maxWidth": "480px"})

# ───────────────────────── Form layout ─────────────────────────
def athlete_picker():
    return section("Select Athlete (from Juvonno)", [
        dbc.Row([
            dbc.Col([html.Label("Branch", className="fw-bold"),
                     dcc.Dropdown(id="branch-dd", options=juv.BRANCH_OPTS,
                                  placeholder="Select a branch…", clearable=True)], md=4),
            dbc.Col([html.Label("Group", className="fw-bold"),
                     dcc.Dropdown(id="group-dd", placeholder="Filter by group (optional)…",
                                  clearable=True, disabled=True)], md=4),
            dbc.Col([html.Label("Athlete", className="fw-bold"),
                     dcc.Loading(dcc.Dropdown(id="athlete-dd",
                                              placeholder="Select a branch first…",
                                              clearable=True, disabled=True), type="circle")], md=4),
        ], className="g-2"),
        html.Div(id="cascade-status", className="mt-2 text-muted small"),
        html.Div(id="athlete-header", className="mt-2"),
    ])

def form_layout():
    sym_rows = []
    for i, symptom in enumerate(S.SYMPTOMS):
        sym_rows.append(html.Tr([
            html.Td(symptom, style={"width": "40%"}),
            html.Td(dbc.RadioItems(
                id={"type": "f-sym", "index": i}, inline=True, value=0,
                options=[{"label": str(v), "value": v} for v in range(0, 7)])),
        ]))

    return html.Div([
        # ── Athlete info / header ──
        section("Athlete Information", [
            dbc.Row([
                dbc.Col([html.Label("Athlete name"), dbc.Input(id="f-name")], md=4),
                dbc.Col([html.Label("ID number (Juvonno)"), dbc.Input(id="f-idnum", disabled=True)], md=2),
                dbc.Col([html.Label("Date of birth"), dbc.Input(id="f-dob", placeholder="YYYY-MM-DD")], md=3),
                dbc.Col([html.Label("Sex"), dbc.Select(id="f-sex", options=[
                    {"label": v, "value": v} for v in ("Male", "Female", "Prefer Not To Say", "Other")])], md=3),
            ], className="g-2 mb-2"),
            dbc.Row([
                dbc.Col([html.Label("Date of examination"),
                         html.Br(),
                         dcc.DatePickerSingle(id="f-exam-date", display_format="YYYY-MM-DD",
                                              date=date.today())], md=3),
                dbc.Col([html.Label("Assessment type"),
                         dbc.RadioItems(id="f-assess-type", inline=True, value="post_injury",
                                        options=[{"label": "Baseline", "value": "baseline"},
                                                 {"label": "Suspected/Post-injury", "value": "post_injury"}])], md=4),
                dbc.Col([html.Label("Date of injury"), dbc.Input(id="f-injury-date", placeholder="YYYY-MM-DD")], md=2),
                dbc.Col([html.Label("Time of injury"), dbc.Input(id="f-injury-time", placeholder="HH:MM")], md=1),
                dbc.Col([html.Label("Dominant hand"),
                         dbc.Select(id="f-hand", options=[{"label": v, "value": v}
                                                          for v in ("Left", "Right", "Ambidextrous")])], md=2),
            ], className="g-2 mb-2"),
            dbc.Row([
                dbc.Col([html.Label("Sport / Team / School"), dbc.Input(id="f-sport")], md=6),
                dbc.Col([html.Label("Time since injury"),
                         dbc.Input(id="f-time-since", placeholder="e.g. 45 mins / 2 days")], md=3),
            ], className="g-2"),
        ]),

        # ── Concussion history ──
        section("Concussion History", [
            dbc.Row([
                dbc.Col([html.Label("Diagnosed concussions in the past"),
                         num_input("f-num-conc", maxv=99)], md=3),
                dbc.Col([html.Label("Most recent concussion"),
                         dbc.Input(id="f-recent-conc", placeholder="YYYY-MM-DD or description")], md=3),
                dbc.Col([html.Label("Recovery time (days)"), num_input("f-recovery-days", maxv=9999)], md=3),
                dbc.Col([html.Label("Primary symptoms"), dbc.Input(id="f-primary-symptoms")], md=3),
            ], className="g-2"),
        ]),

        # ── Red flags ──
        section("Red Flags (Box 1) — any red flag → remove from play, urgent medical assessment", [
            dbc.Checklist(id="f-redflags", options=[{"label": f, "value": i}
                                                    for i, f in enumerate(S.RED_FLAGS)], value=[]),
        ], color="#8f1f2f"),

        # ── Immediate assessment ──
        section("Immediate Assessment — Step 1: Observable Signs", [
            dbc.Checklist(id="f-obs-context", inline=True, value=[],
                          options=[{"label": "Witnessed", "value": "witnessed"},
                                   {"label": "Observed on Video", "value": "video"}],
                          className="mb-2"),
            dbc.Table(html.Tbody([
                html.Tr([html.Td(sign, style={"width": "70%"}),
                         html.Td(yn({"type": "f-obs", "index": i}))])
                for i, sign in enumerate(S.OBSERVABLE_SIGNS)
            ]), size="sm", striped=True),
        ]),

        section("Immediate Assessment — Step 2: Glasgow Coma Scale", [
            dbc.Row([
                dbc.Col([html.Label("Best Eye Response (E)"),
                         dbc.Select(id="f-gcs-e", options=[
                             {"label": f"{v} — {lbl}", "value": v} for lbl, v in S.GCS_EYE])], md=4),
                dbc.Col([html.Label("Best Verbal Response (V)"),
                         dbc.Select(id="f-gcs-v", options=[
                             {"label": f"{v} — {lbl}", "value": v} for lbl, v in S.GCS_VERBAL])], md=4),
                dbc.Col([html.Label("Best Motor Response (M)"),
                         dbc.Select(id="f-gcs-m", options=[
                             {"label": f"{v} — {lbl}", "value": v} for lbl, v in S.GCS_MOTOR])], md=4),
            ], className="g-2"),
            html.Div(id="gcs-total-display", className="mt-2 fw-bold"),
        ]),

        section("Immediate Assessment — Step 3: Cervical Spine", [
            html.P("In a patient who is not lucid or fully conscious, a cervical spine injury "
                   "should be assumed and spinal precautions taken.", className="text-muted small"),
            dbc.Table(html.Tbody([
                html.Tr([html.Td(q, style={"width": "70%"}),
                         html.Td(yn({"type": "f-cerv", "index": i}))])
                for i, q in enumerate(S.CERVICAL_ITEMS)
            ]), size="sm", striped=True),
        ]),

        section("Immediate Assessment — Step 4: Coordination & Ocular/Motor Screen", [
            dbc.Table(html.Tbody([
                html.Tr([html.Td(q, style={"width": "70%"}),
                         html.Td(yn({"type": "f-coord", "index": i}))])
                for i, q in enumerate(S.COORD_OCULAR_ITEMS)
            ]), size="sm", striped=True),
            html.Label("If extraocular movements abnormal, describe:"),
            dbc.Input(id="f-ocular-desc"),
        ]),

        section("Immediate Assessment — Step 5: Maddocks Questions", [
            html.P('"I am going to ask you a few questions, please listen carefully and give '
                   'your best effort. First, tell me what happened?"',
                   className="fst-italic text-primary small"),
            dbc.Table(html.Tbody([
                html.Tr([html.Td(q, style={"width": "70%"}),
                         html.Td(dbc.RadioItems(id={"type": "f-mad", "index": i}, inline=True,
                                                options=[{"label": "0", "value": 0},
                                                         {"label": "1", "value": 1}], value=None))])
                for i, q in enumerate(S.MADDOCKS_QUESTIONS)
            ]), size="sm", striped=True),
        ]),

        # ── Off-field ──
        section("Off-Field — Step 1: Athlete Background", [
            dbc.Table(html.Tbody([
                html.Tr([html.Td(label, style={"width": "70%"}),
                         html.Td(yn({"type": "f-bg", "index": key}))])
                for key, label in S.BACKGROUND_ITEMS
            ]), size="sm", striped=True),
            dbc.Row([
                dbc.Col([html.Label("Notes"), dbc.Textarea(id="f-bg-notes", style={"height": "70px"})], md=6),
                dbc.Col([html.Label("Current medications"),
                         dbc.Textarea(id="f-medications", style={"height": "70px"})], md=6),
            ], className="g-2"),
        ]),

        section("Off-Field — Step 2: Symptom Evaluation (athlete self-report, 0–6)", [
            html.P("Baseline: rate how you typically feel. Post-injury: rate how you feel now.",
                   className="text-muted small"),
            dbc.Table([html.Thead(html.Tr([html.Th("Symptom"),
                                           html.Th("Rating (0 = none, 6 = severe)")])),
                       html.Tbody(sym_rows)], size="sm", striped=True),
            dbc.Row([
                dbc.Col([html.Label("Symptoms worse with physical activity?"), yn("f-worse-phys")], md=4),
                dbc.Col([html.Label("Symptoms worse with mental activity?"), yn("f-worse-ment")], md=4),
                dbc.Col([html.Label(S.PERCENT_NORMAL_QUESTION),
                         num_input("f-pct-normal", maxv=100, width="90px")], md=4),
            ], className="g-2"),
            html.Label("If not 100%, why?"),
            dbc.Textarea(id="f-pct-why", style={"height": "60px"}),
        ]),

        section("Off-Field — Step 3: Cognitive Screening — Orientation", [
            dbc.Table(html.Tbody([
                html.Tr([html.Td(q, style={"width": "70%"}),
                         html.Td(dbc.RadioItems(id={"type": "f-orient", "index": i}, inline=True,
                                                options=[{"label": "0", "value": 0},
                                                         {"label": "1", "value": 1}], value=None))])
                for i, q in enumerate(S.ORIENTATION_QUESTIONS)
            ]), size="sm", striped=True),
        ]),

        section("Off-Field — Step 3: Immediate Memory (3 trials, 1 word/second)", [
            dbc.Row([
                dbc.Col([html.Label("Word list"),
                         dbc.RadioItems(id="f-wordlist", inline=True, value="A",
                                        options=[{"label": k, "value": k} for k in ("A", "B", "C")])], md=4),
                dbc.Col([html.Label("Time last trial completed"),
                         dbc.Input(id="f-im-time", placeholder="HH:MM")], md=3),
            ], className="g-2 mb-2"),
            html.Div(id="im-grid"),
        ]),

        section("Off-Field — Step 3: Concentration — Digits Backward & Months in Reverse", [
            dbc.Row([
                dbc.Col([html.Label("Digit list"),
                         dbc.RadioItems(id="f-digitlist", inline=True, value="A",
                                        options=[{"label": k, "value": k} for k in ("A", "B", "C")])], md=4),
            ], className="mb-2"),
            html.Div(id="dig-rows"),
            html.Hr(),
            html.P('"Now tell me the months of the year in reverse order as QUICKLY and as '
                   'accurately as possible. Start with the last month and go backward: '
                   'December, November… go ahead."', className="fst-italic text-primary small"),
            html.P(" — ".join(S.MONTHS_REVERSED), className="small text-muted"),
            dbc.Row([
                dbc.Col([html.Label("Time to complete (secs)"),
                         num_input("f-months-secs", step=0.1, maxv=999)], md=3),
                dbc.Col([html.Label("Number of errors"), num_input("f-months-errs", maxv=12)], md=3),
                dbc.Col(html.Div(id="months-score-display", className="fw-bold mt-4"), md=4),
            ], className="g-2"),
        ]),

        section("Off-Field — Step 4: Balance — Modified BESS "
                "(errors, 20 s each; test the non-dominant foot)", [
            dbc.Row([
                dbc.Col([html.Label("Foot tested"),
                         dbc.RadioItems(id="f-foot", inline=True,
                                        options=[{"label": v, "value": v}
                                                 for v in ("Left", "Right")])], md=3),
                dbc.Col([html.Label("Testing surface"), dbc.Input(id="f-surface")], md=4),
                dbc.Col([html.Label("Footwear"), dbc.Input(id="f-footwear")], md=4),
            ], className="g-2 mb-2"),
            dbc.Row([
                dbc.Col([html.Label(f"{lbl} (of 10)"), num_input(f"f-bess-{key}", maxv=10)], md=3)
                for key, lbl in S.MBESS_STANCES
            ], className="g-2"),
            html.Div(id="bess-total-display", className="fw-bold mt-2"),
            html.Hr(),
            dbc.Checklist(id="f-foam-toggle", switch=True, value=[],
                          options=[{"label": "Add optional foam-surface mBESS", "value": "on"}]),
            dbc.Collapse(dbc.Row([
                dbc.Col([html.Label(f"Foam — {lbl} (of 10)"), num_input(f"f-foam-{key}", maxv=10)], md=3)
                for key, lbl in S.MBESS_STANCES
            ], className="g-2 mt-1"), id="foam-collapse", is_open=False),
        ]),

        section("Off-Field — Step 4: Timed Tandem Gait (3 m line, heel-to-toe, 3 trials)", [
            dbc.Row([
                dbc.Col([html.Label(f"Trial {t} (secs)"),
                         num_input(f"f-tg-{t}", step=0.01, maxv=999)], md=3)
                for t in (1, 2, 3)
            ], className="g-2"),
            html.Div(id="tg-display", className="fw-bold mt-2"),
            html.Hr(),
            html.Label("Dual Task Gait (optional — serial 7s while walking)"),
            dbc.Row([
                dbc.Col([html.Label(f"Trial {t} time (secs)"),
                         num_input(f"f-dual-{t}", step=0.01, maxv=999)], md=3)
                for t in (1, 2, 3)
            ] + [dbc.Col([html.Label("Counting errors (total)"),
                          num_input("f-dual-errs", maxv=99)], md=3)], className="g-2"),
            dbc.Row([
                dbc.Col([html.Label("Any trials not completed? Why?"),
                         dbc.Input(id="f-tg-incomplete")], md=8),
            ], className="g-2 mt-1"),
        ]),

        section("Off-Field — Step 5: Delayed Recall (≥ 5 min after Immediate Memory)", [
            dbc.Row([dbc.Col([html.Label("Time started"),
                              dbc.Input(id="f-dr-time", placeholder="HH:MM")], md=3)],
                    className="mb-2"),
            html.Div(id="dr-grid"),
        ]),

        section("Step 6: Decision & Attestation", [
            dbc.Row([
                dbc.Col([html.Label("Neurological exam (acute evaluation)"),
                         dbc.RadioItems(id="f-neuro", inline=True,
                                        options=[{"label": v, "value": v}
                                                 for v in ("Normal", "Abnormal")])], md=4),
                dbc.Col([html.Label("Different from usual self?"),
                         dbc.RadioItems(id="f-different", inline=True,
                                        options=[{"label": v, "value": v}
                                                 for v in ("Yes", "No", "Not applicable")])], md=4),
                dbc.Col([html.Label("Concussion diagnosed?"),
                         dbc.RadioItems(id="f-diagnosed", inline=True,
                                        options=[{"label": v, "value": v}
                                                 for v in ("Yes", "No", "Deferred")])], md=4),
            ], className="g-2 mb-2"),
            html.Label("Additional clinical notes"),
            dbc.Textarea(id="f-notes", style={"height": "110px"}),
            html.Hr(),
            html.P("I am an HCP and I have personally administered or supervised the "
                   "administration of this SCAT6.", className="fst-italic small"),
            dbc.Row([
                dbc.Col([html.Label("Examiner (HCP) name"), dbc.Input(id="f-examiner")], md=4),
                dbc.Col([html.Label("Title / speciality"), dbc.Input(id="f-examiner-title")], md=4),
                dbc.Col([html.Label("Registration / license #"),
                         dbc.Input(id="f-examiner-license")], md=4),
            ], className="g-2"),
        ]),

        # ── Live score summary + save ──
        dbc.Card([
            dbc.CardHeader("Score Summary & Save",
                           style={"background": "#1d5b3c", "color": "white", "fontWeight": 600}),
            dbc.CardBody([
                html.Div(id="score-summary"),
                html.Hr(),
                dbc.Checklist(id="f-push-opts", inline=True, value=["pdf", "csv"],
                              options=[{"label": " Upload PDF to Juvonno", "value": "pdf"},
                                       {"label": " Update SCAT6 history CSV in Juvonno",
                                        "value": "csv"}]),
                dbc.Button("Save Assessment", id="btn-save", color="success",
                           size="lg", className="mt-2"),
                dcc.Loading(html.Div(id="save-status", className="mt-2"), type="circle"),
            ]),
        ], className="mb-4"),
    ])

def history_layout():
    return html.Div([
        section("Saved SCAT6 Assessments (local)", [
            dash_table.DataTable(
                id="hist-table",
                columns=[{"name": n, "id": i} for n, i in (
                    ("ID", "id"), ("Date", "date_of_examination"), ("Type", "assessment_type"),
                    ("Examiner", "examiner"), ("Symptoms", "symptom_number"),
                    ("Severity", "symptom_severity"), ("Cognitive", "cognitive_total"),
                    ("mBESS", "mbess_total"), ("Diagnosed", "concussion_diagnosed"),
                    ("Saved at (UTC)", "created_at"))],
                data=[], page_action="none",
                style_table={"overflowX": "auto", "maxHeight": "300px", "overflowY": "auto"},
                style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
                style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left"},
            ),
            dbc.Row([
                dbc.Col(dcc.Dropdown(id="hist-assess-dd",
                                     placeholder="Pick an assessment for PDF / re-push…"), md=5),
                dbc.Col(dbc.Button("Download PDF", id="btn-hist-pdf", color="primary"),
                        width="auto"),
                dbc.Col(dbc.Button("Download history CSV", id="btn-hist-csv", color="secondary"),
                        width="auto"),
                dbc.Col(dbc.Button("Push to Juvonno", id="btn-hist-push", color="success"),
                        width="auto"),
            ], className="g-2 mt-2 align-items-center"),
            dcc.Loading(html.Div(id="hist-status", className="mt-2"), type="circle"),
        ]),
        section("Serial Comparison (SCAT6 Step 6 domains across assessments)", [
            html.Div(id="hist-compare"),
        ]),
        section("Athlete Documents in Juvonno (pull)", [
            dbc.Button("Refresh document list", id="btn-docs-refresh", color="secondary",
                       className="mb-2"),
            dcc.Loading(html.Div(id="docs-table-wrap"), type="circle"),
            dbc.Row([
                dbc.Col(dcc.Dropdown(id="docs-dd", placeholder="Pick a document to download…"),
                        md=6),
                dbc.Col(dbc.Button("Download document", id="btn-docs-dl", color="primary"),
                        width="auto"),
            ], className="g-2 align-items-center"),
            html.Div(id="docs-status", className="mt-2"),
        ]),
    ])

# ───────────────────────── App shell ─────────────────────────
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[dbc.themes.BOOTSTRAP,
                          "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"],
    suppress_callback_exceptions=True,
)
app.title = "SCAT6 Intake — CSI Pacific"

TAB_STYLE = {"padding": "8px 14px", "border": "1px solid #e9ecef", "borderRadius": "8px",
             "background": "#f8f9fb", "color": "#495057", "fontWeight": "500",
             "flex": "1 1 0%", "textAlign": "center"}
TAB_SELECTED_STYLE = {**TAB_STYLE, "border": "1px solid #cfe2ff", "background": "#e7f1ff",
                      "color": "#084298", "fontWeight": "600"}

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

    dbc.Container([
        html.H3("SCAT6™ Intake Tool", className="mt-2"),
        html.P("Sport Concussion Assessment Tool 6 — for use by Health Care Professionals. "
               "Athletes are loaded from Juvonno; completed assessments are saved locally and "
               "pushed to the athlete's Juvonno documents.", className="text-muted small"),
        athlete_picker(),
        dcc.Tabs(id="main-tabs", value="tab-form", children=[
            dcc.Tab(label="New SCAT6", value="tab-form",
                    style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
            dcc.Tab(label="History & Documents", value="tab-history",
                    style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
        ], style={"display": "flex", "gap": "6px", "width": "100%"},
            parent_style={"width": "100%"}, mobile_breakpoint=0),

        # Both panes stay mounted so form state survives tab switches.
        html.Div(form_layout(), id="pane-form", className="mt-3"),
        html.Div(history_layout(), id="pane-history", className="mt-3",
                 style={"display": "none"}),
    ], fluid=True),

    # Stores & downloads
    dcc.Store(id="demo-store", data={}),
    dcc.Store(id="form-collect", data={}),
    dcc.Store(id="history-refresh", data=0),
    dcc.Download(id="dl-pdf"),
    dcc.Download(id="dl-csv"),
    dcc.Download(id="dl-doc"),

    Footer().render(),
])

# ───────────────────────── Auth callbacks (unchanged pattern) ─────────────────────────
@app.callback(Output("redirect-to", "href"),
              Input("init-interval", "n_intervals"),
              State("redirect-to", "pathname"))
def initial_view(n, pathname):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    return no_update if token else BASE_ROOT_URL

@app.callback(Output("navbar-user", "children"), Input("user-refresh", "n_intervals"))
def refresh_user_badge(_n):
    try:
        name = _get_signed_in_name()
        return f"Signed in as: {name}" if name else html.A("Sign in", href=BASE_ROOT_URL,
                                                           className="link-light")
    except Exception:
        return html.A("Sign in", href=BASE_ROOT_URL, className="link-light")

@app.callback(Output("redirect-to", "href", allow_duplicate=True),
              Input("user-refresh", "n_intervals"), prevent_initial_call=True)
def enforce_session(_n):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    return no_update if token else BASE_ROOT_URL

# ───────────────────────── Tab visibility ─────────────────────────
@app.callback(Output("pane-form", "style"), Output("pane-history", "style"),
              Input("main-tabs", "value"))
def toggle_panes(which):
    if which == "tab-history":
        return {"display": "none"}, {"display": "block"}
    return {"display": "block"}, {"display": "none"}

# ───────────────────────── Athlete cascade ─────────────────────────
@app.callback(
    Output("group-dd", "options"), Output("group-dd", "disabled"), Output("group-dd", "value"),
    Output("athlete-dd", "options"), Output("athlete-dd", "disabled"), Output("athlete-dd", "value"),
    Output("cascade-status", "children"),
    Input("branch-dd", "value"), Input("group-dd", "value"),
    prevent_initial_call=True)
def cascade(branch_id, group_filter):
    if not branch_id:
        return [], True, None, [], True, None, "Select a branch to load athletes."
    try:
        athletes = juv.get_athletes_for_branch(int(branch_id))
    except Exception as e:
        traceback.print_exc()
        return [], True, None, [], True, None, f"Error: {e}"

    all_groups = sorted({g for a in athletes for g in (a.get("groups") or [])})
    group_opts = [{"label": g.title(), "value": g} for g in all_groups]

    if ctx.triggered_id == "branch-dd":
        group_filter = None
    filtered = [a for a in athletes
                if not group_filter or group_filter in (a.get("groups") or [])]
    ath_opts = [{"label": a["label"], "value": a["id"]} for a in filtered]

    msg = f"{len(athletes)} athlete(s) in this branch"
    if group_filter:
        msg += f" — {len(filtered)} in group “{group_filter}”"
    return (group_opts, not bool(group_opts), group_filter,
            ath_opts, not bool(ath_opts),
            no_update if ctx.triggered_id == "group-dd" else None,
            msg + ".")

@app.callback(
    Output("demo-store", "data"), Output("athlete-header", "children"),
    Output("f-name", "value"), Output("f-idnum", "value"),
    Output("f-dob", "value"), Output("f-sex", "value"),
    Output("f-examiner", "value"),
    Output("history-refresh", "data", allow_duplicate=True),
    Input("athlete-dd", "value"),
    State("history-refresh", "data"),
    prevent_initial_call=True)
def on_athlete_selected(athlete_id, hist_tick):
    if not athlete_id:
        return {}, "", "", "", "", None, no_update, (hist_tick or 0) + 1
    demo = juv.athlete_demographics(int(athlete_id))
    sex_map = {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}
    sex = sex_map.get(str(demo.get("sex", "")).strip().lower(), None)
    header = dbc.Alert([
        html.B(demo.get("name") or f"Athlete {athlete_id}"),
        html.Span(f"  •  Juvonno ID {demo['id']}"),
        html.Span(f"  •  DOB {demo['dob']}" if demo.get("dob") else ""),
        html.Span(f"  •  Chart {demo['chart_number']}" if demo.get("chart_number") else ""),
    ], color="info", className="py-2 mb-0")
    examiner = _get_signed_in_name() or no_update
    return (demo, header, demo.get("name", ""), str(demo["id"]),
            demo.get("dob", ""), sex, examiner, (hist_tick or 0) + 1)

# ───────────────────────── Dynamic grids ─────────────────────────
@app.callback(Output("im-grid", "children"), Output("dr-grid", "children"),
              Input("f-wordlist", "value"))
def render_word_grids(list_key):
    return (_word_grid(list_key, "f-im", trials=3),
            _word_grid(list_key, "f-dr", trials=1))

@app.callback(Output("dig-rows", "children"), Input("f-digitlist", "value"))
def render_digit_rows(list_key):
    return _digit_rows(list_key)

@app.callback(Output("foam-collapse", "is_open"), Input("f-foam-toggle", "value"))
def toggle_foam(val):
    return "on" in (val or [])

# ───────────────────────── Live scoring ─────────────────────────
def _idx_map(entries):
    """ctx list-of-pattern entries → {index: value}."""
    return {e["id"]["index"]: e.get("value") for e in (entries or [])}

def _collect_to_assessment(collect: dict) -> dict:
    """Convert the indexed form-collect store into the raw assessment dict
    format understood by scat6.compute_all_scores / to_flat_record."""
    c = collect or {}

    def g(dct, key):
        return dct.get(key, dct.get(str(key)))

    sym = c.get("sym", {}) or {}
    obs = c.get("obs", {}) or {}
    cerv = c.get("cerv", {}) or {}
    coord = c.get("coord", {}) or {}
    mad = c.get("mad", {}) or {}
    orient = c.get("orient", {}) or {}
    im = c.get("im", {}) or {}
    dig = c.get("dig", {}) or {}
    dr = c.get("dr", {}) or {}
    gcs = c.get("gcs", [None, None, None])
    months = c.get("months", [None, None])
    mbess = c.get("mbess", [None, None, None])
    foam = c.get("foam", [None, None, None])

    cerv_vals = [g(cerv, i) for i in range(len(S.CERVICAL_ITEMS))]
    coord_vals = [g(coord, i) for i in range(len(S.COORD_OCULAR_ITEMS))]

    def _summary_yn(vals):
        if any(v == "N" for v in vals):
            return "N"
        if vals and all(v == "Y" for v in vals):
            return "Y"
        return ""

    return {
        "symptom_ratings": [g(sym, i) or 0 for i in range(len(S.SYMPTOMS))],
        "observable_signs": [g(obs, i) for i in range(len(S.OBSERVABLE_SIGNS))],
        "cervical_items": cerv_vals,
        "coord_ocular_items": coord_vals,
        "cervical_normal": _summary_yn(cerv_vals),
        "coord_ocular_normal": _summary_yn(coord_vals),
        "maddocks_answers": [g(mad, i) for i in range(len(S.MADDOCKS_QUESTIONS))],
        "orientation_answers": [g(orient, i) for i in range(len(S.ORIENTATION_QUESTIONS))],
        "im_trials": [[1 if im.get(f"{t}-{w}") else 0 for w in range(10)] for t in range(3)],
        "digit_rows": [g(dig, i) for i in range(4)],
        "dr_hits": [1 if dr.get(f"0-{w}") else 0 for w in range(10)],
        "gcs_e": gcs[0], "gcs_v": gcs[1], "gcs_m": gcs[2],
        "months_seconds": months[0], "months_errors": months[1],
        "mbess_double": mbess[0], "mbess_tandem": mbess[1], "mbess_single": mbess[2],
        "mbess_foam_double": foam[0], "mbess_foam_tandem": foam[1], "mbess_foam_single": foam[2],
        "tg_times": c.get("tg", []),
        "dual_task_times": c.get("dual", []),
    }

@app.callback(
    Output("form-collect", "data"),
    Output("score-summary", "children"),
    Output("gcs-total-display", "children"),
    Output("months-score-display", "children"),
    Output("bess-total-display", "children"),
    Output("tg-display", "children"),
    Input({"type": "f-sym", "index": ALL}, "value"),
    Input({"type": "f-obs", "index": ALL}, "value"),
    Input({"type": "f-cerv", "index": ALL}, "value"),
    Input({"type": "f-coord", "index": ALL}, "value"),
    Input({"type": "f-mad", "index": ALL}, "value"),
    Input({"type": "f-orient", "index": ALL}, "value"),
    Input({"type": "f-im", "index": ALL}, "value"),
    Input({"type": "f-dig", "index": ALL}, "value"),
    Input({"type": "f-dr", "index": ALL}, "value"),
    Input("f-gcs-e", "value"), Input("f-gcs-v", "value"), Input("f-gcs-m", "value"),
    Input("f-months-secs", "value"), Input("f-months-errs", "value"),
    Input("f-bess-double", "value"), Input("f-bess-tandem", "value"),
    Input("f-bess-single", "value"),
    Input("f-foam-double", "value"), Input("f-foam-tandem", "value"),
    Input("f-foam-single", "value"),
    Input("f-tg-1", "value"), Input("f-tg-2", "value"), Input("f-tg-3", "value"),
    Input("f-dual-1", "value"), Input("f-dual-2", "value"), Input("f-dual-3", "value"),
)
def live_scores(*_):
    inputs = ctx.inputs_list
    pat = {}
    simple = {}
    for group in inputs:
        if isinstance(group, list):
            if group and isinstance(group[0].get("id"), dict):
                pat[group[0]["id"]["type"]] = _idx_map(group)
            # empty pattern lists are fine — leave as missing
        elif isinstance(group, dict):
            simple[group["id"]] = group.get("value")

    def to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    collect = {
        "sym": pat.get("f-sym", {}), "obs": pat.get("f-obs", {}),
        "cerv": pat.get("f-cerv", {}), "coord": pat.get("f-coord", {}),
        "mad": pat.get("f-mad", {}), "orient": pat.get("f-orient", {}),
        "im": pat.get("f-im", {}), "dig": pat.get("f-dig", {}), "dr": pat.get("f-dr", {}),
        "gcs": [to_int(simple.get("f-gcs-e")), to_int(simple.get("f-gcs-v")),
                to_int(simple.get("f-gcs-m"))],
        "months": [simple.get("f-months-secs"), simple.get("f-months-errs")],
        "mbess": [simple.get("f-bess-double"), simple.get("f-bess-tandem"),
                  simple.get("f-bess-single")],
        "foam": [simple.get("f-foam-double"), simple.get("f-foam-tandem"),
                 simple.get("f-foam-single")],
        "tg": [simple.get("f-tg-1"), simple.get("f-tg-2"), simple.get("f-tg-3")],
        "dual": [simple.get("f-dual-1"), simple.get("f-dual-2"), simple.get("f-dual-3")],
    }

    a = _collect_to_assessment(collect)
    s = S.compute_all_scores(a)

    def badge(label, val, maxv=None):
        txt = "—" if val is None else (f"{val} / {maxv}" if maxv else str(val))
        return dbc.Col(dbc.Card(dbc.CardBody([
            html.Div(label, className="text-muted small"),
            html.Div(txt, className="fs-5 fw-bold"),
        ], className="py-2"), className="text-center"), md=2, xs=4)

    summary = dbc.Row([
        badge("Symptoms", s["symptom_number"], 22),
        badge("Severity", s["symptom_severity"], 132),
        badge("Orientation", s["orientation"], 5),
        badge("Imm. memory", s["immediate_memory"], 30),
        badge("Concentration", s["concentration"], 5),
        badge("Delayed recall", s["delayed_recall"], 10),
        badge("Cognitive total", s["cognitive_total"], 50),
        badge("mBESS errors", s["mbess_total"], 30),
        badge("Tandem fastest", s["tg_fastest"]),
        badge("Maddocks", s["maddocks"], 5),
        badge("GCS", s["gcs_total"], 15),
    ], className="g-2")

    gcs_txt = f"GCS total: {s['gcs_total']} / 15" if s["gcs_total"] is not None else ""
    months_txt = f"Months score: {s['months_score']} / 1"
    bess_txt = (f"mBESS total errors: {s['mbess_total']} / 30"
                if s["mbess_total"] is not None else "")
    tg_txt = (f"Average: {s['tg_average']} s — Fastest: {s['tg_fastest']} s"
              if s["tg_fastest"] is not None else "")
    return collect, summary, gcs_txt, months_txt, bess_txt, tg_txt

# ───────────────────────── Save assessment ─────────────────────────
@app.callback(
    Output("save-status", "children"),
    Output("dl-pdf", "data"),
    Output("history-refresh", "data", allow_duplicate=True),
    Input("btn-save", "n_clicks"),
    State("form-collect", "data"),
    State("athlete-dd", "value"), State("demo-store", "data"),
    State("f-name", "value"), State("f-dob", "value"), State("f-sex", "value"),
    State("f-exam-date", "date"), State("f-assess-type", "value"),
    State("f-injury-date", "value"), State("f-injury-time", "value"),
    State("f-time-since", "value"),
    State("f-hand", "value"), State("f-sport", "value"),
    State("f-num-conc", "value"), State("f-recent-conc", "value"),
    State("f-recovery-days", "value"), State("f-primary-symptoms", "value"),
    State("f-redflags", "value"), State("f-obs-context", "value"),
    State("f-ocular-desc", "value"),
    State({"type": "f-bg", "index": ALL}, "value"),
    State("f-bg-notes", "value"), State("f-medications", "value"),
    State("f-worse-phys", "value"), State("f-worse-ment", "value"),
    State("f-pct-normal", "value"), State("f-pct-why", "value"),
    State("f-wordlist", "value"), State("f-im-time", "value"),
    State("f-digitlist", "value"),
    State("f-foot", "value"), State("f-surface", "value"), State("f-footwear", "value"),
    State("f-tg-incomplete", "value"), State("f-dual-errs", "value"),
    State("f-dr-time", "value"),
    State("f-neuro", "value"), State("f-different", "value"), State("f-diagnosed", "value"),
    State("f-notes", "value"),
    State("f-examiner", "value"), State("f-examiner-title", "value"),
    State("f-examiner-license", "value"),
    State("f-push-opts", "value"),
    State("history-refresh", "data"),
    prevent_initial_call=True)
def save_assessment(n_clicks, collect, athlete_id, demo,
                    name, dob, sex, exam_date, assess_type,
                    injury_date, injury_time, time_since, hand, sport,
                    num_conc, recent_conc, recovery_days, primary_symptoms,
                    redflags, obs_context, ocular_desc,
                    _bg_vals, bg_notes, medications,
                    worse_phys, worse_ment, pct_normal, pct_why,
                    wordlist, im_time, digitlist,
                    foot, surface, footwear, tg_incomplete, dual_errs, dr_time,
                    neuro, different, diagnosed, notes,
                    examiner, examiner_title, examiner_license,
                    push_opts, hist_tick):
    if not n_clicks:
        raise PreventUpdate
    if not athlete_id:
        return (dbc.Alert("Select an athlete from Juvonno before saving.", color="danger"),
                no_update, no_update)

    # background pattern-state values by key (from ctx to keep index association)
    bg_map = {}
    for grp in ctx.states_list:
        if isinstance(grp, list) and grp and isinstance(grp[0].get("id"), dict) \
                and grp[0]["id"].get("type") == "f-bg":
            bg_map = {e["id"]["index"]: e.get("value") for e in grp}
            break

    a = _collect_to_assessment(collect or {})
    redflag_bools = [i in set(redflags or []) for i in range(len(S.RED_FLAGS))]
    a.update({
        "athlete_id": int(athlete_id),
        "athlete_name": (name or (demo or {}).get("name") or f"Athlete {athlete_id}").strip(),
        "dob": dob or "", "sex": sex or "",
        "date_of_examination": (exam_date or date.today().isoformat())[:10],
        "assessment_type": assess_type or "",
        "date_of_injury": injury_date or "", "time_of_injury": injury_time or "",
        "time_since_injury": time_since or "",
        "dominant_hand": hand or "", "sport_team": sport or "",
        "num_past_concussions": num_conc, "most_recent_concussion": recent_conc or "",
        "recovery_days": recovery_days, "primary_symptoms": primary_symptoms or "",
        "red_flags": redflag_bools,
        "obs_context": obs_context or [], "ocular_description": ocular_desc or "",
        "background": bg_map, "background_notes": bg_notes or "",
        "medications": medications or "",
        "worse_physical": worse_phys or "", "worse_mental": worse_ment or "",
        "percent_normal": pct_normal, "percent_normal_why": pct_why or "",
        "word_list": wordlist or "A", "im_time_completed": im_time or "",
        "digit_list": digitlist or "A",
        "foot_tested": foot or "", "test_surface": surface or "", "footwear": footwear or "",
        "tg_incomplete_reason": tg_incomplete or "", "dual_task_errors": dual_errs,
        "dr_time_started": dr_time or "",
        "neuro_exam": neuro or "", "different_from_usual": different or "",
        "concussion_diagnosed": diagnosed or "",
        "notes": notes or "",
        "examiner": examiner or _get_signed_in_name() or "",
        "examiner_title": examiner_title or "", "examiner_license": examiner_license or "",
    })

    scores = S.compute_all_scores(a)

    msgs = []
    try:
        new_id = store.save_assessment(a, scores)
        a["assessment_id"] = new_id
        msgs.append(f"Assessment #{new_id} saved locally.")
    except Exception as e:
        traceback.print_exc()
        return dbc.Alert(f"Local save failed: {e}", color="danger"), no_update, no_update

    push_opts = push_opts or []
    push_errors = []
    try:
        msgs += push_to_juvonno(a, scores, "pdf" in push_opts, "csv" in push_opts)
    except Exception as e:
        traceback.print_exc()
        push_errors.append(f"Juvonno push failed: {e}")

    pdf_bytes = build_scat6_pdf(a, scores)
    dl = dcc.send_bytes(lambda b: b.write(pdf_bytes), _pdf_name(a))

    alerts = [dbc.Alert([html.Div(m) for m in msgs], color="success")]
    if push_errors:
        alerts.append(dbc.Alert([html.Div(m) for m in push_errors], color="warning"))
    return html.Div(alerts), dl, (hist_tick or 0) + 1

# ───────────────────────── History tab ─────────────────────────
@app.callback(
    Output("hist-table", "data"), Output("hist-assess-dd", "options"),
    Output("hist-compare", "children"),
    Input("history-refresh", "data"), Input("athlete-dd", "value"))
def refresh_history(_tick, athlete_id):
    if not athlete_id:
        return [], [], html.Div("Select an athlete to see saved assessments.",
                                className="text-muted")
    rows = store.list_assessments(int(athlete_id))
    opts = [{"label": f"#{r['id']} — {r['date_of_examination']} ({r['assessment_type']})",
             "value": r["id"]} for r in rows]

    if not rows:
        compare = html.Div("No saved assessments yet.", className="text-muted")
    else:
        recs = [store.get_assessment(r["id"]) for r in rows]
        cols = [{"name": "Domain", "id": "domain"}]
        data = {label: {"domain": label + (f" (of {mx})" if mx else "")}
                for label, key, mx in S.DECISION_DOMAINS}
        for r, rec in zip(rows, recs):
            col_id = f"a{r['id']}"
            cols.append({"name": f"{r['date_of_examination']} (#{r['id']})", "id": col_id})
            merged = {}
            if rec:
                merged.update(rec["scores"])
                merged.update({k: v for k, v in rec["assessment"].items()
                               if k in ("neuro_exam",)})
            for label, key, _mx in S.DECISION_DOMAINS:
                val = merged.get(key)
                data[label][col_id] = "—" if val in (None, "") else val
        compare = dash_table.DataTable(
            columns=cols, data=list(data.values()), page_action="none",
            style_table={"overflowX": "auto"},
            style_header={"fontWeight": "600", "backgroundColor": "#2b3a67",
                          "color": "white", "whiteSpace": "pre-line"},
            style_cell={"padding": "8px", "fontSize": 13, "textAlign": "center"},
            style_cell_conditional=[{"if": {"column_id": "domain"},
                                     "textAlign": "left", "fontWeight": "500"}],
        )
    return rows, opts, compare

@app.callback(Output("dl-pdf", "data", allow_duplicate=True),
              Output("hist-status", "children", allow_duplicate=True),
              Input("btn-hist-pdf", "n_clicks"),
              State("hist-assess-dd", "value"), prevent_initial_call=True)
def hist_download_pdf(n, assess_id):
    if not n or not assess_id:
        raise PreventUpdate
    rec = store.get_assessment(int(assess_id))
    if not rec:
        return no_update, dbc.Alert("Assessment not found.", color="danger")
    pdf_bytes = build_scat6_pdf(rec["assessment"], rec["scores"])
    return (dcc.send_bytes(lambda b: b.write(pdf_bytes), _pdf_name(rec["assessment"])),
            no_update)

@app.callback(Output("dl-csv", "data"),
              Output("hist-status", "children", allow_duplicate=True),
              Input("btn-hist-csv", "n_clicks"),
              State("athlete-dd", "value"), prevent_initial_call=True)
def hist_download_csv(n, athlete_id):
    if not n or not athlete_id:
        raise PreventUpdate
    csv_bytes = build_history_csv_bytes(int(athlete_id))
    return (dcc.send_bytes(lambda b: b.write(csv_bytes), _csv_name(int(athlete_id))),
            no_update)

@app.callback(Output("hist-status", "children"),
              Input("btn-hist-push", "n_clicks"),
              State("hist-assess-dd", "value"), prevent_initial_call=True)
def hist_push(n, assess_id):
    if not n or not assess_id:
        raise PreventUpdate
    rec = store.get_assessment(int(assess_id))
    if not rec:
        return dbc.Alert("Assessment not found.", color="danger")
    try:
        msgs = push_to_juvonno(rec["assessment"], rec["scores"], True, True)
        return dbc.Alert([html.Div(m) for m in msgs], color="success")
    except Exception as e:
        traceback.print_exc()
        return dbc.Alert(f"Push failed: {e}", color="danger")

# ───────────────────────── Juvonno documents (pull) ─────────────────────────
@app.callback(Output("docs-table-wrap", "children"), Output("docs-dd", "options"),
              Input("btn-docs-refresh", "n_clicks"), Input("athlete-dd", "value"),
              prevent_initial_call=True)
def refresh_docs(_n, athlete_id):
    if not athlete_id:
        return html.Div("Select an athlete first.", className="text-muted"), []
    docs = juv.list_customer_documents(int(athlete_id))
    if not docs:
        return html.Div("No documents found for this athlete in Juvonno.",
                        className="text-muted"), []
    rows, opts = [], []
    for d in docs:
        did = d.get("id")
        name = d.get("name") or d.get("filename") or d.get("file_name") or f"Document {did}"
        ddate = str(d.get("date") or d.get("created_at") or "")[:10]
        desc = d.get("description") or ""
        rows.append({"ID": did, "Name": name, "Date": ddate, "Description": desc})
        opts.append({"label": f"{name} ({ddate})", "value": did})
    table = dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in ("ID", "Name", "Date", "Description")],
        page_action="none",
        style_table={"overflowX": "auto", "maxHeight": "260px", "overflowY": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
        style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left"})
    return table, opts

@app.callback(Output("dl-doc", "data"), Output("docs-status", "children"),
              Input("btn-docs-dl", "n_clicks"),
              State("docs-dd", "value"), State("athlete-dd", "value"),
              prevent_initial_call=True)
def download_doc(n, doc_id, athlete_id):
    if not n or not doc_id or not athlete_id:
        raise PreventUpdate
    try:
        name, raw = juv.download_customer_document(int(athlete_id), int(doc_id))
        return (dcc.send_bytes(lambda b: b.write(raw), name),
                dbc.Alert(f"Downloaded {name} ({len(raw):,} bytes).", color="success",
                          className="py-2"))
    except Exception as e:
        traceback.print_exc()
        return no_update, dbc.Alert(f"Download failed: {e}", color="danger")

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
