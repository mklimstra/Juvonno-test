# app.py — SCAT6 intake tool for practitioners.
#
# Same OAuth2 flow as before (apps.csipacific.ca via dash-auth-external) and the
# same Juvonno branch → group → athlete cascade. UI is built with
# dash-mantine-components + dash-iconify; the original bootstrap Navbar and
# Footer (layout/) are kept unchanged. Completed assessments are stored locally
# (SQLite) and pushed to the athlete's Juvonno documents as a formatted PDF
# plus a single per-athlete SCAT6 history CSV that is pulled, appended, and
# re-uploaded (superseded copies are deleted when the API allows it).
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

            # Clean up superseded copies. Juvonno's public API defines no DELETE
            # for documents, but some instances accept it — try, and be honest
            # about the result either way.
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

    if errors and not msgs:
        raise RuntimeError("; ".join(errors))
    msgs.extend(errors)  # partial failure: surface alongside successes
    return msgs

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
        id=id_, value=None)

def num_input(id_, label=None, maxv=None, step=1, decimals=False, width=140):
    return dmc.NumberInput(id=id_, label=label, min=0, max=maxv, step=step,
                           allowDecimal=bool(decimals), value="", w=width)

def zero_one(id_):
    return dmc.RadioGroup(
        dmc.Group([dmc.Radio(label="0", value="0"), dmc.Radio(label="1", value="1")], gap="md"),
        id=id_, value=None)

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
                                        checked=False))))
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
                data=[str(v) for v in range(0, 7)], fullWidth=True)),
        ]))

    return dmc.Box([
        # ── Athlete info ──
        section("Athlete Information", [
            dmc.Grid([
                col(dmc.TextInput(id="f-name", label="Athlete name")),
                col(dmc.TextInput(id="f-idnum", label="ID number (Juvonno)", disabled=True), span=2),
                col(dmc.TextInput(id="f-dob", label="Date of birth", placeholder="YYYY-MM-DD"), span=3),
                col(dmc.Select(id="f-sex", label="Sex",
                               data=["Male", "Female", "Prefer Not To Say", "Other"]), span=3),
            ], gutter="sm"),
            dmc.Grid([
                col(dmc.DatePickerInput(id="f-exam-date", label="Date of examination",
                                        value=date.today().isoformat(),
                                        valueFormat="YYYY-MM-DD",
                                        leftSection=icon("tabler:calendar", width=16)), span=3),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label="Baseline", value="baseline"),
                               dmc.Radio(label="Suspected/Post-injury", value="post_injury")]),
                    id="f-assess-type", value="post_injury", label="Assessment type"), span=4),
                col(dmc.TextInput(id="f-injury-date", label="Date of injury",
                                  placeholder="YYYY-MM-DD"), span=2),
                col(dmc.TextInput(id="f-injury-time", label="Time of injury",
                                  placeholder="HH:MM"), span=1),
                col(dmc.Select(id="f-hand", label="Dominant hand",
                               data=["Left", "Right", "Ambidextrous"]), span=2),
            ], gutter="sm"),
            dmc.Grid([
                col(dmc.TextInput(id="f-sport", label="Sport / Team / School"), span=6),
                col(dmc.TextInput(id="f-time-since", label="Time since injury",
                                  placeholder="e.g. 45 mins / 2 days"), span=3),
            ], gutter="sm"),
        ], icon_name="tabler:id-badge-2"),

        # ── Concussion history ──
        section("Concussion History", [
            dmc.Grid([
                col(num_input("f-num-conc", "Diagnosed concussions in the past", maxv=99), span=3),
                col(dmc.TextInput(id="f-recent-conc", label="Most recent concussion",
                                  placeholder="YYYY-MM-DD or description"), span=3),
                col(num_input("f-recovery-days", "Recovery time (days)", maxv=9999), span=3),
                col(dmc.TextInput(id="f-primary-symptoms", label="Primary symptoms"), span=3),
            ], gutter="sm"),
        ], icon_name="tabler:history"),

        # ── Red flags ──
        section("Red Flags (Box 1) — any red flag → remove from play, urgent medical assessment", [
            dmc.CheckboxGroup(
                dmc.Stack([dmc.Checkbox(label=f, value=str(i))
                           for i, f in enumerate(S.RED_FLAGS)], gap=6),
                id="f-redflags", value=[]),
        ], icon_name="tabler:flag-exclamation", color="#8f1f2f"),

        # ── Immediate assessment ──
        section("Immediate Assessment — Step 1: Observable Signs", [
            dmc.CheckboxGroup(
                dmc.Group([dmc.Checkbox(label="Witnessed", value="witnessed"),
                           dmc.Checkbox(label="Observed on Video", value="video")]),
                id="f-obs-context", value=[], mb="sm"),
            qtable([(dmc.Text(sign, size="sm"), yn({"type": "f-obs", "index": i}))
                    for i, sign in enumerate(S.OBSERVABLE_SIGNS)]),
        ], icon_name="tabler:eye"),

        section("Immediate Assessment — Step 2: Glasgow Coma Scale", [
            dmc.Grid([
                col(dmc.Select(id="f-gcs-e", label="Best Eye Response (E)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_EYE])),
                col(dmc.Select(id="f-gcs-v", label="Best Verbal Response (V)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_VERBAL])),
                col(dmc.Select(id="f-gcs-m", label="Best Motor Response (M)",
                               data=[{"label": f"{v} — {lbl}", "value": str(v)}
                                     for lbl, v in S.GCS_MOTOR])),
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
            dmc.TextInput(id="f-ocular-desc",
                          label="If extraocular movements abnormal, describe:", mt="xs"),
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
                col(dmc.Textarea(id="f-bg-notes", label="Notes", autosize=True,
                                 minRows=2), span=6),
                col(dmc.Textarea(id="f-medications", label="Current medications",
                                 autosize=True, minRows=2), span=6),
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
            dmc.Textarea(id="f-pct-why", label="If not 100%, why?", autosize=True,
                         minRows=2, mt="xs"),
        ], icon_name="tabler:mood-sick"),

        section("Off-Field — Step 3: Cognitive Screening — Orientation", [
            qtable([(dmc.Text(q, size="sm"), zero_one({"type": "f-orient", "index": i}))
                    for i, q in enumerate(S.ORIENTATION_QUESTIONS)]),
        ], icon_name="tabler:compass"),

        section("Off-Field — Step 3: Immediate Memory (3 trials, 1 word/second)", [
            dmc.Grid([
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=k, value=k) for k in ("A", "B", "C")]),
                    id="f-wordlist", value="A", label="Word list")),
                col(dmc.TextInput(id="f-im-time", label="Time last trial completed",
                                  placeholder="HH:MM"), span=3),
            ], gutter="sm", mb="xs"),
            dmc.Box(id="im-grid"),
        ], icon_name="tabler:list-numbers"),

        section("Off-Field — Step 3: Concentration — Digits Backward & Months in Reverse", [
            dmc.RadioGroup(
                dmc.Group([dmc.Radio(label=k, value=k) for k in ("A", "B", "C")]),
                id="f-digitlist", value="A", label="Digit list", mb="xs"),
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
                    id="f-foot", value=None, label="Foot tested"), span=3),
                col(dmc.TextInput(id="f-surface", label="Testing surface")),
                col(dmc.TextInput(id="f-footwear", label="Footwear")),
            ], gutter="sm", mb="xs"),
            dmc.Group([num_input(f"f-bess-{key}", f"{lbl} (of 10)", maxv=10)
                       for key, lbl in S.MBESS_STANCES], gap="md"),
            dmc.Text(id="bess-total-display", fw=600, mt="xs"),
            dmc.Divider(my="sm"),
            dmc.Switch(id="f-foam-toggle", checked=False,
                       label="Add optional foam-surface mBESS"),
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
            dmc.TextInput(id="f-tg-incomplete",
                          label="Any trials not completed? Why?", mt="xs"),
        ], icon_name="tabler:walk"),

        section("Off-Field — Step 5: Delayed Recall (≥ 5 min after Immediate Memory)", [
            dmc.TextInput(id="f-dr-time", label="Time started", placeholder="HH:MM",
                          w=160, mb="xs"),
            dmc.Box(id="dr-grid"),
        ], icon_name="tabler:clock-pause"),

        section("Step 6: Decision & Attestation", [
            dmc.Grid([
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v) for v in ("Normal", "Abnormal")]),
                    id="f-neuro", value=None, label="Neurological exam (acute evaluation)")),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v)
                               for v in ("Yes", "No", "Not applicable")]),
                    id="f-different", value=None, label="Different from usual self?")),
                col(dmc.RadioGroup(
                    dmc.Group([dmc.Radio(label=v, value=v)
                               for v in ("Yes", "No", "Deferred")]),
                    id="f-diagnosed", value=None, label="Concussion diagnosed?")),
            ], gutter="sm", mb="xs"),
            dmc.Textarea(id="f-notes", label="Additional clinical notes",
                         autosize=True, minRows=3),
            dmc.Divider(my="sm"),
            dmc.Text("I am an HCP and I have personally administered or supervised the "
                     "administration of this SCAT6.", size="sm", fs="italic", mb="xs"),
            dmc.Grid([
                col(dmc.TextInput(id="f-examiner", label="Examiner (HCP) name")),
                col(dmc.TextInput(id="f-examiner-title", label="Title / speciality")),
                col(dmc.TextInput(id="f-examiner-license", label="Registration / license #")),
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
                id="f-push-opts", value=["pdf", "csv"], mb="sm"),
            dmc.Button("Save Assessment", id="btn-save", color="green", size="md",
                       leftSection=icon("tabler:device-floppy")),
            dcc.Loading(dmc.Box(id="save-status", mt="sm"), type="circle"),
        ], icon_name="tabler:sum", color="#1d5b3c"),
    ])

