# app.py — SCAT6 intake tool for practitioners.
#
# Same OAuth2 flow as before (apps.csipacific.ca via dash-auth-external) and the
# same Juvonno branch → group → athlete cascade. UI: dash-mantine-components +
# dash-iconify; the original bootstrap Navbar and Footer (layout/) are unchanged.
#
# Data flow:
#   • Juvonno is the source of truth for history — the History tab reads the
#     athlete's SCAT6_History CSV and document list (older PDFs) from Juvonno.
#   • Local SQLite is the offline safety net: every assessment saves locally
#     first, then pushes to Juvonno (PDF + appended history CSV). If the push
#     fails (no internet / API down), the assessment stays queued and is
#     retried automatically in the background, or manually via "Sync now".
#   • All form inputs persist in the browser (localStorage), so a refresh or
#     dropped connection mid-intake loses nothing.
import io, os, base64, traceback
from datetime import date

import requests
import pandas as pd
from dash_auth_external import DashAuthExternal
from dash import (Dash, Input, Output, State, html, dcc, dash_table,
                  no_update, ALL, ctx)
from dash.exceptions import PreventUpdate
import dash_mantine_components as dmc
from dash_iconify import DashIconify

from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

import scat6 as S
import scat6_store as store
from scat6_pdf import build_scat6_pdf
import juvonno_api as juv

# ───────────────────────── Constants ─────────────────────────
BASE_ROOT_URL = APP_URL  # login entry point (same value the old app hardcoded)
NAVY = "#2b3a67"
PERSIST = {"persistence": True, "persistence_type": "local"}

def icon(name, **kw):
    return DashIconify(icon=name, width=kw.pop("width", 20), **kw)

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
    """CSV of all locally-stored assessments for the athlete (used only to seed
    a brand-new history CSV in Juvonno, or rebuild a lost one)."""
    rows = []
    for meta in store.list_assessments(int(cid)):
        rec = store.get_assessment(meta["id"])
        if rec:
            rows.append(S.to_flat_record(rec["assessment"], rec["scores"]))
    df = pd.DataFrame(rows, columns=S.CSV_COLUMNS)
    return df.to_csv(index=False).encode("utf-8")

def fetch_history_df(cid: int):
    """Pull the newest SCAT6 history CSV for this athlete from Juvonno.
    Returns (DataFrame or None, message)."""
    copies = juv.find_documents_by_name(int(cid), _csv_name(int(cid)))
    if not copies:
        return None, "No SCAT6 history CSV found in Juvonno for this athlete yet."
    _, raw = juv.download_customer_document(int(cid), int(copies[-1].get("id")))
    df = pd.read_csv(io.BytesIO(raw))
    return df, f"Loaded {len(df)} assessment(s) from Juvonno ({_csv_name(int(cid))})."

def push_to_juvonno(assessment: dict, scores: dict, parts) -> tuple:
    """Upload PDF and/or pull-append-reupload the history CSV, per `parts`
    (iterable containing 'pdf' and/or 'csv'). The steps are independent.
    Returns (msgs, errors, failed_parts)."""
    msgs, errors, failed = [], [], []
    parts = set(parts or [])
    cid = int(assessment["athlete_id"])
    exam_date = assessment.get("date_of_examination") or date.today().isoformat()

    if "pdf" in parts:
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
            failed.append("pdf")

    if "csv" in parts:
        try:
            csv_name = _csv_name(cid)
            new_row = S.to_flat_record(assessment, scores)
            old_copies = juv.find_documents_by_name(cid, csv_name)  # newest last
            df = pd.DataFrame(columns=S.CSV_COLUMNS)
            if old_copies:
                try:
                    _, raw = juv.download_customer_document(cid, int(old_copies[-1].get("id")))
                    df = pd.read_csv(io.BytesIO(raw))
                except Exception as e:
                    msgs.append(f"Could not read existing history CSV ({e}); "
                                f"rebuilding from local records")
            if df.empty:
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

            # Clean up superseded copies where the instance allows DELETE.
            if old_copies:
                deleted = sum(
                    1 for d in old_copies
                    if d.get("id") is not None
                    and juv.delete_customer_document(cid, int(d["id"]))
                )
                if deleted == len(old_copies):
                    msgs.append(f"Removed {deleted} superseded history CSV cop(y/ies).")
                elif deleted:
                    msgs.append(f"Removed {deleted} of {len(old_copies)} older CSV copies; "
                                f"Juvonno refused the rest.")
                else:
                    msgs.append("Note: Juvonno's API does not allow deleting documents, so "
                                "older CSV copies remain on the chart — the newest "
                                f"{csv_name} is always the complete, current history.")
        except Exception as e:
            traceback.print_exc()
            errors.append(f"History CSV update failed: {e}")
            failed.append("csv")

    return msgs, errors, failed

def sync_pending() -> tuple:
    """Push every unsynced assessment to Juvonno. Returns (n_synced, n_still_pending,
    detail_lines)."""
    pending = store.list_unsynced()
    if not pending:
        return 0, 0, []
    synced_n, lines = 0, []
    for meta in pending:
        rec = store.get_assessment(meta["id"])
        if not rec:
            continue
        a = rec["assessment"]
        a["assessment_id"] = meta["id"]
        parts = [p for p in (meta.get("pending_parts") or "").split(",") if p] \
            or list(a.get("push_opts") or ["pdf", "csv"])
        try:
            msgs, errors, failed = push_to_juvonno(a, rec["scores"], parts)
        except Exception as e:
            errors, failed = [str(e)], parts
        label = f"#{meta['id']} {meta.get('athlete_name', '')} ({meta.get('date_of_examination', '')})"
        if not failed:
            store.mark_synced(meta["id"])
            synced_n += 1
            lines.append(f"Synced {label}")
        else:
            store.set_pending_parts(meta["id"], ",".join(failed))
            lines.append(f"Still pending {label}: {'; '.join(errors) or 'upload failed'}")
    return synced_n, len(store.list_unsynced()), lines

