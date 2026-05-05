# app.py
import os, hashlib, base64, sqlite3, traceback, functools
from datetime import date
from html import escape as html_escape

import requests
import pandas as pd
import dash
from dash_auth_external import DashAuthExternal
from dash import Dash, Input, Output, State, html, dcc, dash_table, no_update, ALL, ctx
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

# Repo components & settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET
import training_dashboard as td  # reuse groups, API access, DB path, etc.

# ───────────────────────── Constants ─────────────────────────
BASE_ROOT_URL = "https://0199594c-6df2-cf52-c051-91a6b8901094.share.connect.posit.cloud/"

# ───────────────────────── Auth / Server ─────────────────────────
auth = DashAuthExternal(
    AUTH_URL, TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Serve assets (same as repo)
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"

# Ensure the SQLite table exists on first run (so first comment works).
try:
    td._db().close()
except Exception:
    pass

# ───────────────────────── Styles (tabs & pills) ─────────────────────────
TABS_CONTAINER_STYLE = {
    "display": "flex",
    "gap": "6px",
    "alignItems": "center",
    "borderBottom": "0",
    "marginBottom": "4px",
    "width": "100%",
}
TAB_STYLE = {
    "padding": "8px 14px",
    "border": "1px solid #e9ecef",
    "borderRadius": "8px",
    "background": "#f8f9fb",
    "color": "#495057",
    "fontWeight": "500",
    "flex": "1 1 0%",
    "textAlign": "center",
}
TAB_SELECTED_STYLE = {
    "padding": "8px 14px",
    "border": "1px solid #cfe2ff",
    "borderRadius": "8px",
    "background": "#e7f1ff",
    "color": "#084298",
    "fontWeight": "600",
    "boxShadow": "inset 0 1px 0 rgba(255,255,255,.6)",
    "flex": "1 1 0%",
    "textAlign": "center",
}

# ───────────────────────── UI helpers (pills/dots/colors) ─────────────────────────
PILL_BG_DEFAULT = "#eef2f7"
PILL_BORDER_RADIUS = "6px"
PALETTE = ["#e7f0ff", "#fde2cf", "#e6f3e6", "#f3e6f7", "#fff3cd", "#e0f7fa", "#fbe7eb", "#e7f5ff"]
BORDER = "#cfd6de"

def color_for_label(text: str) -> str:
    if not text:
        return PILL_BG_DEFAULT
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(PALETTE)
    return PALETTE[idx]

def pill_html(text: str, bg=None, fg="#111", border=BORDER) -> str:
    bg = bg or PILL_BG_DEFAULT
    return (
        f'<span style="display:inline-block;padding:2px 8px;'
        f'border-radius:{PILL_BORDER_RADIUS};background:{bg};color:{fg};'
        f'border:1px solid {border};font-size:12px;'
        f'line-height:18px;white-space:nowrap;">{html_escape(text)}</span>'
    )

def dot_html(hex_color: str, size: int = 10, mr: int = 8) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )

def status_pill_component(text: str, kind: str = "success"):
    if kind == "success":
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#e9f7ef", "color": "#0f5132", "border": "1px solid #badbcc",
            "fontSize": "12px", "lineHeight": "18px", "WhiteSpace": "nowrap"
        }
    elif kind == "danger":
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#fdecea", "color": "#842029", "border": "1px solid #f5c2c7",
            "fontSize": "12px", "lineHeight": "18px", "WhiteSpace": "nowrap"
        }
    else:
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#eef2f7", "color": "#111", "border": "1px solid #cfd6de",
            "fontSize": "12px", "lineHeight": "18px", "WhiteSpace": "nowrap"
        }
    return html.Span(text, style=style)

# ───────────────────────── Signed-in name helpers ─────────────────────────
def _b64url_decode(part: str) -> bytes:
    part = part + '=' * (-len(part) % 4)
    return base64.urlsafe_b64decode(part.encode("utf-8"))