def history_layout():
    return dmc.Box([
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
                style_cell={"padding": "8px", "fontSize": 13, "textAlign": "left",
                            "fontFamily": "inherit"},
            ),
            dmc.Group([
                dmc.Select(id="hist-assess-dd", placeholder="Pick an assessment…",
                           searchable=True, clearable=True, w=320),
                dmc.Button("Download PDF", id="btn-hist-pdf", color="blue",
                           leftSection=icon("tabler:file-type-pdf")),
                dmc.Button("Download history CSV", id="btn-hist-csv", variant="light",
                           leftSection=icon("tabler:file-type-csv")),
                dmc.Button("Push to Juvonno", id="btn-hist-push", color="green",
                           leftSection=icon("tabler:cloud-upload")),
            ], gap="sm", mt="sm"),
            dcc.Loading(dmc.Box(id="hist-status", mt="sm"), type="circle"),
        ], icon_name="tabler:database"),
        section("Serial Comparison (SCAT6 Step 6 domains across assessments)", [
            dmc.Box(id="hist-compare"),
        ], icon_name="tabler:chart-line"),
        section("Athlete Documents in Juvonno (pull)", [
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

        Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

        dmc.Container([
            dmc.Group([icon("tabler:first-aid-kit", width=30, color=NAVY),
                       dmc.Title("SCAT6™ Intake Tool", order=2, c=NAVY)],
                      gap="xs", mt="md"),
            dmc.Text("Sport Concussion Assessment Tool 6 — for use by Health Care "
                     "Professionals. Athletes are loaded from Juvonno; completed assessments "
                     "are saved locally and pushed to the athlete's Juvonno documents.",
                     size="sm", c="dimmed", mb="md"),
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
        return err_alert(f"Local save failed: {e}"), no_update, no_update

    push_opts = push_opts or []
    push_errors = []
    try:
        msgs += push_to_juvonno(a, scores, "pdf" in push_opts, "csv" in push_opts)
    except Exception as e:
        traceback.print_exc()
        push_errors.append(f"Juvonno push failed: {e}")

    pdf_bytes = build_scat6_pdf(a, scores)
    dl = dcc.send_bytes(lambda b: b.write(pdf_bytes), _pdf_name(a))

    alerts = [ok_alert([dmc.Text(m, size="sm") for m in msgs])]
    if push_errors:
        alerts.append(warn_alert([dmc.Text(m, size="sm") for m in push_errors]))
    return dmc.Stack(alerts, gap="xs"), dl, (hist_tick or 0) + 1

# ───────────────────────── History tab ─────────────────────────
@app.callback(
    Output("hist-table", "data"), Output("hist-assess-dd", "data"),
    Output("hist-compare", "children"),
    Input("history-refresh", "data"), Input("athlete-dd", "value"))
def refresh_history(_tick, athlete_id):
    if not athlete_id:
        return [], [], dmc.Text("Select an athlete to see saved assessments.",
                                size="sm", c="dimmed")
    rows = store.list_assessments(int(athlete_id))
    opts = [{"label": f"#{r['id']} — {r['date_of_examination']} ({r['assessment_type']})",
             "value": str(r["id"])} for r in rows]

    if not rows:
        compare = dmc.Text("No saved assessments yet.", size="sm", c="dimmed")
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
            style_header={"fontWeight": "600", "backgroundColor": NAVY,
                          "color": "white", "whiteSpace": "pre-line"},
            style_cell={"padding": "8px", "fontSize": 13, "textAlign": "center",
                        "fontFamily": "inherit"},
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
        return no_update, err_alert("Assessment not found.")
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
        return err_alert("Assessment not found.")
    try:
        msgs = push_to_juvonno(rec["assessment"], rec["scores"], True, True)
        return ok_alert([dmc.Text(m, size="sm") for m in msgs])
    except Exception as e:
        traceback.print_exc()
        return err_alert(f"Push failed: {e}")

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