# ───────────────────────── Small UI builders ─────────────────────────
def ok_alert(children):
    return dmc.Alert(children, color="green", variant="light",
                     icon=icon("tabler:circle-check"))

def warn_alert(children):
    return dmc.Alert(children, color="yellow", variant="light",
                     icon=icon("tabler:alert-triangle"))

def err_alert(children):
    return dmc.Alert(children, color="red", variant="light",
                     icon=icon("tabler:alert-circle"))

def yn(id_, options=("Y", "N")):
    return dmc.RadioGroup(
        dmc.Group([dmc.Radio(label=o, value=o) for o in options], gap="md"),
        id=id_, value=None, **PERSIST)

def num_input(id_, label=None, maxv=None, step=1, decimals=False, width=140):
    return dmc.NumberInput(id=id_, label=label, min=0, max=maxv, step=step,
                           allowDecimal=bool(decimals), value="", w=width, **PERSIST)

def zero_one(id_):
    return dmc.RadioGroup(
        dmc.Group([dmc.Radio(label="0", value="0"), dmc.Radio(label="1", value="1")], gap="md"),
        id=id_, value=None, **PERSIST)

def text_input(id_, label=None, **kw):
    return dmc.TextInput(id=id_, label=label, **PERSIST, **kw)

def textarea(id_, label=None, **kw):
    return dmc.Textarea(id=id_, label=label, autosize=True, **PERSIST, **kw)

def section(title, children, icon_name="tabler:clipboard-check", color=NAVY):
    return dmc.Paper([
        dmc.Group([icon(icon_name, color="white"),
                   dmc.Text(title, fw=600, c="white")],
                  gap="xs", p="sm", style={"background": color,
                                           "borderRadius": "8px 8px 0 0"}),
        dmc.Box(children, p="md"),
    ], withBorder=True, radius="md", shadow="xs", mb="md")

def qtable(rows):
    """Two-column question/answer table."""
    return dmc.Table(
        [html.Tbody([html.Tr([html.Td(q, style={"width": "70%"}), html.Td(a)])
                     for q, a in rows])],
        striped=True, verticalSpacing="xs", withTableBorder=False)

def _word_grid(list_key: str, comp_type: str, trials: int):
    """Checkbox grid: words × trials (immediate memory) or words × 1 (delayed recall)."""
    words = S.WORD_LISTS.get(list_key or "A", S.WORD_LISTS["A"])
    header = [html.Th("Word")] + [html.Th(f"Trial {t+1}", style={"textAlign": "center"})
                                  for t in range(trials)]
    body = []
    for wi, w in enumerate(words):
        cells = [html.Td(dmc.Text(w, fw=500, size="sm"))]
        for t in range(trials):
            cells.append(html.Td(
                dmc.Center(dmc.Checkbox(id={"type": comp_type, "index": f"{t}-{wi}"},
                                        checked=False, **PERSIST))))
        body.append(html.Tr(cells))
    return dmc.Table([html.Thead(html.Tr(header)), html.Tbody(body)],
                     striped=True, verticalSpacing="xs", withTableBorder=False,
                     style={"maxWidth": "480px"})

def _digit_rows(list_key: str):
    pairs = S.DIGIT_LISTS.get(list_key or "A", S.DIGIT_LISTS["A"])
    body = []
    for i, (s1, s2) in enumerate(pairs):
        body.append(html.Tr([
            html.Td([dmc.Text(s1, ff="monospace", size="sm"),
                     dmc.Text(s2, ff="monospace", size="sm", c="dimmed")]),
            html.Td(zero_one({"type": "f-dig", "index": i})),
        ]))
    return dmc.Table(
        [html.Thead(html.Tr([html.Th("String (alternate below)"), html.Th("Score")])),
         html.Tbody(body)],
        striped=True, verticalSpacing="xs", withTableBorder=False,
        style={"maxWidth": "480px"})

def col(children, span=4):
    return dmc.GridCol(children, span={"base": 12, "md": span})

# ───────────────────────── Layout pieces ─────────────────────────
def athlete_picker():
    return section("Select Athlete (from Juvonno)", [
        dmc.Grid([
            col(dmc.Select(id="branch-dd", label="Branch",
                           data=[{"label": o["label"], "value": str(o["value"])}
                                 for o in juv.BRANCH_OPTS],
                           placeholder="Select a branch…", searchable=True,
                           clearable=True,
                           leftSection=icon("tabler:building-hospital", width=16))),
            col(dmc.Select(id="group-dd", label="Group",
                           placeholder="Filter by group (optional)…", searchable=True,
                           clearable=True, disabled=True,
                           leftSection=icon("tabler:users-group", width=16))),
            col(dcc.Loading(dmc.Select(id="athlete-dd", label="Athlete",
                                       placeholder="Select a branch first…",
                                       searchable=True, clearable=True, disabled=True,
                                       leftSection=icon("tabler:user", width=16)),
                            type="circle")),
        ], gutter="sm"),
        dmc.Text(id="cascade-status", size="sm", c="dimmed", mt="xs"),
        dmc.Box(id="athlete-header", mt="xs"),
    ], icon_name="tabler:user-search")