def _name_from_jwt(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) < 2: return ""
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
        # Try Bearer
        try:
            r = requests.get(f"{SITE_URL}/api/csiauth/me/",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             timeout=5)
            if r.status_code == 200:
                js = r.json()
                first = (js.get("first_name") or "").strip()
                last  = (js.get("last_name") or "").strip()
                name = f"{first} {last}".strip() or js.get("email", "")
                if name: return name
        except Exception:
            pass
        # Try query param
        try:
            r2 = requests.get(f"{SITE_URL}/api/csiauth/me/", params={"access_token": token}, timeout=5)
            if r2.status_code == 200:
                js = r2.json()
                first = (js.get("first_name") or "").strip()
                last  = (js.get("last_name") or "").strip()
                name = f"{first} {last}".strip() or js.get("email", "")
                if name: return name
        except Exception:
            pass
        # JWT decode fallback
        return _name_from_jwt(token) or ""
    except Exception:
        return ""

# ───────────────────────── Status choices ─────────────────────────
STATUS_CHOICES = [
    "Full participation without Health problems",
    "Full participation with Illness/Injury",
    "Reduced participation with Illness/Injury",
    "No Participation due to Illness/Injury",
]

# ───────────────────────── Cache current status per athlete ─────────────────────────
# ───────────────────────── Cache current status per athlete ─────────────────────────
@functools.lru_cache(maxsize=2048)
def _current_status_for_customer(cid: int) -> str:
    try:
        appts = td.CID_TO_APPTS.get(int(cid), [])
        status_rows = []
        for ap in appts:
            try:
                aid = ap.get("id")
                date_str = td.tidy_date_str(ap.get("date"))
                dt = pd.to_datetime(date_str, errors="coerce")
                if pd.isna(dt):
                    continue
                eids = td.encounter_ids_for_appt(aid)
                max_eid = max(eids) if eids else None
                s = td.extract_training_status(td.fetch_encounter(max_eid)) if max_eid else ""
                if s:
                    status_rows.append((dt.normalize(), s))
            except Exception:
                continue
        if not status_rows:
            return ""
        df_s = pd.DataFrame(status_rows, columns=["Date", "Status"]).sort_values("Date")
        df_s = df_s.drop_duplicates("Date", keep="last")
        full_idx = pd.date_range(start=df_s["Date"].min(),
                                 end=pd.Timestamp("today").normalize(), freq="D")
        df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
        df_full["Status"] = df_full["Status"].ffill()
        return str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""
    except Exception:
        return ""

