# training_dashboard.py — dashboard content + callbacks
from __future__ import annotations
import os, sqlite3, requests, functools, traceback
from datetime import datetime
from typing import Dict, List, Union, Iterable, Tuple, Optional

import numpy as np
import pandas as pd

import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, dash_table, Input, Output, State, no_update

try:
    import plotly_calplot as pc
    PLOTLYCAL_AVAILABLE = True
except ImportError:
    PLOTLYCAL_AVAILABLE = False

import plotly.graph_objects as go

# ────────── API config ──────────
API_KEY = os.getenv("JUV_API_KEY", "6e94ea1a2a2e2742b6d8511957ffee10ee574d5e")
BASE    = "https://csipacific.juvonno.com/api"
HEADERS = {"accept": "application/json"}

def _get(path: str, **params):
    params.setdefault("api_key", API_KEY)
    r = requests.get(f"{BASE}/{path.lstrip('/')}", params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

# ────────── SQLite persistence for comments ──────────
DB_PATH = os.path.join(os.path.dirname(__file__), "comments.db")

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_label TEXT,
            date TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def db_add_comment(customer_id: int, customer_label: str, date_str: str, comment: str):
    conn = _db()
    conn.execute(
        "INSERT INTO comments(customer_id, customer_label, date, comment, created_at) VALUES (?,?,?,?,?)",
        (int(customer_id) if customer_id is not None else None,
         customer_label or "", date_str, comment, datetime.utcnow().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()

def db_list_comments(customer_ids: Iterable[int] | None) -> List[Dict]:
    conn = _db(); cur = conn.cursor()
    if customer_ids:
        vals = [int(x) for x in customer_ids]
        q = ",".join("?" for _ in vals)
        cur.execute(f"""
          SELECT date, comment, customer_label, customer_id
          FROM comments
          WHERE customer_id IN ({q})
          ORDER BY date ASC, id ASC
        """, vals)
    else:
        cur.execute("SELECT date, comment, customer_label, customer_id FROM comments ORDER BY date ASC, id ASC")
    rows = cur.fetchall(); conn.close()
    return [{"Date": r[0], "Comment": r[1], "Athlete": r[2], "Athlete ID": r[3]} for r in rows]

# ────────── Customers / groups ──────────
def _norm(s: str) -> str:
    return (s or "").strip().lower()

def fetch_customers_full() -> Dict[int, Dict]:
    out, page = [], 1
    while True:
        js = _get("customers/list", include="groups", page=page, count=100, status="ACTIVE")
        rows = js.get("list", js)
        if not rows: break
        out.extend(rows)
        if len(rows) < 100: break
        page += 1
    return {c["id"]: c for c in out if c.get("id")}

CUSTOMERS = fetch_customers_full()

def groups_of(cust: Dict) -> List[str]:
    src = cust.get("groups") if "groups" in cust else cust.get("group")
    names: List[str] = []
    if isinstance(src, list):
        for it in src:
            if isinstance(it, str): names.append(_norm(it))
            elif isinstance(it, dict) and it.get("name"): names.append(_norm(it["name"]))
    elif isinstance(src, dict) and src.get("name"): names.append(_norm(src["name"]))
    elif isinstance(src, str): names.append(_norm(src))
    return names

CID_TO_GROUPS = {cid: groups_of(c) for cid, c in CUSTOMERS.items()}
ALL_GROUPS    = sorted({g for lst in CID_TO_GROUPS.values() for g in lst})
GROUP_OPTS    = [{"label": g.title(), "value": g} for g in ALL_GROUPS]

# ────────── Appointments (branch 1) ──────────
def fetch_branch_appts(branch=1) -> List[Dict]:
    rows, page = [], 1
    while True:
        js = _get(f"appointments/list/{branch}", start_date="2000-01-01", status="all", page=page, count=100)
        block = js.get("list", js)
        if not block: break
        rows.extend(block)
        if len(block) < 100: break
        page += 1
    return rows

BRANCH_APPTS     = fetch_branch_appts(1)
CID_TO_APPTS: Dict[int, List[Dict]] = {}
for ap in BRANCH_APPTS:
    cust = ap.get("customer", {})
    if isinstance(cust, dict) and cust.get("id"):
        CID_TO_APPTS.setdefault(cust["id"], []).append(ap)

# ────────── Encounters / Training Status ──────────
FLAGS = [{}, {"include": "fields"}, {"include": "answers"}, {"full": 1}]

@functools.lru_cache(maxsize=1024)
def fetch_encounter(eid: int) -> Dict:
    for root in (f"encounters/{eid}", f"encounters/charts/{eid}", f"encounters/intakes/{eid}"):
        for f in FLAGS:
            try:
                js = _get(root, **f)
                return js.get("encounter", js) if isinstance(js, dict) else js
            except requests.HTTPError as e:
                if e.response.status_code in (400, 404): continue
                raise
    return {}

def extract_training_status(enc_payload: Union[Dict, List]) -> str:
    valid = {
        "Full participation without injury/illness/other health problems",
        "Full participation with injury/illness/other health problems",
        "Reduced participation with injury/illness/other health problems",
        "No participation due to injury/illness/other health problems",
        "No participation unrelated to injury/illness/other health problems",
    }
    stack: List[Union[Dict, List]] = [enc_payload] if isinstance(enc_payload, (dict, list)) else []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node_id   = str(node.get("id", "")).lower()
            raw_val   = str(node.get("value", "")).strip()
            node_val  = " ".join(raw_val.split())
            node_name = " ".join((node.get("name") or node.get("label") or node.get("title") or "").split()).lower()
            if node_id == "id_select_2" and node_val in valid: return node_val
            if "training status" in node_name and node_val in valid: return node_val
            for v in node.values():
                if isinstance(v, (dict, list)): stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return ""

@functools.lru_cache(maxsize=2048)
def encounter_ids_for_appt(aid: int) -> List[int]:
    try:
        js = _get("encounters/appointment", appointment_id=aid)
    except requests.HTTPError:
        return []
    ids: List[int] = []
    for key in ("charts", "intakes"):
        arr = js.get(key)
        if isinstance(arr, list):
            for val in arr:
                try: ids.append(int(val))
                except (ValueError, TypeError): pass
    return ids

# ── Appointment-level complaints ──
@functools.lru_cache(maxsize=4096)
def list_complaints_for_appt(aid: int) -> List[Dict]:
    try:
        js = _get(f"appointments/{aid}/complaints")
    except requests.HTTPError:
        return []
    if isinstance(js, list): return js
    if isinstance(js, dict) and isinstance(js.get("list"), list): return js["list"]
    return []

# ── Athlete-level complaints (merge) ──
def _fmt_date(val) -> str:
    if not val: return ""
    try: return pd.to_datetime(str(val)).strftime("%Y-%m-%d")
    except Exception: return str(val)

def _extract_name(rec: Dict) -> str:
    for k in ("name", "title", "problem", "injury", "body_part", "complaint"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    return ""

def _norm_complaint_fields(rec: Dict) -> Dict:
    title    = _extract_name(rec)
    onset    = (rec.get("onset_date") or rec.get("onsetDate") or rec.get("onset") or
                rec.get("start_date") or rec.get("date") or rec.get("injury_onset") or "")
    priority = (rec.get("priority") or rec.get("priority_name") or rec.get("priorityName") or
                rec.get("priority_level") or "")
    status   = (rec.get("status") or rec.get("status_name") or rec.get("statusName") or
                rec.get("state") or rec.get("complaint_status") or "")
    cid      = rec.get("id") or rec.get("complaint_id") or rec.get("complaintId") or None
    return {"Id": cid, "Title": title, "Onset": _fmt_date(onset),
            "Priority": str(priority).strip(), "Status": (str(status).strip() or "—")}

@functools.lru_cache(maxsize=512)
def fetch_customer_complaints(customer_id: int) -> List[Dict]:
    out: List[Dict] = []

    # 1) Customer-level endpoints
    try:
        js = _get(f"customers/{customer_id}/complaints", include="full", page=1, count=100)
        block = js.get("list", js)
        if isinstance(block, list): out.extend(block)
        page = 2
        while True:
            js2 = _get(f"customers/{customer_id}/complaints", include="full", page=page, count=100)
            blk = js2.get("list", js2)
            if not isinstance(blk, list) or not blk: break
            out.extend(blk)
            if len(blk) < 100: break
            page += 1
    except requests.HTTPError:
        pass

    try:
        page = 1
        while True:
            js = _get("complaints/list", customer_id=customer_id, page=page, count=100)
            block = js.get("list", js)
            if not isinstance(block, list) or not block: break
            out.extend(block)
            if len(block) < 100: break
            page += 1
    except requests.HTTPError:
        pass

    # 2) Appointment-level + 3) Inline appointment complaint
    for ap in CID_TO_APPTS.get(customer_id, []):
        for rec in list_complaints_for_appt(ap.get("id")):
            out.append(rec)
        comp_inline = ap.get("complaint")
        if isinstance(comp_inline, dict):
            name = _extract_name(comp_inline)
            if name: out.append({"name": name, "id": comp_inline.get("id")})

    # Normalize + dedupe (by Id or Title casefold)
    normed = [_norm_complaint_fields(r) for r in out if isinstance(r, dict)]
    dedup: Dict[Tuple, Dict] = {}
    for r in normed:
        key = (r.get("Id") or r.get("Title").casefold(),)
        if key in dedup:
            prev = dedup[key]
            for f in ("Priority", "Status", "Onset", "Title"):
                if (not prev.get(f)) and r.get(f): prev[f] = r[f]
        else:
            dedup[key] = r
    def _sort_key(d):
        try: return (0, pd.to_datetime(d["Onset"]))
        except Exception: return (1, pd.Timestamp.min)
    return sorted(dedup.values(), key=_sort_key, reverse=True)

# ────────── Pastel palette (table + calendar) ──────────
STATUS_ORDER = [
    "Full participation without injury/illness/other health problems",
    "Full participation with injury/illness/other health problems",
    "Reduced participation with injury/illness/other health problems",
    "No participation due to injury/illness/other health problems",
    "No participation unrelated to injury/illness/other health problems",
]
PASTEL_COLOR = {
    STATUS_ORDER[0]: "#BDE7BD",  # pastel green
    STATUS_ORDER[1]: "#D6F2C6",  # lighter green
    STATUS_ORDER[2]: "#FFD9A8",  # pastel orange
    STATUS_ORDER[3]: "#F5B1B1",  # pastel red
    STATUS_ORDER[4]: "#D8C6F0",  # pastel purple
}
COLOR_LIST = [PASTEL_COLOR[s] for s in STATUS_ORDER]
STATUS_CODE = {s: i for i, s in enumerate(STATUS_ORDER)}

def tidy_date_str(raw) -> str:
    if isinstance(raw, dict): raw = raw.get("start", "")
    raw = raw or ""
    return raw.split("T", 1)[0] if isinstance(raw, str) else str(raw)

def dot_html(hex_color: str, size: int = 10, mr: int = 8) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )

def discrete_colorscale_from_hexes(hexes: List[str]) -> list:
    n = len(hexes)
    if n == 0: return []
    if n == 1: return [[0.0, hexes[0]], [1.0, hexes[0]]]
    eps = 1e-6
    stops = [[0.0, hexes[0]]]
    for i in range(1, n):
        p = i / (n - 1)
        stops.append([max(p - eps, 0.0), hexes[i-1]])
        stops.append([p, hexes[i]])
    stops[-1][0] = 1.0
    return stops

# ────────── Clickable card headers (plus/minus) ──────────
LIGHT_GREY = "#f2f3f5"

def clickable_header(title: str, click_id: str, symbol_id: str, header_id: str):
    """Light grey CardHeader with full-width clickable inner div and +/- symbol."""
    return dbc.CardHeader(
        html.Div(
            [html.Span(id=symbol_id, children="−", className="me-2"),
             html.Span(title, className="fw-semibold")],
            id=click_id, n_clicks=0,
            style={"cursor":"pointer","userSelect":"none","padding":"0.75rem 1rem","width":"100%"},
            className="d-flex align-items-center",
        ),
        id=header_id,
        className="bg-light",
        style={"backgroundColor": LIGHT_GREY, "borderBottom": "1px solid #e9ecef"},
    )

CARD_STYLE = {"overflow": "hidden", "border": "1px solid #e9ecef", "borderRadius": "0.5rem", "backgroundColor": "white"}

# ────────── Public layout builder ──────────
def layout_body():
    return dbc.Container([
        html.H3("Training Status", className="mt-1"),

        # Filters / selection
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="grp", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s)…"), md=6),
            dbc.Col(dbc.Button("Load", id="go", color="primary", className="w-100"), md=2),
        ], className="g-2"),
        html.Hr(),
        html.Div(id="customer-checklist-container"),
        html.Br(),

        # 0) Athlete Summary (open)
        dbc.Card([
            clickable_header("Athlete Summary", "hdr-summary", "sym-summary", "hdr-summary-container"),
            dbc.Collapse(dbc.CardBody(html.Div(id="athlete-summary-container", style={"paddingTop":"0.5rem"})),
                         id="col-summary", is_open=True)
        ], className="mb-3", style=CARD_STYLE),

        # 1) Training-Status Calendar (closed)
        dbc.Card([
            clickable_header("Training-Status Calendar", "hdr-cal", "sym-cal", "hdr-cal-container"),
            dbc.Collapse(dbc.CardBody([
                dcc.Dropdown(id="focus-complaint", placeholder="Focus complaint (optional)…",
                             clearable=True, style={"maxWidth":"420px"}, className="mb-2"),
                html.Div(id="calendar-heatmap-container"),
                html.Div(html.B("Hovered date:"), className="mt-2"),
                html.Div(id="hover-date", style={"fontStyle": "italic"})
            ], style={"paddingTop":"0.5rem"}), id="col-cal", is_open=False)
        ], className="mb-3", style=CARD_STYLE),

        # 2) Comments (open)
        dbc.Card([
            clickable_header("Comments", "hdr-comments", "sym-comments", "hdr-comments-container"),
            dbc.Collapse(dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.DatePickerSingle(id="comment-date", display_format="YYYY-MM-DD"), md=3),
                    dbc.Col(dcc.Dropdown(id="comment-athlete", placeholder="Choose athlete…"), md=3),
                    dbc.Col(dcc.Textarea(id="comment-text",
                                         placeholder="Add a note about the athlete for this date…",
                                         style={"width":"100%","height":"80px"}), md=4),
                    dbc.Col(dbc.Button("Save", id="comment-save", color="success", className="w-100"), md=2),
                ], className="g-2"),
                html.Div(id="comment-hint", className="text-muted", style={"fontSize":"12px", "marginTop":"4px"}),
                html.Hr(),
                dash_table.DataTable(
                    id="comments-table",
                    columns=[{"name":"Date","id":"Date"},{"name":"Comment","id":"Comment"}],
                    data=[],
                    page_size=7,
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
                    style_cell={"padding":"8px","fontSize":13,
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"whiteSpace":"normal","height":"auto","borderBottom":"1px solid #eee"},
                ),
            ], style={"paddingTop":"0.5rem"}), id="col-comments", is_open=True)
        ], className="mb-3", style=CARD_STYLE),

        # 3) Appointments table (closed)
        dbc.Card([
            clickable_header("Appointments", "hdr-table", "sym-table", "hdr-table-container"),
            dbc.Collapse(dbc.CardBody(html.Div(id="appointment-table-container", style={"paddingTop":"0.5rem"})),
                         id="col-table", is_open=False)
        ], className="mb-4", style=CARD_STYLE),

        dcc.Store(id="selected-athletes-map", data={}),
        dbc.Alert(id="msg", is_open=False, duration=0, color="danger"),
    ], fluid=True)