def form_layout():
    sym_rows = []
    for i, symptom in enumerate(S.SYMPTOMS):
        sym_rows.append(html.Tr([
            html.Td(dmc.Text(symptom, size="sm"), style={"width": "40%"}),
            html.Td(dmc.SegmentedControl(
                id={"type": "f-sym", "index": i}, value="0", size="xs",
                data=[str(v) for v in range(0, 7)], fullWidth=True, **PERSIST)),
        ]))

    return dmc.Box([
        # ── Athlete info ──
        section("Athlete Information", [
            dmc.Grid([
                col(text_input("f-name", "Athlete name")),
                col(dmc.TextInput(id="f-idnum", label="ID number (Juvonno)",
                                  disabled=True), span=2),
                col(text_input("f-dob", "Date of birth", placeholder="YYYY-MM-DD"), span=3),
                col(dmc.Select(id="f-sex", label="Sex",
                               data=["Male", "Female", "Prefer Not To Say", "Other"],
                               **PERSIST), span=3),
            ], gutter="sm"),
            dmc.Grid([
                col(dmc.DatePickerInput(id="f-exam-date", label="Date of examination",
                                        value=date.today().isoformat(),
                                        valueFormat="YYYY-MM-DD",
                                        leftSection=icon("tabler:calendar", width=16),
                                        **PERSIST), span=3),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label="Baseline", value="baseline"),
                               dmc.Radio(label="Suspected/Post-injury", value="post_injury")]),
                    id="f-assess-type", value="post_injury", label="Assessment type",
                    **PERSIST), span=4),
                col(text_input("f-injury-date", "Date of injury",
                               placeholder="YYYY-MM-DD"), span=2),
                col(text_input("f-injury-time", "Time of injury",
                               placeholder="HH:MM"), span=1),
                col(dmc.Select(id="f-hand", label="Dominant hand",
                               data=["Left", "Right", "Ambidextrous"], **PERSIST), span=2),
            ], gutter="sm"),
            dmc.Grid([
                col(text_input("f-sport", "Sport / Team / School"), span=6),
                col(text_input("f-time-since", "Time since injury",
                               placeholder="e.g. 45 mins / 2 days"), span=3),
            ], gutter="sm"),
        ], icon_name="tabler:id-badge-2"),

        # ── Concussion history ──
        section("Concussion History", [
            dmc.Grid([
                col(num_input("f-num-conc", "Diagnosed concussions in the past", maxv=99), span=3),
                col(text_input("f-recent-conc", "Most recent concussion",
                               placeholder="YYYY-MM-DD or description"), span=3),
                col(num_input("f-recovery-days", "Recovery time (days)", maxv=9999), span=3),
                col(text_input("f-primary-symptoms", "Primary symptoms"), span=3),
            ], gutter="sm"),
        ], icon_name="tabler:history"),

        # ── Red flags ──
        section("Red Flags (Box 1) — any red flag → remove from play, urgent medical assessment", [
            dmc.CheckboxGroup(
                dmc.Stack([dmc.Checkbox(label=f, value=str(i))
                           for i, f in enumerate(S.RED_FLAGS)], gap=6),
                id="f-redflags", value=[], **PERSIST),
        ], icon_name="tabler:flag-exclamation", color="#8f1f2f"),

        # ── Immediate assessment ──
        section("Immediate Assessment — Step 1: Observable Signs", [
            dmc.CheckboxGroup(
                dmc.Group([dmc.Checkbox(label="Witnessed", value="witnessed"),
                           dmc.Checkbox(label="Observed on Video", value="video")]),
                id="f-obs-context", value=[], mb="sm", **PERSIST),
            qtable([(dmc.Text(sign, size="sm"), yn({"type": "f-obs", "index": i}))
                    for i, sign in enumerate(S.OBSERVABLE_SIGNS)]),
        ], icon_name="tabler:eye"),

        section("Immediate Assessment — Step 2: Glasgow Coma Scale", [
            dmc.Grid([
                col(dmc.Select(id="f-gcs-e", label="Best Eye Response (E)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_EYE], **PERSIST)),
                col(dmc.Select(id="f-gcs-v", label="Best Verbal Response (V)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_VERBAL], **PERSIST)),
                col(dmc.Select(id="f-gcs-m", label="Best Motor Response (M)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_MOTOR], **PERSIST)),
            ], gutter="sm"),
            dmc.Text(id="gcs-total-display", fw=600, mt="xs"),
        ], icon_name="tabler:brain"),

        section("Immediate Assessment — Step 3: Cervical Spine", [
            dmc.Text("In a patient who is not lucid or fully conscious, a cervical spine "
                     "injury should be assumed and spinal precautions taken.",
                     size="sm", c="dimmed", mb="xs"),
            qtable([(dmc.Text(q, size="sm"), yn({"type": "f-cerv", "index": i}))
                    for i, q in enumerate(S.CERVICAL_ITEMS)]),
        ], icon_name="tabler:bone"),

        section("Immediate Assessment — Step 4: Coordination & Ocular/Motor Screen", [
            qtable([(dmc.Text(q, size="sm"), yn({"type": "f-coord", "index": i}))
                    for i, q in enumerate(S.COORD_OCULAR_ITEMS)]),
            text_input("f-ocular-desc",
                       "If extraocular movements abnormal, describe:", mt="xs"),
        ], icon_name="tabler:eye-check"),

        section("Immediate Assessment — Step 5: Maddocks Questions", [
            dmc.Text('"I am going to ask you a few questions, please listen carefully and '
                     'give your best effort. First, tell me what happened?"',
                     size="sm", fs="italic", c="blue", mb="xs"),
            qtable([(dmc.Text(q, size="sm"), zero_one({"type": "f-mad", "index": i}))
                    for i, q in enumerate(S.MADDOCKS_QUESTIONS)]),
        ], icon_name="tabler:message-question"),

        # ── Off-field ──
        section("Off-Field — Step 1: Athlete Background", [
            qtable([(dmc.Text(label, size="sm"), yn({"type": "f-bg", "index": key}))
                    for key, label in S.BACKGROUND_ITEMS]),
            dmc.Grid([
                col(textarea("f-bg-notes", "Notes", minRows=2), span=6),
                col(textarea("f-medications", "Current medications", minRows=2), span=6),
            ], gutter="sm", mt="xs"),
        ], icon_name="tabler:notes"),

        section("Off-Field — Step 2: Symptom Evaluation (athlete self-report, 0–6)", [
            dmc.Text("Baseline: rate how you typically feel. Post-injury: rate how you feel now.",
                     size="sm", c="dimmed", mb="xs"),
            dmc.Table([html.Thead(html.Tr([html.Th("Symptom"),
                                           html.Th("Rating (0 = none, 6 = severe)")])),
                       html.Tbody(sym_rows)],
                      striped=True, verticalSpacing="xs", withTableBorder=False),
            dmc.Grid([
                col(dmc.Box([dmc.Text("Symptoms worse with physical activity?", size="sm", mb=4),
                             yn("f-worse-phys")])),
                col(dmc.Box([dmc.Text("Symptoms worse with mental activity?", size="sm", mb=4),
                             yn("f-worse-ment")])),
                col(num_input("f-pct-normal", S.PERCENT_NORMAL_QUESTION, maxv=100)),
            ], gutter="sm", mt="sm"),
            textarea("f-pct-why", "If not 100%, why?", minRows=2, mt="xs"),
        ], icon_name="tabler:mood-sick"),

        section("Off-Field — Step 3: Cognitive Screening — Orientation", [
            qtable([(dmc.Text(q, size="sm"), zero_one({"type": "f-orient", "index": i}))
                    for i, q in enumerate(S.ORIENTATION_QUESTIONS)]),
        ], icon_name="tabler:compass"),

        section("Off-Field — Step 3: Immediate Memory (3 trials, 1 word/second)", [
            dmc.Grid([
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=k, value=k) for k in ("A", "B", "C")]),
                    id="f-wordlist", value="A", label="Word list", **PERSIST)),
                col(text_input("f-im-time", "Time last trial completed",
                               placeholder="HH:MM"), span=3),
            ], gutter="sm", mb="xs"),
            dmc.Box(id="im-grid"),
        ], icon_name="tabler:list-numbers"),

        section("Off-Field — Step 3: Concentration — Digits Backward & Months in Reverse", [
            dmc.RadioGroup(
                dmc.Group([dmc.Radio(label=k, value=k) for k in ("A", "B", "C")]),
                id="f-digitlist", value="A", label="Digit list", mb="xs", **PERSIST),
            dmc.Box(id="dig-rows"),
            dmc.Divider(my="sm"),
            dmc.Text('"Now tell me the months of the year in reverse order as QUICKLY and '
                     'as accurately as possible. Start with the last month and go backward: '
                     'December, November… go ahead."', size="sm", fs="italic", c="blue"),
            dmc.Text(" — ".join(S.MONTHS_REVERSED), size="xs", c="dimmed", mb="xs"),
            dmc.Group([
                num_input("f-months-secs", "Time to complete (secs)", maxv=999,
                          step=0.1, decimals=True),
                num_input("f-months-errs", "Number of errors", maxv=12),
                dmc.Text(id="months-score-display", fw=600, mt="lg"),
            ], gap="md", align="flex-end"),
        ], icon_name="tabler:123"),

        section("Off-Field — Step 4: Balance — Modified BESS "
                "(errors, 20 s each; test the non-dominant foot)", [
            dmc.Grid([
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v) for v in ("Left", "Right")]),
                    id="f-foot", value=None, label="Foot tested", **PERSIST), span=3),
                col(text_input("f-surface", "Testing surface")),
                col(text_input("f-footwear", "Footwear")),
            ], gutter="sm", mb="xs"),
            dmc.Group([num_input(f"f-bess-{key}", f"{lbl} (of 10)", maxv=10)
                       for key, lbl in S.MBESS_STANCES], gap="md"),
            dmc.Text(id="bess-total-display", fw=600, mt="xs"),
            dmc.Divider(my="sm"),
            dmc.Switch(id="f-foam-toggle", checked=False,
                       label="Add optional foam-surface mBESS", **PERSIST),
            dmc.Box(dmc.Group([num_input(f"f-foam-{key}", f"Foam — {lbl} (of 10)", maxv=10)
                               for key, lbl in S.MBESS_STANCES], gap="md", mt="xs"),
                    id="foam-collapse", style={"display": "none"}),
        ], icon_name="tabler:yoga"),

        section("Off-Field — Step 4: Timed Tandem Gait (3 m line, heel-to-toe, 3 trials)", [
            dmc.Group([num_input(f"f-tg-{t}", f"Trial {t} (secs)", maxv=999,
                                 step=0.01, decimals=True) for t in (1, 2, 3)], gap="md"),
            dmc.Text(id="tg-display", fw=600, mt="xs"),
            dmc.Divider(my="sm"),
            dmc.Text("Dual Task Gait (optional — serial 7s while walking)", fw=500, size="sm"),
            dmc.Group([num_input(f"f-dual-{t}", f"Trial {t} time (secs)", maxv=999,
                                 step=0.01, decimals=True) for t in (1, 2, 3)]
                      + [num_input("f-dual-errs", "Counting errors (total)", maxv=99)],
                      gap="md", mt="xs"),
            text_input("f-tg-incomplete", "Any trials not completed? Why?", mt="xs"),
        ], icon_name="tabler:walk"),

        section("Off-Field — Step 5: Delayed Recall (≥ 5 min after Immediate Memory)", [
            text_input("f-dr-time", "Time started", placeholder="HH:MM", w=160, mb="xs"),
            dmc.Box(id="dr-grid"),
        ], icon_name="tabler:clock-pause"),

        section("Step 6: Decision & Attestation", [
            dmc.Grid([
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v) for v in ("Normal", "Abnormal")]),
                    id="f-neuro", value=None,
                    label="Neurological exam (acute evaluation)", **PERSIST)),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v)
                               for v in ("Yes", "No", "Not applicable")]),
                    id="f-different", value=None,
                    label="Different from usual self?", **PERSIST)),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v)
                               for v in ("Yes", "No", "Deferred")]),
                    id="f-diagnosed", value=None,
                    label="Concussion diagnosed?", **PERSIST)),
            ], gutter="sm", mb="xs"),
            textarea("f-notes", "Additional clinical notes", minRows=3),
            dmc.Divider(my="sm"),
            dmc.Text("I am an HCP and I have personally administered or supervised the "
                     "administration of this SCAT6.", size="sm", fs="italic", mb="xs"),
            dmc.Grid([
                col(text_input("f-examiner", "Examiner (HCP) name")),
                col(text_input("f-examiner-title", "Title / speciality")),
                col(text_input("f-examiner-license", "Registration / license #")),
            ], gutter="sm"),
        ], icon_name="tabler:stethoscope"),

        # ── Live score summary + save ──
        section("Score Summary & Save", [
            dmc.Box(id="score-summary"),
            dmc.Divider(my="sm"),
            dmc.CheckboxGroup(
                dmc.Group([dmc.Checkbox(label="Upload PDF to Juvonno", value="pdf"),
                           dmc.Checkbox(label="Update SCAT6 history CSV in Juvonno",
                                        value="csv")]),
                id="f-push-opts", value=["pdf", "csv"], mb="sm", **PERSIST),
            dmc.Group([
                dmc.Button("Save Assessment", id="btn-save", color="green", size="md",
                           leftSection=icon("tabler:device-floppy")),
                dmc.Button("Clear form", id="btn-clear", variant="subtle", color="gray",
                           leftSection=icon("tabler:eraser")),
            ], gap="sm"),
            dmc.Text("Entries are kept in this browser until cleared — a refresh or "
                     "dropped connection won't lose the form. If Juvonno can't be "
                     "reached, the assessment is saved locally and uploaded "
                     "automatically when the connection returns.",
                     size="xs", c="dimmed", mt="xs"),
            dcc.Loading(dmc.Box(id="save-status", mt="sm"), type="circle"),
        ], icon_name="tabler:sum", color="#1d5b3c"),
    ])