# ───────────────────────── Tab 1 (Overview) ─────────────────────────
def tab1_layout():
    return dbc.Container([
        html.H3("Browse Athlete Data", className="mt-2"),

        # Selection row
        dbc.Card([
            dbc.CardHeader("Select Athlete"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Branch", className="fw-bold"),
                        dcc.Dropdown(
                            id="t1-branch-dd",
                            options=td.BRANCH_OPTS,
                            placeholder="Select a branch…",
                            clearable=True,
                        ),
                    ], md=4),
                    dbc.Col([
                        html.Label("Athlete", className="fw-bold"),
                        dcc.Loading(
                            dcc.Dropdown(
                                id="t1-athlete-dd",
                                placeholder="Select a branch first…",
                                clearable=True,
                                disabled=True,
                            ),
                            type="circle",
                        ),
                    ], md=4),
                ], className="g-2"),
                html.Div(id="t1-cascade-status", className="mt-2 text-muted small"),
            ])
        ], className="mb-3"),

        # Complaints pills
        dbc.Card([
            dbc.CardHeader("Complaints — click one to view encounters"),
            dbc.CardBody(
                dcc.Loading(
                    html.Div(
                        id="t1-complaints-pills",
                        className="d-flex flex-wrap gap-2",
                        style={"minHeight": "38px"},
                    ),
                    type="circle",
                )
            ),
        ], className="mb-3"),

        # Encounters
        dbc.Card([
            dbc.CardHeader(html.Span(id="t1-encounters-header", children="Encounters")),
            dbc.CardBody(
                dcc.Loading(
                    html.Div(id="t1-encounters-container"),
                    type="circle",
                )
            ),
        ], className="mb-3"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger"),
        html.Hr(),

        # Comments
        dbc.Card([
            dbc.CardHeader([
                html.Span("Comments", className="me-2"),
                html.Span(id="t1-selected-athlete-label", className="fw-semibold text-muted")
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.Textarea(
                        id="t1-comment-text",
                        placeholder="Add a note about the selected athlete…",
                        style={"width":"100%","height":"110px"}
                    ), md=8),
                    dbc.Col([
                        dcc.DatePickerSingle(id="t1-comment-date", display_format="YYYY-MM-DD", style={"width":"100%"}),
                        dcc.Dropdown(id="t1-complaint-dd", placeholder="Pick a complaint (optional)…",
                                     style={"width":"100%","marginTop":"6px"}),
                        dbc.Button(
                            "Show Status Override", id="t1-toggle-status", color="secondary",
                            className="w-100", style={"marginTop": "6px"}
                        ),
                        dbc.Collapse(
                            dcc.Dropdown(
                                id="t1-status-override",
                                options=[{"label": s, "value": s} for s in STATUS_CHOICES],
                                placeholder="Override status…",
                                clearable=True,
                                style={"width": "100%", "marginTop": "6px"}
                            ),
                            id="t1-status-collapse",
                            is_open=False
                        ),
                        dbc.Button("Save Comment", id="t1-save-comment", color="success",
                                   className="w-100", style={"marginTop":"6px"}),
                    ], md=4),
                ], className="g-2"),
                html.Div(id="t1-comment-status", className="mt-2"),
                html.Hr(),
                dash_table.DataTable(
                    id="t1-comments-table",
                    columns=[
                        {"name":"Date","id":"Date", "editable": False},
                        {"name":"By","id":"By", "editable": False},
                        {"name":"Athlete","id":"Athlete", "editable": False},
                        {"name":"Complaint","id":"Complaint", "editable": False},
                        {"name":"Status","id":"Status", "editable": False},
                        {"name":"Comment","id":"Comment", "editable": True},
                        {"name":"_id","id":"_id", "hidden": True, "editable": False},
                    ],
                    data=[],
                    row_deletable=True,
                    editable=False,
                    page_action="none",
                    style_table={"overflowX":"auto","maxHeight":"240px","overflowY":"auto"},
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","lineHeight":"22px"},
                    style_cell={"padding":"9px","fontSize":14,"lineHeight":"22px",
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"borderBottom":"1px solid #eceff4"},
                    style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
                ),
            ])
        ], className="mb-4"),

        # Stores
        dcc.Store(id="t1-active-complaint-id", data=None),
        dcc.Store(id="t1-selected-complaint-data", data={}),
        dcc.Store(id="t1-rows-json", data=[]),
        dcc.Store(id="t1-selected-branch", data=None),
        dcc.Store(id="t1-selected-athlete", data=None),
        dcc.Store(id="t1-selected-complaint", data=None),
    ], fluid=True)

# ───────────────────────── Tab 2 (Training Dashboard) ─────────────────────────
def tab2_layout():
    return td.layout_body()

# ───────────────────────── App shell ─────────────────────────
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[dbc.themes.BOOTSTRAP,
                          "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"],
    suppress_callback_exceptions=True,
)

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

    dbc.Container([
        dcc.Tabs(
            id="main-tabs",
            value="tab-1",
            children=[
                dcc.Tab(label="Athlete Status", value="tab-1",
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
                dcc.Tab(label="Status History", value="tab-2",
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
            ],
            style=TABS_CONTAINER_STYLE,
            parent_style={"width": "100%"},
            mobile_breakpoint=0,
        ),
        html.Div(id="tabs-content", className="mt-3"),
    ], fluid=True),

    Footer().render(),
])

# ───────────────────────── Tab switcher ─────────────────────────
@app.callback(Output("tabs-content", "children"), Input("main-tabs", "value"))
def render_tab(which):
    return tab1_layout() if which == "tab-1" else tab2_layout()

# ───────────────────────── Login redirect & navbar user ─────────────────────────
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals"),
    State("redirect-to", "pathname"),
)
def initial_view(n, pathname):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    if token:
        return no_update
    return BASE_ROOT_URL

