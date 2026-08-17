# app.py — SCAT6 intake tool for practitioners.
#
# Same OAuth2 flow as before (apps.csipacific.ca via dash-auth-external) and the
# same Juvonno branch → group → athlete cascade. UI: dash-mantine-components +
# dash-iconify; the original bootstrap Navbar and Footer (layout/) are unchanged.
#
# Data flow:
#   • Juvonno is the source of truth: each assessment is one timestamped PDF on
#     the athlete's chart, with the full data embedded inside the PDF metadata.
#     The History tab lists those PDFs and scrapes them back into tables.
#   • Local SQLite is the offline safety net: every assessment saves locally
#     first, then the PDF pushes to Juvonno. If the push fails (no internet /
#     API down), it stays queued and is retried automatically, or via "Sync now".
#   • All form inputs persist in the browser (localStorage), so a refresh or
#     dropped connection mid-intake loses nothing.
import io, os, base64, functools, traceback
from datetime import date, datetime

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
from scat6_pdf import build_scat6_pdf, extract_assessment_from_pdf
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
def _now_local():
    """Local (Pacific) datetime for exam timestamps."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        return datetime.now()

def _pdf_name(assessment: dict) -> str:
    d = (assessment.get("date_of_examination") or "nodate").replace("/", "-")
    t = (assessment.get("time_of_examination") or "").replace(":", "")
    t_part = f"_{t}" if t else ""
    return f"SCAT6_{d}{t_part}_athlete{assessment.get('athlete_id', '')}.pdf"

def push_to_juvonno(assessment: dict, scores: dict) -> tuple:
    """Upload the assessment PDF (with embedded data) to the athlete's Juvonno
    documents. Returns (msgs, errors, failed_parts)."""
    msgs, errors, failed = [], [], []
    cid = int(assessment["athlete_id"])
    exam_date = assessment.get("date_of_examination") or date.today().isoformat()
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
    return msgs, errors, failed

# ───────────────────────── History from Juvonno PDFs ─────────────────────────
def _is_scat6_pdf(doc: dict) -> bool:
    name = str(doc.get("name") or doc.get("filename") or doc.get("file_name") or "").lower()
    return name.startswith("scat6_") and not name.endswith(".csv")

@functools.lru_cache(maxsize=1024)
def _scraped_doc(cid: int, doc_id: int):
    """Download one SCAT6 PDF from Juvonno and recover its data (cached —
    documents in Juvonno are immutable)."""
    try:
        _, raw = juv.download_customer_document(int(cid), int(doc_id))
        return extract_assessment_from_pdf(raw)
    except Exception as e:
        print(f"scrape doc {doc_id}: {e}")
        return None

def fetch_history_records(cid: int):
    """All SCAT6 assessments for this athlete, reconstructed from the PDFs
    stored in Juvonno. Returns (records, n_docs, n_unreadable); each record is
    {'assessment':…, 'scores':…, 'doc_id':…, 'doc_name':…}."""
    docs = [d for d in juv.list_customer_documents(int(cid)) if _is_scat6_pdf(d)]
    records, unreadable = [], 0
    for d in docs:
        if d.get("id") is None:
            continue
        rec = _scraped_doc(int(cid), int(d["id"]))
        if rec:
            rec = dict(rec)
            rec["doc_id"] = int(d["id"])
            rec["doc_name"] = d.get("name") or d.get("filename") or ""
            records.append(rec)
        else:
            unreadable += 1
    def _key(r):
        a = r.get("assessment", {})
        return (str(a.get("date_of_examination") or ""),
                str(a.get("time_of_examination") or ""), r.get("doc_id", 0))
    records.sort(key=_key)
    return records, len(docs), unreadable

def history_csv_bytes(records) -> bytes:
    """Flatten Juvonno-scraped records into one longitudinal CSV."""
    rows = [S.to_flat_record(r.get("assessment", {}), r.get("scores", {}))
            for r in records]
    df = pd.DataFrame(rows, columns=S.CSV_COLUMNS)
    return df.to_csv(index=False).encode("utf-8")

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
        try:
            msgs, errors, failed = push_to_juvonno(a, rec["scores"])
        except Exception as e:
            errors, failed = [str(e)], ["pdf"]
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

def m_table(header_cells, body_rows, navy_header=False, max_height=None,
            min_width=640, first_col_bold=False, center_body=False):
    """Read-only Mantine-styled table (dmc.Table) with scroll + sticky header."""
    hstyle = ({"background": NAVY, "color": "white"} if navy_header
              else {"background": "var(--mantine-color-gray-1)"})
    thead = dmc.TableThead(dmc.TableTr([dmc.TableTh(c, style=hstyle)
                                        for c in header_cells]))
    trs = []
    for r in body_rows:
        tds = []
        for j, cval in enumerate(r):
            if not isinstance(cval, (dict,)) and not hasattr(cval, "to_plotly_json"):
                cval = dmc.Text("—" if cval in (None, "") else str(cval), size="sm",
                                fw=600 if (j == 0 and first_col_bold) else None)
            style = {"textAlign": "center"} if (center_body and j > 0) else None
            tds.append(dmc.TableTd(cval, style=style))
        trs.append(dmc.TableTr(tds))
    table = dmc.Table([thead, dmc.TableTbody(trs)], striped=True,
                      highlightOnHover=True, withTableBorder=True,
                      verticalSpacing="xs", horizontalSpacing="md",
                      stickyHeader=bool(max_height))
    return dmc.TableScrollContainer(table, minWidth=min_width,
                                    maxHeight=max_height, type="native")

_TYPE_BADGE = {"baseline": ("Baseline", "teal"), "post_injury": ("Post-injury", "orange")}

def type_badge(atype):
    label, color = _TYPE_BADGE.get(str(atype or "").strip(),
                                   ((str(atype).replace("_", "-").title() or "—")
                                    if atype else "—", "gray"))
    return dmc.Badge(label, color=color, variant="light", size="sm")

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
                # Athlete identity fields intentionally do NOT persist — they
                # stay empty until an athlete is selected from Juvonno.
                col(dmc.TextInput(id="f-name", label="Athlete name",
                                  placeholder="Select an athlete above…")),
                col(dmc.TextInput(id="f-idnum", label="ID number (Juvonno)",
                                  disabled=True), span=2),
                col(dmc.DatePickerInput(id="f-dob", label="Date of birth",
                                        clearable=True,
                                        valueFormat="YYYY-MM-DD",
                                        leftSection=icon("tabler:calendar", width=16)),
                    span=3),
                col(dmc.Select(id="f-sex", label="Sex",
                               data=["Male", "Female", "Prefer Not To Say", "Other"]),
                    span=3),
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
                col(dmc.DatePickerInput(id="f-injury-date", label="Date of injury",
                                        clearable=True, valueFormat="YYYY-MM-DD",
                                        leftSection=icon("tabler:calendar", width=16),
                                        **PERSIST), span=2),
                col(dmc.TimePicker(id="f-injury-time", label="Time of injury",
                                   withDropdown=True, clearable=True,
                                   leftSection=icon("tabler:clock", width=16),
                                   **PERSIST), span=1),
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
                col(dmc.DatePickerInput(id="f-recent-conc",
                                        label="Most recent concussion",
                                        clearable=True, valueFormat="YYYY-MM-DD",
                                        leftSection=icon("tabler:calendar", width=16),
                                        **PERSIST), span=3),
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
                col(dmc.Select(id="f-pct-normal", label=S.PERCENT_NORMAL_QUESTION,
                               data=[{"label": f"{v}%", "value": str(v)}
                                     for v in range(100, -1, -5)],
                               clearable=True, searchable=True, w=180, **PERSIST)),
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
                col(dmc.TimePicker(id="f-im-time", label="Time last trial completed",
                                   withDropdown=True, clearable=True,
                                   leftSection=icon("tabler:clock", width=16),
                                   **PERSIST), span=3),
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
            dmc.TimePicker(id="f-dr-time", label="Time started", withDropdown=True,
                           clearable=True, w=180, mb="xs",
                           leftSection=icon("tabler:clock", width=16), **PERSIST),
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
            dmc.Checkbox(id="f-push-pdf", checked=True, mb="sm",
                         label="Upload PDF to Juvonno (the PDF is the athlete's "
                               "permanent SCAT6 record)", **PERSIST),
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
        section("SCAT6 History (scraped from Juvonno PDFs)", [
            dmc.Text("Each saved SCAT6 is a timestamped PDF on the athlete's Juvonno "
                     "chart with the full assessment data embedded inside it. This "
                     "table is rebuilt by pulling those PDFs from Juvonno.",
                     size="sm", c="dimmed", mb="sm"),
            dmc.Group([
                dmc.Button("Refresh from Juvonno", id="btn-hist-refresh", variant="light",
                           leftSection=icon("tabler:refresh")),
                dmc.Button("Export history CSV (from PDFs)", id="btn-hist-csv", color="blue",
                           leftSection=icon("tabler:file-type-csv")),
            ], gap="sm", mb="sm"),
            dcc.Loading(dmc.Box(id="hist-table-wrap"), type="circle"),
            dmc.Box(id="hist-status", mt="sm"),
        ], icon_name="tabler:cloud-download"),
        section("Serial Comparison (SCAT6 Step 6 domains across assessments)", [
            dcc.Loading(dmc.Box(id="hist-compare"), type="circle"),
        ], icon_name="tabler:chart-line"),
        section("Athlete Documents in Juvonno", [
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
    # Proper mobile viewport; input font sizing that stops iOS focus-zoom is in
    # assets/custom.css.
    meta_tags=[{"name": "viewport",
                "content": "width=device-width, initial-scale=1"}],
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
        return {}, "", "", "", None, None, no_update, (hist_tick or 0) + 1
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
            demo.get("dob") or None, sex, examiner, (hist_tick or 0) + 1)

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
    State("f-push-pdf", "checked"),
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
                    push_pdf, hist_tick):
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
        "percent_normal": (int(pct_normal) if pct_normal not in (None, "") else None),
        "percent_normal_why": pct_why or "",
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
        "time_of_examination": _now_local().strftime("%H:%M"),
    })

    scores = S.compute_all_scores(a)

    # 1) Always save locally first — this is the offline safety net.
    try:
        new_id = store.save_assessment(a, scores, synced=(not push_pdf),
                                       pending_parts="pdf" if push_pdf else "")
        a["assessment_id"] = new_id
    except Exception as e:
        traceback.print_exc()
        return err_alert(f"Local save failed: {e}"), no_update, no_update
    msgs = [f"Assessment #{new_id} saved locally."]

    # 2) Then try to push to Juvonno; failures stay queued for auto-retry.
    queued = []
    if push_pdf:
        try:
            push_msgs, push_errs, failed = push_to_juvonno(a, scores)
        except Exception as e:
            traceback.print_exc()
            push_msgs, push_errs, failed = [], [str(e)], ["pdf"]
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
        None, None, "", None, "",  # injury date/time, time since, hand, sport
        "", None, "", "",          # concussion history (count, recent date, days, symptoms)
        "", "", "",                # ocular desc, bg notes, medications
        None, None, None, "",      # worse phys/ment, pct, why
        "A", None, "A",            # word list, im time, digit list
        None, "", "", "", None,    # foot, surface, footwear, tg incomplete, dr time
        None, None, None, "",      # decision fields, notes
        "", "",                    # examiner title / license
        date.today().isoformat(),  # exam date
        demo.get("name", ""),      # re-prefill from selected athlete
        demo.get("dob") or None,
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
        records, n_docs, unreadable = fetch_history_records(int(athlete_id))
    except Exception as e:
        traceback.print_exc()
        return (dmc.Text("Could not reach Juvonno.", size="sm", c="dimmed"),
                dmc.Text("Could not reach Juvonno.", size="sm", c="dimmed"),
                err_alert(f"Failed to load history from Juvonno: {e}"))
    if not records:
        note = dmc.Text("No SCAT6 PDFs found on this athlete's Juvonno chart yet.",
                        size="sm", c="dimmed")
        status = ""
        if unreadable:
            status = warn_alert(f"{unreadable} SCAT6 PDF(s) could not be read "
                                f"(created before data embedding).")
        return note, dmc.Text("No assessments in Juvonno yet.", size="sm", c="dimmed"), status

    def _when(rec, i):
        a = rec.get("assessment", {})
        d = a.get("date_of_examination") or f"Assessment {i + 1}"
        t = a.get("time_of_examination") or ""
        return f"{d} {t}".strip()

    body = []
    for i, rec in enumerate(records):
        merged = {**rec.get("assessment", {}), **rec.get("scores", {})}
        row = [dmc.Text(_when(rec, i), size="sm", fw=500),
               type_badge(merged.get("assessment_type"))]
        for _, key in HIST_TABLE_COLS[2:]:
            v = merged.get(key)
            row.append(dmc.Text("—" if v in (None, "") else str(v), size="sm"))
        body.append(row)
    table = m_table([n for n, _ in HIST_TABLE_COLS], body, max_height=320)

    # Serial comparison from the scraped PDFs
    headers = [dmc.Text("Domain", fw=600, size="sm", c="white")]
    for i, rec in enumerate(records):
        headers.append(dmc.Stack([
            dmc.Text(_when(rec, i), size="sm", fw=600, c="white"),
            type_badge(rec.get("assessment", {}).get("assessment_type")),
        ], gap=4, align="center"))
    comp_rows = []
    for label, key, mx in S.DECISION_DOMAINS:
        row = [dmc.Text(label + (f" (of {mx})" if mx else ""), size="sm", fw=500)]
        for rec in records:
            merged = {**rec.get("scores", {}),
                      **{k: v for k, v in rec.get("assessment", {}).items()
                         if k == "neuro_exam"}}
            v = merged.get(key)
            row.append(dmc.Text("—" if v in (None, "") else str(v), size="sm",
                                ta="center"))
        comp_rows.append(row)
    compare = m_table(headers, comp_rows, navy_header=True, center_body=True,
                      min_width=520 + 150 * len(records))

    msg = f"Loaded {len(records)} assessment(s) from {n_docs} SCAT6 PDF(s) in Juvonno."
    partial = sum(1 for r in records if r.get("partial"))
    if partial:
        msg += f" {partial} older PDF(s) were text-scraped (scores only)."
    status = dmc.Text(msg, size="sm", c="dimmed")
    if unreadable:
        status = dmc.Stack([status,
                            warn_alert(f"{unreadable} SCAT6 PDF(s) could not be read.")],
                           gap="xs")
    return table, compare, status

@app.callback(Output("dl-csv", "data"),
              Output("hist-status", "children", allow_duplicate=True),
              Input("btn-hist-csv", "n_clicks"),
              State("athlete-dd", "value"), prevent_initial_call=True)
def hist_download_csv(n, athlete_id):
    if not n or not athlete_id:
        raise PreventUpdate
    cid = int(athlete_id)
    try:
        records, _n_docs, _unreadable = fetch_history_records(cid)
        if not records:
            return no_update, warn_alert("No SCAT6 PDFs in Juvonno for this athlete yet.")
        csv_bytes = history_csv_bytes(records)
        return (dcc.send_bytes(lambda b: b.write(csv_bytes),
                               f"SCAT6_History_{cid}.csv"), no_update)
    except Exception as e:
        traceback.print_exc()
        return no_update, err_alert(f"Building history CSV from Juvonno failed: {e}")

# ───────────────────────── Offline queue / sync ─────────────────────────
def _pending_children():
    pending = store.list_unsynced()
    if not pending:
        return dmc.Group([icon("tabler:circle-check", color="green"),
                          dmc.Text("Nothing pending — all assessments are uploaded.",
                                   size="sm", c="dimmed")], gap="xs")
    body = [[p["id"], p["athlete_name"], p["date_of_examination"],
             dmc.Badge(p.get("pending_parts") or "pdf", color="orange",
                       variant="light", size="sm"),
             p["created_at"]] for p in pending]
    return m_table(["ID", "Athlete", "Date", "Waiting to upload", "Saved at (UTC)"],
                   body, max_height=240)

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
    body = [[r["ID"],
             dmc.Group([icon("tabler:file-type-pdf" if str(r["Name"]).lower().endswith(".pdf")
                             else "tabler:file", width=16, color=NAVY),
                        dmc.Text(str(r["Name"]), size="sm", fw=500)], gap=6),
             r["Date"], r["Description"]] for r in rows]
    table = m_table(["ID", "Name", "Date", "Description"], body, max_height=280)
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