def history_layout():
    return dmc.Box([
        section("SCAT6 History (from Juvonno)", [
            dmc.Group([
                dmc.Button("Refresh from Juvonno", id="btn-hist-refresh", variant="light",
                           leftSection=icon("tabler:refresh")),
                dmc.Button("Download history CSV", id="btn-hist-csv", color="blue",
                           leftSection=icon("tabler:file-type-csv")),
            ], gap="sm", mb="sm"),
            dcc.Loading(dmc.Box(id="hist-table-wrap"), type="circle"),
            dmc.Box(id="hist-status", mt="sm"),
        ], icon_name="tabler:cloud-download"),
        section("Serial Comparison (SCAT6 Step 6 domains across assessments)", [
            dcc.Loading(dmc.Box(id="hist-compare"), type="circle"),
        ], icon_name="tabler:chart-line"),
        section("Athlete Documents in Juvonno (older PDFs & CSVs)", [
            dmc.Button("Refresh document list", id="btn-docs-refresh", variant="light",
                       leftSection=icon("tabler:refresh"), mb="sm"),
            dcc.Loading(dmc.Box(id="docs-table-wrap"), type="circle"),
            dmc.Group([
                dmc.Select(id="docs-dd", placeholder="Pick a document to download…",
                           searchable=True, clearable=True, w=380),
                dmc.Button("Download document", id="btn-docs-dl", color="blue",
                           leftSection=icon("tabler:download")),
            ], gap="sm", mt="sm"),
            dmc.Box(id="docs-status", mt="sm"),
        ], icon_name="tabler:files"),
        section("Pending Uploads (offline queue)", [
            dmc.Text("Assessments saved while Juvonno was unreachable wait here and are "
                     "retried automatically every 2 minutes. Nothing is lost if the "
                     "internet drops — the local copy is kept until it uploads.",
                     size="sm", c="dimmed", mb="sm"),
            dmc.Box(id="pending-wrap"),
            dmc.Group([
                dmc.Button("Sync now", id="btn-sync", color="orange",
                           leftSection=icon("tabler:cloud-upload")),
            ], gap="sm", mt="sm"),
            dcc.Loading(dmc.Box(id="sync-status", mt="sm"), type="circle"),
        ], icon_name="tabler:cloud-pause"),
    ])