@app.callback(Output("navbar-user", "children"), Input("user-refresh", "n_intervals"))
def refresh_user_badge(_n):
    try:
        name = _get_signed_in_name()
        return f"Signed in as: {name}" if name else html.A("Sign in", href=BASE_ROOT_URL, className="link-light")
    except Exception:
        return html.A("Sign in", href=BASE_ROOT_URL, className="link-light")

@app.callback(
    Output("redirect-to", "href", allow_duplicate=True),
    Input("user-refresh", "n_intervals"),
    prevent_initial_call=True
)
def enforce_session(_n):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    if token:
        return no_update
    return BASE_ROOT_URL

# ───────────────────────── Tab 1: Toggle status override (and clear when off) ─────────────────────────
@app.callback(
    Output("t1-status-collapse", "is_open"),
    Output("t1-status-override", "value"),
    Input("t1-toggle-status", "n_clicks"),
    State("t1-status-collapse", "is_open"),
    prevent_initial_call=True
)
def toggle_status_override(n, is_open):
    new_state = not bool(is_open)
    return new_state, (None if not new_state else no_update)

# ───────────────────────── Tab 1: On select athlete ─────────────────────────
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-comments-table", "data"),
    Output("t1-selected-athlete-label", "children"),
    Output("t1-comment-date", "date"),
    Output("t1-status-override", "value", allow_duplicate=True),
    Input("t1-athlete-dd", "value"),
    State("t1-athlete-dd", "options"),
    prevent_initial_call=True,
)
def t1_on_select(athlete_id, athlete_options):
    today = date.today().strftime("%Y-%m-%d")

    if not athlete_id:
        return [], None, [], "", today, None

    cid = int(athlete_id)

    label = ""
    for opt in (athlete_options or []):
        if opt.get("value") == athlete_id:
            label = opt.get("label", "")
            break
    if not label:
        label = f"Athlete {cid}"

    try:
        complaints = td.fetch_customer_complaints(cid)
        names = [c["Title"] for c in complaints if c.get("Title")]
        opts = [{"label": n, "value": n} for n in sorted(set(names))]
        val = opts[0]["value"] if opts else None
    except Exception:
        opts = []
        val = None

    comments = _db_list_comments_with_ids([cid])
    expanded = [_expand_comment_record(rec, label, cid) for rec in comments]

    return opts, val, expanded, f" — {label}", today, None

# ───────────────────────── Tab 1: Save comment ─────────────────────────
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    Output("t1-comment-text", "value", allow_duplicate=True),
    Output("t1-comment-status", "children", allow_duplicate=True),
    Output("t1-status-override", "value", allow_duplicate=True),
    State("t1-athlete-dd", "value"),
    State("t1-athlete-dd", "options"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    State("t1-status-override", "value"),
    State("t1-comments-table", "data"),
    Input("t1-save-comment", "n_clicks"),
    prevent_initial_call=True,
)
def t1_save_comment(athlete_id, athlete_options, complaint, date_str, text, status_override, table_data, _n):
    if not _n or not athlete_id or not date_str or not (text or "").strip():
        raise PreventUpdate

    cid = int(athlete_id)

    label = ""
    for opt in (athlete_options or []):
        if opt.get("value") == athlete_id:
            label = opt.get("label", "")
            break
    if not label:
        label = f"Athlete {cid}"

    author = _get_signed_in_name()

    status_to_use = status_override or _current_status_for_customer(cid)

    new_id = _db_add_comment_returning(
        cid, label, date_str, text.strip(),
        complaint=(complaint or ""),
        author=(author or ""),
        status_override=(status_override or "")
    )

    new_row = {
        "_id": new_id,
        "Date": date_str,
        "By": author or "",
        "Athlete": label,
        "Complaint": complaint or "",
        "Status": status_to_use or "",
        "Comment": text.strip(),
    }

    current = table_data or []
    updated = current + [new_row]

    return updated, "", status_pill_component("Comment saved.", "success"), None