# ────────── Callback registration ──────────
def register_callbacks(app: dash.Dash):

    # Helpers for header toggle
    def _toggle(open_: bool) -> bool: return not open_
    def _sym(open_: bool) -> str:     return "−" if open_ else "+"
    def _hdr_style(open_: bool) -> dict:
        return {"backgroundColor": LIGHT_GREY,
                "borderBottom": "1px solid #e9ecef" if open_ else "0px solid transparent"}

    # Collapsible toggles
    @app.callback(
        Output("col-summary","is_open"),
        Output("sym-summary","children"),
        Output("hdr-summary-container","style"),
        Input("hdr-summary","n_clicks"),
        State("col-summary","is_open"),
        prevent_initial_call=True)
    def toggle_summary(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("col-cal","is_open"),
        Output("sym-cal","children"),
        Output("hdr-cal-container","style"),
        Input("hdr-cal","n_clicks"),
        State("col-cal","is_open"),
        prevent_initial_call=True)
    def toggle_cal(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("col-comments","is_open"),
        Output("sym-comments","children"),
        Output("hdr-comments-container","style"),
        Input("hdr-comments","n_clicks"),
        State("col-comments","is_open"),
        prevent_initial_call=True)
    def toggle_comments(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("col-table","is_open"),
        Output("sym-table","children"),
        Output("hdr-table-container","style"),
        Input("hdr-table","n_clicks"),
        State("col-table","is_open"),
        prevent_initial_call=True)
    def toggle_table(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    # ① Load groups → Customer checklist
    @app.callback(
        Output("customer-checklist-container", "children"),
        Output("msg", "children"), Output("msg", "is_open"),
        Input("go", "n_clicks"),
        State("grp", "value"),
        prevent_initial_call=True,
    )
    def make_customer_checklist(n_clicks, groups_raw):
        if not groups_raw:
            return no_update, "Select at least one group.", True
        targets = {_norm(g) for g in groups_raw}
        matching = [
            {"label": f"{c['first_name']} {c['last_name']} (ID {cid})", "value": cid}
            for cid, c in CUSTOMERS.items()
            if targets & set(CID_TO_GROUPS.get(cid, []))
        ]
        if not matching:
            return html.Div("No patients in those groups."), "", False
        checklist = dbc.Checklist(
            id="cust-buttons", options=matching, value=[],
            inline=False, switch=False, style={"flexDirection": "column"},
        )
        return checklist, "", False

    # ② Selection → Calendar + Table + Complaint Focus
    @app.callback(
        Output("calendar-heatmap-container", "children"),
        Output("appointment-table-container", "children"),
        Output("selected-athletes-map", "data"),
        Output("focus-complaint", "options"),
        Output("focus-complaint", "value"),
        Output("msg", "children", allow_duplicate=True),
        Output("msg", "is_open", allow_duplicate=True),
        Input("cust-buttons", "value"),
        State("focus-complaint", "value"),
        prevent_initial_call=True,
    )
    def show_calendar_and_table(selected_cids, previously_focused):
        try:
            if not selected_cids:
                return "", html.Div("Select one or more customers."), {}, [], None, "", False

            rows = []
            id_to_label = {}
            appointment_complaints: set[str] = set()
            customer_complaints_union: set[str] = set()

            for cid in selected_cids:
                cust = CUSTOMERS.get(cid, {})
                label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()
                id_to_label[cid] = label

                # Customer-level complaints (ensures items like "knee" appear)
                for c in fetch_customer_complaints(cid):
                    n = (c.get("Title") or "").strip()
                    if n: customer_complaints_union.add(n)

                # Appointments + all complaints
                for ap in CID_TO_APPTS.get(cid, []):
                    aid = ap.get("id")
                    date_str = tidy_date_str(ap.get("date"))
                    eids = encounter_ids_for_appt(aid)
                    max_eid = max(eids) if eids else None
                    status = extract_training_status(fetch_encounter(max_eid)) if max_eid else ""

                    names: List[str] = []
                    for rec in list_complaints_for_appt(aid):
                        nm = _extract_name(rec)
                        if nm: names.append(nm)
                    comp_inline = ap.get("complaint")
                    if isinstance(comp_inline, dict):
                        nm = _extract_name(comp_inline)
                        if nm: names.append(nm)

                    names = sorted(set(n.strip() for n in names if n.strip()))
                    if names: appointment_complaints.update(names)

                    rows.append({
                        "Date":            date_str,
                        "Training Status": status,
                        "Complaint Names": "; ".join(names) if names else "",
                    })

            # Build union
            all_names = sorted({n for n in (appointment_complaints | customer_complaints_union) if n})

            if not rows:
                opts = [{"label":"All complaints","value":"__ALL__"}] + [{"label": n, "value": n} for n in all_names]
                return "", html.Div("No appointments found."), id_to_label, opts, "__ALL__", "", False

            df = pd.DataFrame(rows)
            df["Date"] = pd.to_datetime(df["Date"], format="%Y-%m-%d", errors="coerce")
            df = df.dropna(subset=["Date"]).sort_values(["Date"]).reset_index(drop=True)

            # Focus dropdown
            opts = [{"label":"All complaints","value":"__ALL__"}] + [{"label": n, "value": n} for n in all_names]
            focus_value = previously_focused if previously_focused and \
                        (previously_focused == "__ALL__" or previously_focused in all_names) else "__ALL__"

            # Apply focus
            work = df.copy()
            if focus_value and focus_value != "__ALL__":
                mask = work["Complaint Names"].str.contains(
                    rf"(^|;\s*){pd.regex.escape(focus_value)}($|;\s*)", case=False, na=False
                )
                work = work[mask].copy()

            # Table
            def build_status_cell(s: str) -> str:
                col = PASTEL_COLOR.get(s)
                return f"{dot_html(col)}{s}" if col else (s or "")

            work["Status"] = work["Training Status"].apply(build_status_cell)
            table = dash_table.DataTable(
                id="appt-table",
                data=work.assign(Date=work["Date"].dt.strftime("%Y-%m-%d"))[[
                    "Date","Status","Complaint Names"
                ]].rename(columns={"Complaint Names":"Complaints"}).to_dict("records"),
                columns=[
                    {"name":"Date","id":"Date"},
                    {"name":"Status","id":"Status","presentation":"markdown"},
                    {"name":"Complaints","id":"Complaints"},
                ],
                markdown_options={"html": True},
                page_size=12,
                style_table={"overflowX":"auto"},
                style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","borderBottom":"1px solid #e9ecef"},
                style_cell={"padding":"9px","fontSize":14,
                            "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                            "textAlign":"left"},
                style_data={"borderBottom":"1px solid #eceff4"},
                style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
            )

            # Calendar
            df_valid = work[work["Training Status"].isin(STATUS_ORDER)].copy()
            if df_valid.empty:
                return html.Div("No valid date/status for calendar."), table, id_to_label, opts, focus_value, "", False

            df_valid = df_valid.sort_values("Date").drop_duplicates("Date", keep="last")
            df_valid["Status Code"] = df_valid["Training Status"].map(STATUS_CODE)

            full_index = pd.date_range(start=df_valid["Date"].min(),
                                    end=pd.Timestamp("today").normalize(), freq="D")
            heat_df = pd.DataFrame({"Date": full_index})
            heat_df = heat_df.merge(df_valid[["Date","Status Code"]], on="Date", how="left").sort_values("Date")
            heat_df["Status Code"] = heat_df["Status Code"].ffill().fillna(-1).astype(int)
            heat_df = heat_df[heat_df["Status Code"] >= 0].copy()

            if not PLOTLYCAL_AVAILABLE:
                return html.Div([
                    html.P("Cannot draw calendar heatmap: 'plotly-calplot' is not installed."),
                    html.P("Install with: pip install plotly-calplot"),
                ]), table, id_to_label, opts, focus_value, "", False

            colorscale = discrete_colorscale_from_hexes(COLOR_LIST)
            fig_cal = pc.calplot(heat_df, x="Date", y="Status Code", colorscale=colorscale)

            heatmap: Optional[go.Heatmap] = next((t for t in fig_cal.data if isinstance(t, go.Heatmap)), None)
            if heatmap is not None:
                heatmap.showscale = True
                heatmap.zmin = 0
                heatmap.zmax = 4
                heatmap.xgap = 2
                heatmap.ygap = 2

            # Typography / labels in black
            fig_cal.update_layout(
                title_text=f"Calendar Heatmap: {int(heat_df['Date'].dt.year.max())}",
                margin=dict(l=18, r=18, t=46, b=10),
                height=480,
                paper_bgcolor="white",
                plot_bgcolor="white",
                font=dict(family="system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        size=13, color="#111111"),
                title_font_color="#111111",
            )
            fig_cal.update_xaxes(tickfont=dict(color="#111111"))
            fig_cal.update_yaxes(tickfont=dict(color="#111111"))

            # Day numbers (bold if appointment day)
            appt_dates = set(work["Date"].dt.date.tolist())
            if heatmap is not None:
                x_cats = list(heatmap.x)
                y_cats = list(heatmap.y)
            else:
                x_cats, y_cats = [], []

            txt_x, txt_y, txt_text = [], [], []
            for d in heat_df["Date"].dt.date.tolist():
                wd = d.strftime("%a")
                wk = int(pd.Timestamp(d).isocalendar().week)
                if (not x_cats or wd in x_cats) and (not y_cats or (wk in y_cats or str(wk) in y_cats)):
                    day_num = d.day
                    txt = f"<b>{day_num}</b>" if d in appt_dates else f"{day_num}"
                    txt_x.append(wd); txt_y.append(wk); txt_text.append(txt)

            if txt_x:
                fig_cal.add_trace(go.Scatter(
                    x=txt_x, y=txt_y,
                    mode="text",
                    text=txt_text,
                    textfont=dict(size=10, color="#111111"),
                    textposition="middle center",
                    hoverinfo="skip",
                    showlegend=False
                ))

            # Optional tiny dots on appointment days
            for d in appt_dates:
                wd = d.strftime("%a"); wk = int(pd.Timestamp(d).isocalendar().week)
                if (not x_cats or wd in x_cats) and (not y_cats or (wk in y_cats or str(wk) in y_cats)):
                    fig_cal.add_trace(go.Scatter(
                        x=[wd], y=[wk],
                        mode="markers",
                        marker=dict(symbol="circle", color="#d62728", size=5),
                        hoverinfo="skip", showlegend=False
                    ))

            cal_graph = dcc.Graph(id="cal-graph", figure=fig_cal, config={"displayModeBar": False})
            return cal_graph, table, id_to_label, opts, focus_value, "", False

        except Exception:
            tb = traceback.format_exc()
            print("\n=== UNEXPECTED ERROR ===\n", tb)
            return html.Div("Error building calendar."), html.Div([
                html.P("Unexpected error in processing:"),
                html.Pre(tb),
            ]), {}, [], None, "", False

    # Comments controls
    @app.callback(
        Output("comment-athlete", "options"),
        Output("comment-athlete", "value"),
        Output("comment-hint", "children"),
        Input("selected-athletes-map", "data"),
    )
    def update_comment_controls(id_to_label):
        if not id_to_label:
            return [], None, "Select an athlete above; comments will filter to that athlete."
        opts = [{"label": lbl, "value": int(cid)} for cid, lbl in id_to_label.items()]
        first_val = opts[0]["value"]
        return opts, first_val, "Showing comments for the selected athlete."

    @app.callback(Output("comments-table", "data"), Input("comment-athlete", "value"))
    def refresh_comments_for_selected(athlete_id):
        if not athlete_id: return []
        return db_list_comments([int(athlete_id)])

    # Hover & click on calendar
    @app.callback(Output("hover-date", "children"), Input("cal-graph", "hoverData"), prevent_initial_call=True)
    def display_hover_date(hoverData):
        if not hoverData or "points" not in hoverData: return ""
        pt = hoverData["points"][0]
        candidate = pt.get("customdata",[None])[0] if "customdata" in pt else pt.get("x","")
        try:
            dt = pd.to_datetime(candidate).date(); return f"{dt}"
        except Exception:
            return str(candidate)

    @app.callback(Output("comment-date", "date"), Input("cal-graph", "clickData"), prevent_initial_call=True)
    def click_prefill_date(clickData):
        if not clickData or "points" not in clickData: return no_update
        pt = clickData["points"][0]
        candidate = pt.get("customdata",[None])[0] if "customdata" in pt else pt.get("x","")
        try:
            dt = pd.to_datetime(candidate).date(); return dt.strftime("%Y-%m-%d")
        except Exception:
            return no_update

    # Save comment → DB + refresh
    @app.callback(
        Output("comments-table", "data", allow_duplicate=True),
        State("comment-athlete", "value"),
        State("selected-athletes-map", "data"),
        State("comment-date", "date"),
        State("comment-text", "value"),
        Input("comment-save", "n_clicks"),
        prevent_initial_call=True,
    )
    def save_comment_and_refresh(athlete_id, id_to_label, date_str, text, _n):
        if not athlete_id or not date_str or not (text or "").strip(): return no_update
        athlete_id = int(athlete_id)
        label = id_to_label.get(str(athlete_id)) or id_to_label.get(athlete_id) or f"ID {athlete_id}"
        db_add_comment(athlete_id, label, date_str, text.strip())
        return db_list_comments([athlete_id])

    # Athlete Summary
    @app.callback(
        Output("athlete-summary-container", "children"),
        Input("selected-athletes-map", "data"),
        Input("comment-athlete", "value"),
    )
    def render_athlete_summary(id_to_label, focus_id):
        cid = None
        if focus_id:
            cid = int(focus_id)
        elif id_to_label:
            try: cid = int(next(iter(id_to_label.keys())))
            except Exception: cid = None

        if not cid or cid not in CUSTOMERS:
            return html.Div("Select an athlete to see demographics, current status, and complaints.", className="text-muted")

        cust = CUSTOMERS[cid]
        label = id_to_label.get(str(cid)) or id_to_label.get(cid) \
                or f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})"

        dob   = cust.get("dob") or cust.get("birthdate") or ""
        sex   = cust.get("sex") or cust.get("gender") or ""
        email = cust.get("email") or ""
        phone = cust.get("phone") or cust.get("mobile") or ""

        chips = [html.Span(g.title(), className="badge bg-light text-dark me-1 mb-1",
                        style={"border":"1px solid #e3e6eb"}) for g in CID_TO_GROUPS.get(cid, [])]

        # Current training status (forward-filled)
        appts = CID_TO_APPTS.get(cid, [])
        status_rows: List[Tuple[pd.Timestamp, str]] = []
        for ap in appts:
            aid = ap.get("id")
            date_str = tidy_date_str(ap.get("date"))
            dt = pd.to_datetime(date_str, errors="coerce")
            if pd.isna(dt): continue
            eids = encounter_ids_for_appt(aid)
            max_eid = max(eids) if eids else None
            s = extract_training_status(fetch_encounter(max_eid)) if max_eid else ""
            if s: status_rows.append((dt.normalize(), s))
        current_status = ""
        if status_rows:
            df_s = pd.DataFrame(status_rows, columns=["Date","Status"]).sort_values("Date")
            df_s = df_s.drop_duplicates("Date", keep="last")
            full_idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
            df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
            df_full["Status"] = df_full["Status"].ffill()
            current_status = str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""

        dot_color = PASTEL_COLOR.get(current_status, "#e6e6e6")
        big_dot = html.Span(style={
            "display":"inline-block","width":"18px","height":"18px",
            "borderRadius":"50%","background":dot_color,
            "border":"1px solid rgba(0,0,0,.25)","marginRight":"10px"
        })

        complaints = fetch_customer_complaints(cid)
        if complaints:
            comp_rows = [{"Title": c["Title"], "Onset": c["Onset"], "Priority": c["Priority"], "Status": c["Status"]}
                        for c in complaints]
            comp_table = dash_table.DataTable(
                columns=[{"name":"Title","id":"Title"},
                        {"name":"Onset","id":"Onset"},
                        {"name":"Priority","id":"Priority"},
                        {"name":"Status","id":"Status"}],
                data=comp_rows, page_size=5,
                style_header={"fontWeight":"600","backgroundColor":"#fafbfc"},
                style_cell={"padding":"6px","fontSize":13,
                            "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                            "textAlign":"left"},
                style_data={"borderBottom":"1px solid #eee"},
                style_table={"overflowX":"auto"},
            )
        else:
            comp_table = html.Div("No complaints found.", className="text-muted")

        return dbc.Row([
            dbc.Col([
                html.H5(label, className="mb-2"),
                html.Div(chips, className="mb-2"),
                html.Div([
                    html.Span("Current Status: ", className="fw-semibold me-1"),
                    big_dot, html.Span(current_status or "—")
                ], className="mb-2"),
                html.Div([
                    html.Div([html.Span("DOB: ", className="fw-semibold"), html.Span(dob or "—")]),
                    html.Div([html.Span("Sex: ", className="fw-semibold"), html.Span(sex or "—")]),
                    html.Div([html.Span("Email: ", className="fw-semibold"), html.Span(email or "—")]),
                    html.Div([html.Span("Phone: ", className="fw-semibold"), html.Span(phone or "—")]),
                ], style={"fontSize":"14px"})
            ], md=5),
            dbc.Col([
                html.Div("Complaints", className="fw-semibold mb-2"),
                comp_table
            ], md=7),
        ])