# ───────────────────────── App shell ─────────────────────────
app = Dash(
    __name__,
    server=server,
    # Bootstrap kept only for the unchanged Navbar / Footer components.
    external_stylesheets=[
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
    ],
    suppress_callback_exceptions=True,
)
app.title = "SCAT6 Intake — CSI Pacific"

app.layout = dmc.MantineProvider(
    theme={"primaryColor": "indigo",
           "fontFamily": "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial"},
    children=html.Div([
        dcc.Location(id="redirect-to", refresh=True),
        dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
        dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),
        dcc.Interval(id="sync-interval", interval=120_000, n_intervals=0),

        Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

        dmc.Container([
            dmc.Group([icon("tabler:first-aid-kit", width=30, color=NAVY),
                       dmc.Title("SCAT6™ Intake Tool", order=2, c=NAVY),
                       dmc.Badge(id="pending-badge", color="orange", variant="light",
                                 leftSection=icon("tabler:cloud-pause", width=12))],
                      gap="xs", mt="md"),
            dmc.Text("Sport Concussion Assessment Tool 6 — for use by Health Care "
                     "Professionals. Athletes are loaded from Juvonno; history is read "
                     "from the athlete's Juvonno documents; assessments filled out "
                     "offline are queued locally and uploaded when the connection "
                     "returns.", size="sm", c="dimmed", mb="md"),
            athlete_picker(),
            dmc.Tabs([
                dmc.TabsList([
                    dmc.TabsTab("New SCAT6", value="tab-form",
                                leftSection=icon("tabler:clipboard-plus", width=16)),
                    dmc.TabsTab("History & Documents", value="tab-history",
                                leftSection=icon("tabler:folder-open", width=16)),
                ], grow=True),
            ], id="main-tabs", value="tab-form", variant="pills", mb="md"),

            # Both panes stay mounted so form state survives tab switches.
            dmc.Box(form_layout(), id="pane-form"),
            dmc.Box(history_layout(), id="pane-history", style={"display": "none"}),

            dmc.Space(h=90),  # keep content clear of the fixed footer
        ], size="xl", px="md"),

        # Stores & downloads
        dcc.Store(id="demo-store", data={}),
        dcc.Store(id="form-collect", data={}),
        dcc.Store(id="history-refresh", data=0),
        dcc.Download(id="dl-pdf"),
        dcc.Download(id="dl-csv"),
        dcc.Download(id="dl-doc"),

        Footer().render(),
    ]),
)

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
    Output("group-dd", "data"), Output("group-dd", "disabled"), Output("group-dd", "value"),
    Output("athlete-dd", "data"), Output("athlete-dd", "disabled"), Output("athlete-dd", "value"),
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
    ath_opts = [{"label": a["label"], "value": str(a["id"])} for a in filtered]

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
    bits = [f"Juvonno ID {demo['id']}"]
    if demo.get("dob"):
        bits.append(f"DOB {demo['dob']}")
    if demo.get("chart_number"):
        bits.append(f"Chart {demo['chart_number']}")
    header = dmc.Alert(
        dmc.Group([dmc.Text(demo.get("name") or f"Athlete {athlete_id}", fw=700),
                   dmc.Text(" • ".join(bits), size="sm")], gap="md"),
        color="blue", variant="light", icon=icon("tabler:user-check"), py=8)
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