# ───────────────────────── Tab 1: Persist edits/deletes ─────────────────────────
@app.callback(
    Output("t1-comment-status", "children", allow_duplicate=True),
    Input("t1-comments-table", "data_timestamp"),
    State("t1-comments-table", "data"),
    State("t1-comments-table", "data_previous"),
    prevent_initial_call=True,
)
def t1_persist_comment_mutations(_ts, data, data_prev):
    try:
        if data_prev is None:
            raise PreventUpdate

        prev_by_id = {r["_id"]: r for r in data_prev if r.get("_id") is not None}
        now_by_id  = {r["_id"]: r for r in data     if r.get("_id") is not None}

        deleted_ids = [cid for cid in prev_by_id.keys() if cid not in now_by_id]
        for i in deleted_ids:
            _db_delete_comment(i)

        any_edit = False
        for cid, now in now_by_id.items():
            before = prev_by_id.get(cid)
            if not before:
                continue
            if (before.get("Comment") or "") != (now.get("Comment") or ""):
                _db_update_comment_text(cid, now.get("Comment") or "")
                any_edit = True

        if deleted_ids and any_edit:
            return status_pill_component("Comments updated & deleted.", "success")
        elif deleted_ids:
            return status_pill_component("Comment deleted.", "success")
        elif any_edit:
            return status_pill_component("Comment updated.", "success")
        else:
            raise PreventUpdate
    except Exception as e:
        return status_pill_component(f"Comment persistence error: {e}", "danger")

# ───────────────────────── SQLite helpers (reuse td.DB_PATH) ─────────────────────────
def _db_connect():
    conn = sqlite3.connect(td.DB_PATH, check_same_thread=False)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(comments)")
        cols = [row[1] for row in cur.fetchall()]
        to_add = []
        if "author" not in cols:
            to_add.append(("author", "TEXT"))
        if "complaint" not in cols:
            to_add.append(("complaint", "TEXT"))
        if "status_override" not in cols:
            to_add.append(("status_override", "TEXT"))
        for name, sqltype in to_add:
            cur.execute(f"ALTER TABLE comments ADD COLUMN {name} {sqltype}")
        conn.commit()
    except Exception:
        pass
    return conn