@app.callback(Output("foam-collapse", "style"), Input("f-foam-toggle", "checked"))
def toggle_foam(checked):
    return {"display": "block"} if checked else {"display": "none"}

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

    def _num_or_none(v):
        try:
            return None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            return None

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
        "months_seconds": _num_or_none(months[0]), "months_errors": months[1],
        "mbess_double": mbess[0] if mbess[0] != "" else None,
        "mbess_tandem": mbess[1] if mbess[1] != "" else None,
        "mbess_single": mbess[2] if mbess[2] != "" else None,
        "mbess_foam_double": foam[0] if foam[0] != "" else None,
        "mbess_foam_tandem": foam[1] if foam[1] != "" else None,
        "mbess_foam_single": foam[2] if foam[2] != "" else None,
        "tg_times": [_num_or_none(t) for t in c.get("tg", [])],
        "dual_task_times": [_num_or_none(t) for t in c.get("dual", [])],
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
    Input({"type": "f-im", "index": ALL}, "checked"),
    Input({"type": "f-dig", "index": ALL}, "value"),
    Input({"type": "f-dr", "index": ALL}, "checked"),
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

    def tile(label, val, maxv=None, icon_name="tabler:hash"):
        txt = "—" if val is None else (f"{val} / {maxv}" if maxv else str(val))
        return dmc.Paper([
            dmc.Group([icon(icon_name, width=16, color=NAVY),
                       dmc.Text(label, size="xs", c="dimmed")], gap=6),
            dmc.Text(txt, fw=700, size="lg"),
        ], withBorder=True, radius="md", p="xs", ta="center")

    summary = dmc.SimpleGrid([
        tile("Symptoms", s["symptom_number"], 22, "tabler:mood-sick"),
        tile("Severity", s["symptom_severity"], 132, "tabler:gauge"),
        tile("Orientation", s["orientation"], 5, "tabler:compass"),
        tile("Imm. memory", s["immediate_memory"], 30, "tabler:list-numbers"),
        tile("Concentration", s["concentration"], 5, "tabler:123"),
        tile("Delayed recall", s["delayed_recall"], 10, "tabler:clock-pause"),
        tile("Cognitive total", s["cognitive_total"], 50, "tabler:brain"),
        tile("mBESS errors", s["mbess_total"], 30, "tabler:yoga"),
        tile("Tandem fastest", s["tg_fastest"], None, "tabler:walk"),
        tile("Maddocks", s["maddocks"], 5, "tabler:message-question"),
        tile("GCS", s["gcs_total"], 15, "tabler:first-aid-kit"),
    ], cols={"base": 3, "md": 6}, spacing="xs")

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
    State("f-exam-date", "value"), State("f-assess-type", "value"),
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
        return (err_alert("Select an athlete from Juvonno before saving."),
                no_update, no_update)

    # background pattern-state values by key (from ctx to keep index association)
    bg_map = {}
    for grp in ctx.states_list:
        if isinstance(grp, list) and grp and isinstance(grp[0].get("id"), dict) \
                and grp[0]["id"].get("type") == "f-bg":
            bg_map = {e["id"]["index"]: e.get("value") for e in grp}
            break

    a = _collect_to_assessment(collect or {})
    redflag_set = {str(x) for x in (redflags or [])}
    redflag_bools = [str(i) in redflag_set for i in range(len(S.RED_FLAGS))]
    push_opts = list(push_opts or [])
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
        "push_opts": push_opts,
    })

    scores = S.compute_all_scores(a)

    # 1) Always save locally first — this is the offline safety net.
    try:
        new_id = store.save_assessment(a, scores, synced=(not push_opts),
                                       pending_parts=",".join(push_opts))
        a["assessment_id"] = new_id
    except Exception as e:
        traceback.print_exc()
        return err_alert(f"Local save failed: {e}"), no_update, no_update
    msgs = [f"Assessment #{new_id} saved locally."]

    # 2) Then try to push to Juvonno; failures stay queued for auto-retry.
    queued = []
    if push_opts:
        try:
            push_msgs, push_errs, failed = push_to_juvonno(a, scores, push_opts)
        except Exception as e:
            traceback.print_exc()
            push_msgs, push_errs, failed = [], [str(e)], list(push_opts)
        msgs += push_msgs
        if not failed:
            store.mark_synced(new_id)
        else:
            store.set_pending_parts(new_id, ",".join(failed))
            queued = push_errs or ["Upload failed"]

    pdf_bytes = build_scat6_pdf(a, scores)
    dl = dcc.send_bytes(lambda b: b.write(pdf_bytes), _pdf_name(a))

    alerts = [ok_alert([dmc.Text(m, size="sm") for m in msgs])]
    if queued:
        alerts.append(warn_alert(
            [dmc.Text(m, size="sm") for m in queued]
            + [dmc.Text("This assessment is safely stored locally and will upload "
                        "automatically when the connection to Juvonno returns "
                        "(or use “Sync now” on the History tab).",
                        size="sm", fw=600)]))
    return dmc.Stack(alerts, gap="xs"), dl, (hist_tick or 0) + 1

# ───────────────────────── Clear form ─────────────────────────
@app.callback(
    Output({"type": "f-sym", "index": ALL}, "value"),
    Output({"type": "f-obs", "index": ALL}, "value"),
    Output({"type": "f-cerv", "index": ALL}, "value"),
    Output({"type": "f-coord", "index": ALL}, "value"),
    Output({"type": "f-mad", "index": ALL}, "value"),
    Output({"type": "f-orient", "index": ALL}, "value"),
    Output({"type": "f-im", "index": ALL}, "checked"),
    Output({"type": "f-dig", "index": ALL}, "value"),
    Output({"type": "f-dr", "index": ALL}, "checked"),
    Output({"type": "f-bg", "index": ALL}, "value"),
    Output("f-gcs-e", "value"), Output("f-gcs-v", "value"), Output("f-gcs-m", "value"),
    Output("f-months-secs", "value"), Output("f-months-errs", "value"),
    Output("f-bess-double", "value"), Output("f-bess-tandem", "value"),
    Output("f-bess-single", "value"),
    Output("f-foam-double", "value"), Output("f-foam-tandem", "value"),
    Output("f-foam-single", "value"),
    Output("f-tg-1", "value"), Output("f-tg-2", "value"), Output("f-tg-3", "value"),
    Output("f-dual-1", "value"), Output("f-dual-2", "value"), Output("f-dual-3", "value"),
    Output("f-dual-errs", "value"),
    Output("f-redflags", "value"), Output("f-obs-context", "value"),
    Output("f-foam-toggle", "checked"),
    Output("f-injury-date", "value"), Output("f-injury-time", "value"),
    Output("f-time-since", "value"), Output("f-hand", "value"), Output("f-sport", "value"),
    Output("f-num-conc", "value"), Output("f-recent-conc", "value"),
    Output("f-recovery-days", "value"), Output("f-primary-symptoms", "value"),
    Output("f-ocular-desc", "value"), Output("f-bg-notes", "value"),
    Output("f-medications", "value"),
    Output("f-worse-phys", "value"), Output("f-worse-ment", "value"),
    Output("f-pct-normal", "value"), Output("f-pct-why", "value"),
    Output("f-wordlist", "value"), Output("f-im-time", "value"),
    Output("f-digitlist", "value"),
    Output("f-foot", "value"), Output("f-surface", "value"), Output("f-footwear", "value"),
    Output("f-tg-incomplete", "value"), Output("f-dr-time", "value"),
    Output("f-neuro", "value"), Output("f-different", "value"),
    Output("f-diagnosed", "value"), Output("f-notes", "value"),
    Output("f-examiner-title", "value"), Output("f-examiner-license", "value"),
    Output("f-exam-date", "value"),
    Output("f-name", "value", allow_duplicate=True),
    Output("f-dob", "value", allow_duplicate=True),
    Output("f-sex", "value", allow_duplicate=True),
    Input("btn-clear", "n_clicks"),
    State("demo-store", "data"),
    prevent_initial_call=True)
def clear_form(n, demo):
    if not n:
        raise PreventUpdate
    sizes = []
    for grp in ctx.outputs_list[:10]:
        sizes.append(len(grp) if isinstance(grp, list) else 0)
    demo = demo or {}
    sex_map = {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}
    return (
        ["0"] * sizes[0],          # symptoms back to 0
        [None] * sizes[1],         # observable signs
        [None] * sizes[2],         # cervical
        [None] * sizes[3],         # coordination
        [None] * sizes[4],         # maddocks
        [None] * sizes[5],         # orientation
        [False] * sizes[6],        # immediate memory checks
        [None] * sizes[7],         # digits
        [False] * sizes[8],        # delayed recall checks
        [None] * sizes[9],         # background
        None, None, None,          # gcs
        "", "",                    # months
        "", "", "",                # mbess
        "", "", "",                # foam
        "", "", "",                # tg
        "", "", "", "",            # dual + errs
        [], [],                    # redflags, obs-context
        False,                     # foam toggle
        "", "", "", None, "",      # injury date/time, time since, hand, sport
        "", "", "", "",            # concussion history
        "", "", "",                # ocular desc, bg notes, medications
        None, None, "", "",        # worse phys/ment, pct, why
        "A", "", "A",              # word list, im time, digit list
        None, "", "", "", "",      # foot, surface, footwear, tg incomplete, dr time
        None, None, None, "",      # decision fields, notes
        "", "",                    # examiner title / license
        date.today().isoformat(),  # exam date
        demo.get("name", ""),      # re-prefill from selected athlete
        demo.get("dob", ""),
        sex_map.get(str(demo.get("sex", "")).strip().lower(), None),
    )

# ───────────────────────── History tab (reads from Juvonno) ─────────────────────────
HIST_TABLE_COLS = [
    ("Date", "date_of_examination"), ("Type", "assessment_type"),
    ("Examiner", "examiner"), ("Symptoms", "symptom_number"),
    ("Severity", "symptom_severity"), ("Cognitive", "cognitive_total"),
    ("mBESS", "mbess_total"), ("Tandem fastest", "tg_fastest"),
    ("Diagnosed", "concussion_diagnosed"),
]

@app.callback(
    Output("hist-table-wrap", "children"), Output("hist-compare", "children"),
    Output("hist-status", "children"),
    Input("history-refresh", "data"), Input("athlete-dd", "value"),
    Input("btn-hist-refresh", "n_clicks"))