def _db_add_comment_returning(customer_id: int, customer_label: str, date_str: str, comment: str,
                              complaint: str = "", author: str = "", status_override: str = "") -> int:
    conn = _db_connect(); cur = conn.cursor()
    cur.execute(
        """INSERT INTO comments(customer_id, customer_label, date, comment, complaint, author, status_override, created_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (int(customer_id), customer_label or "", date_str, comment,
         complaint or None, author or None, status_override or None)
    )
    new_id = cur.lastrowid
    conn.commit(); conn.close()
    return int(new_id)

def _db_list_comments_with_ids(customer_ids):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("PRAGMA table_info(comments)")
    cols = [row[1] for row in cur.fetchall()]
    has_author = "author" in cols
    has_complaint = "complaint" in cols
    has_status_override = "status_override" in cols

    select_cols = ["id", "date", "comment", "customer_label", "customer_id", "created_at"]
    if has_author: select_cols.append("author")
    if has_complaint: select_cols.append("complaint")
    if has_status_override: select_cols.append("status_override")
    sel = ", ".join(select_cols)

    if customer_ids:
        vals = [int(x) for x in customer_ids]
        q = ",".join("?" for _ in vals)
        cur.execute(f"""
          SELECT {sel}
          FROM comments
          WHERE customer_id IN ({q})
          ORDER BY date ASC, id ASC
        """, vals)
    else:
        cur.execute(f"SELECT {sel} FROM comments ORDER BY date ASC, id ASC")
    rows = cur.fetchall(); conn.close()

    out = []
    for r in rows:
        base = {
            "_id": r[0],
            "Date": r[1],
            "Comment": r[2],
            "Athlete": r[3],
            "_cid": r[4],
            "_created_at": r[5],
        }
        idx = 6
        author = r[idx] if has_author else ""
        idx += 1 if has_author else 0
        complaint = r[idx] if has_complaint else ""
        idx += 1 if has_complaint else 0
        status_override = r[idx] if has_status_override else ""
        base["_author"] = author or ""
        base["_complaint"] = complaint or ""
        base["_status_override"] = status_override or ""
        out.append(base)
    return out

def _db_delete_comment(comment_id: int):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("DELETE FROM comments WHERE id = ?", (int(comment_id),))
    conn.commit(); conn.close()

def _db_update_comment_text(comment_id: int, new_text: str):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("UPDATE comments SET comment = ? WHERE id = ?", (new_text, int(comment_id)))
    conn.commit(); conn.close()

def _expand_comment_record(rec, athlete_label, cid: int):
    status = rec.get("_status_override") or _current_status_for_customer(int(cid))
    return {
        "_id": rec["_id"],
        "Date": rec["Date"],
        "By": rec.get("_author", "") or "",
        "Athlete": athlete_label,
        "Complaint": rec.get("_complaint", "") or "",
        "Status": status or "",
        "Comment": rec["Comment"],
    }

# ───────────────────────── Pill helper ─────────────────────────
def _build_complaint_pills(complaints, active_id=None):
    if not complaints:
        return [html.Span("No complaints found for this athlete.", className="text-muted small")]
    pills = []
    for c in complaints:
        cid   = c.get("Id") or c.get("id")
        title = (c.get("Title") or "").strip()
        lat   = (c.get("Laterality") or "").strip()
        label = f"{title} ({lat})" if lat else title
        is_active = (cid is not None and cid == active_id)
        bg = color_for_label(title)
        style = {
            "padding": "5px 14px",
            "borderRadius": "20px",
            "fontSize": "13px",
            "fontWeight": "600" if is_active else "400",
            "cursor": "pointer",
            "border": "2px solid #084298" if is_active else f"1px solid {BORDER}",
            "background": "#cfe2ff" if is_active else bg,
            "color": "#084298" if is_active else "#111",
            "boxShadow": "0 0 0 3px rgba(13,110,253,.25)" if is_active else "none",
            "transition": "all .15s",
        }
        pills.append(html.Button(label, id={"type": "t1-complaint-pill", "index": cid}, style=style, n_clicks=0))
    return pills

# ───────────────────────── Cascading Callbacks ─────────────────────────
# Step 1: Load athletes when branch is selected
@app.callback(
    Output("t1-athlete-dd", "options"),
    Output("t1-athlete-dd", "disabled"),
    Output("t1-athlete-dd", "value"),
    Output("t1-cascade-status", "children"),
    Input("t1-branch-dd", "value"),
    prevent_initial_call=True
)
def t1_load_athletes(branch_id):
    if not branch_id:
        return [], True, None, "Select a branch to load athletes."
    try:
        athletes = td.get_athletes_for_branch(int(branch_id))
        options = [{"label": a["label"], "value": a["id"]} for a in athletes]
        status_msg = f"{len(options)} athlete(s) in this branch. Select one to view complaints." if options else "No athletes found for this branch."
        return options, False, None, status_msg
    except Exception as e:
        print(f"Error loading athletes: {e}")
        traceback.print_exc()
        return [], True, None, f"Error: {str(e)}"

# Step 2: Render complaint pills when athlete is selected
@app.callback(
    Output("t1-complaints-pills", "children"),
    Output("t1-active-complaint-id", "data"),
    Input("t1-athlete-dd", "value"),
    prevent_initial_call=True,
)
def t1_render_complaint_pills(athlete_id):
    if not athlete_id:
        return [], None
    try:
        complaints = td.fetch_customer_complaints(int(athlete_id))
        return _build_complaint_pills(complaints, active_id=None), None
    except Exception as e:
        print(f"Error rendering complaint pills: {e}")
        traceback.print_exc()
        return [dbc.Alert(f"Error loading complaints: {e}", color="danger")], None

# Step 3: Pill click → highlight active pill + update store
@app.callback(
    Output("t1-complaints-pills", "children", allow_duplicate=True),
    Output("t1-active-complaint-id", "data", allow_duplicate=True),
    Input({"type": "t1-complaint-pill", "index": ALL}, "n_clicks"),
    State("t1-athlete-dd", "value"),
    prevent_initial_call=True,
)
def t1_pill_clicked(n_clicks_list, athlete_id):
    if not any(n_clicks_list) or not athlete_id or not ctx.triggered:
        raise PreventUpdate
    triggered_prop = ctx.triggered[0]["prop_id"]
    try:
        import json as _json
        active_id = _json.loads(triggered_prop.rsplit(".", 1)[0])["index"]
    except Exception:
        raise PreventUpdate
    try:
        complaints = td.fetch_customer_complaints(int(athlete_id))
        return _build_complaint_pills(complaints, active_id=active_id), active_id
    except Exception:
        raise PreventUpdate

# Step 4: Load encounters automatically when active complaint changes
@app.callback(
    Output("t1-encounters-container", "children"),
    Output("t1-encounters-header", "children"),
    Output("t1-selected-complaint-data", "data"),
    Input("t1-active-complaint-id", "data"),
    State("t1-athlete-dd", "value"),
    prevent_initial_call=True,
)
def t1_load_encounters(complaint_id, athlete_id):
    if not complaint_id or not athlete_id:
        return html.Div(), "Encounters", {}
    try:
        encounters = td.get_encounters_for_complaint(int(complaint_id), int(athlete_id))

        # Build header with complaint name
        header = f"Encounters — complaint {complaint_id}"
        try:
            complaints = td.fetch_customer_complaints(int(athlete_id))
            for c in complaints:
                if (c.get("Id") or c.get("id")) == complaint_id:
                    t   = (c.get("Title") or "").strip()
                    lat = (c.get("Laterality") or "").strip()
                    header = f"Encounters — {t + ' (' + lat + ')' if lat else t}"
                    break
        except Exception:
            pass

        if not encounters:
            return dbc.Alert("No encounters found for this complaint.", color="info"), header, {}

        def _enc_date(enc):
            raw = enc.get("chart_date") or enc.get("date") or ""
            return raw.split("T")[0] if raw else "—"

        def _enc_type(enc):
            try:
                return enc["data"][0]["template"]["tab_name"]
            except (KeyError, IndexError, TypeError):
                return enc.get("type") or enc.get("encounter_type") or "—"

        def _enc_fields_summary(enc):
            try:
                fields = enc["data"][0]["fields"]
                parts = []
                for f in fields:
                    if not f.get("id", "").startswith("Id_select"):
                        continue
                    val = (f.get("value") or "").strip()
                    if not val or val.startswith("-----"):
                        continue
                    name = (f.get("name") or "").strip()
                    parts.append(f"{name}: {val}" if name else val)
                return "; ".join(parts) if parts else "—"
            except (KeyError, IndexError, TypeError):
                return "—"

        rows = [
            {
                "Date": _enc_date(enc),
                "Form": _enc_type(enc),
                "Training Status": td.extract_training_status(enc) or "—",
                "Fields": _enc_fields_summary(enc),
            }
            for enc in sorted(encounters, key=lambda e: _enc_date(e))
        ]

        table = dash_table.DataTable(
            data=rows,
            columns=[
                {"name": "Date", "id": "Date"},
                {"name": "Form", "id": "Form"},
                {"name": "Training Status", "id": "Training Status"},
                {"name": "Fields", "id": "Fields"},
            ],
            style_table={"overflowX": "auto"},
            style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
            style_cell={
                "padding": "9px", "fontSize": 14, "textAlign": "left",
                "whiteSpace": "normal", "maxWidth": "500px",
            },
            style_data={"borderBottom": "1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
        )
        content = html.Div([
            html.H6(f"{len(encounters)} encounter(s) found"),
            table
        ])
        return content, header, {"complaint_id": complaint_id, "athlete_id": athlete_id, "encounters_count": len(encounters)}

    except Exception as e:
        print(f"Error loading encounters: {e}")
        traceback.print_exc()
        return dbc.Alert([html.Div("Error loading encounters:"), html.Pre(str(e))], color="danger"), "Encounters", {}

# ───────────────────────── Training tab callbacks ─────────────────────────
td.register_callbacks(app)

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