def refresh_history(_tick, athlete_id, _n):
    empty = dmc.Text("Select an athlete to load their SCAT6 history from Juvonno.",
                     size="sm", c="dimmed")
    if not athlete_id:
        return empty, empty, ""
    try:
        df, msg = fetch_history_df(int(athlete_id))
    except Exception as e:
        traceback.print_exc()
        return (dmc.Text("Could not reach Juvonno.", size="sm", c="dimmed"),
                dmc.Text("Could not reach Juvonno.", size="sm", c="dimmed"),
                err_alert(f"Failed to load history from Juvonno: {e}"))
    if df is None or df.empty:
        note = dmc.Text(msg, size="sm", c="dimmed")
        return note, dmc.Text("No assessments in Juvonno yet.", size="sm", c="dimmed"), ""

    df = df.fillna("")
    if "date_of_examination" in df.columns:
        df = df.sort_values("date_of_examination", kind="stable")

    table = dash_table.DataTable(
        data=[{k: r.get(k, "") for _, k in HIST_TABLE_COLS} for r in
              df.to_dict("records")],
        columns=[{"name": n, "id": k} for n, k in HIST_TABLE_COLS],
        page_action="none",
        style_table={"overflowX": "auto", "maxHeight": "300px", "overflowY": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
        style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left",
                    "fontFamily": "inherit"})

    # Serial comparison straight from the Juvonno CSV
    cols = [{"name": "Domain", "id": "domain"}]
    data = {label: {"domain": label + (f" (of {mx})" if mx else "")}
            for label, key, mx in S.DECISION_DOMAINS}
    for i, r in enumerate(df.to_dict("records")):
        col_id = f"c{i}"
        hdr = str(r.get("date_of_examination", "")) or f"Assessment {i + 1}"
        cols.append({"name": hdr, "id": col_id})
        for label, key, _mx in S.DECISION_DOMAINS:
            val = r.get(key, "")
            data[label][col_id] = "—" if val in (None, "") else val
    compare = dash_table.DataTable(
        columns=cols, data=list(data.values()), page_action="none",
        style_table={"overflowX": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": NAVY, "color": "white"},
        style_cell={"padding": "8px", "fontSize": 13, "textAlign": "center",
                    "fontFamily": "inherit"},
        style_cell_conditional=[{"if": {"column_id": "domain"},
                                 "textAlign": "left", "fontWeight": "500"}])
    return table, compare, dmc.Text(msg, size="sm", c="dimmed")

@app.callback(Output("dl-csv", "data"),
              Output("hist-status", "children", allow_duplicate=True),
              Input("btn-hist-csv", "n_clicks"),
              State("athlete-dd", "value"), prevent_initial_call=True)
def hist_download_csv(n, athlete_id):
    if not n or not athlete_id:
        raise PreventUpdate
    cid = int(athlete_id)
    try:
        copies = juv.find_documents_by_name(cid, _csv_name(cid))
        if not copies:
            return no_update, warn_alert("No SCAT6 history CSV in Juvonno for this athlete yet.")
        name, raw = juv.download_customer_document(cid, int(copies[-1].get("id")))
        fname = name if name.lower().endswith(".csv") else _csv_name(cid)
        return dcc.send_bytes(lambda b: b.write(raw), fname), no_update
    except Exception as e:
        traceback.print_exc()
        return no_update, err_alert(f"Download from Juvonno failed: {e}")

# ───────────────────────── Offline queue / sync ─────────────────────────
def _pending_children():
    pending = store.list_unsynced()
    if not pending:
        return dmc.Group([icon("tabler:circle-check", color="green"),
                          dmc.Text("Nothing pending — all assessments are uploaded.",
                                   size="sm", c="dimmed")], gap="xs")
    rows = [{"ID": p["id"], "Athlete": p["athlete_name"],
             "Date": p["date_of_examination"],
             "Waiting to upload": p.get("pending_parts") or "pdf,csv",
             "Saved at (UTC)": p["created_at"]} for p in pending]
    return dash_table.DataTable(
        data=rows, columns=[{"name": c, "id": c} for c in rows[0].keys()],
        page_action="none",
        style_table={"overflowX": "auto", "maxHeight": "220px", "overflowY": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#fff4e6"},
        style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left",
                    "fontFamily": "inherit"})

@app.callback(Output("pending-wrap", "children"), Output("pending-badge", "children"),
              Output("pending-badge", "style"),
              Input("history-refresh", "data"))
def refresh_pending(_tick):
    n = len(store.list_unsynced())
    badge_style = {} if n else {"display": "none"}
    return _pending_children(), f"{n} pending upload{'s' if n != 1 else ''}", badge_style

@app.callback(Output("sync-status", "children"),
              Output("history-refresh", "data", allow_duplicate=True),
              Input("btn-sync", "n_clicks"), Input("sync-interval", "n_intervals"),
              State("history-refresh", "data"),
              prevent_initial_call=True)
def run_sync(_n, _i, hist_tick):
    manual = ctx.triggered_id == "btn-sync"
    if not store.list_unsynced():
        if manual:
            return ok_alert("Nothing to sync — all assessments are uploaded."), no_update
        raise PreventUpdate
    synced_n, still_pending, lines = sync_pending()
    if synced_n == 0 and not manual:
        raise PreventUpdate  # quiet background retry; don't churn the UI
    body = [dmc.Text(l, size="sm") for l in lines]
    alert = ok_alert(body) if still_pending == 0 else warn_alert(body)
    return alert, (hist_tick or 0) + 1

# ───────────────────────── Juvonno documents (pull) ─────────────────────────
@app.callback(Output("docs-table-wrap", "children"), Output("docs-dd", "data"),
              Input("btn-docs-refresh", "n_clicks"), Input("athlete-dd", "value"),
              prevent_initial_call=True)
def refresh_docs(_n, athlete_id):
    if not athlete_id:
        return dmc.Text("Select an athlete first.", size="sm", c="dimmed"), []
    docs = juv.list_customer_documents(int(athlete_id))
    if not docs:
        return dmc.Text("No documents found for this athlete in Juvonno.",
                        size="sm", c="dimmed"), []
    rows, opts = [], []
    for d in docs:
        did = d.get("id")
        name = d.get("name") or d.get("filename") or d.get("file_name") or f"Document {did}"
        ddate = str(d.get("date") or d.get("created_at") or "")[:10]
        desc = d.get("description") or ""
        rows.append({"ID": did, "Name": name, "Date": ddate, "Description": desc})
        opts.append({"label": f"{name} ({ddate})", "value": str(did)})
    table = dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in ("ID", "Name", "Date", "Description")],
        page_action="none",
        style_table={"overflowX": "auto", "maxHeight": "260px", "overflowY": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
        style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left",
                    "fontFamily": "inherit"})
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
                ok_alert(f"Downloaded {name} ({len(raw):,} bytes)."))
    except Exception as e:
        traceback.print_exc()
        return no_update, err_alert(f"Download failed: {e}")

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
