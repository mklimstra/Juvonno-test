import os
import math
import requests
import pandas as pd
from datetime import datetime

import dash
from dash import Dash, dcc, html, Input, Output, State, callback_context, dash_table, no_update
import dash_bootstrap_components as dbc

# Import your existing training dashboard module (as provided in your last message)
import training_dashboard as td

# --------------------------------------------------------------------------------------
# App (simple; no auth wiring here — this focuses on the two-tab app working correctly)
# --------------------------------------------------------------------------------------
app = Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)
server = app.server


# --------------------------------------------------------------------------------------
# Tiny helpers that reuse training_dashboard.py’s data & logic
# --------------------------------------------------------------------------------------

def _athletes_for_groups(selected_groups):
    """Return [(cid, label, groups_str)] filtered by selected group names (case-insensitive)."""
    if not selected_groups:
        return []
    wanted = {td._norm(g) for g in selected_groups}
    out = []
    for cid, cust in td.CUSTOMERS.items():
        groups = td.CID_TO_GROUPS.get(cid, [])
        if set(groups) & wanted:
            label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()
            out.append((cid, label, ", ".join(g.title() for g in groups)))
    # stable sort by last name-ish
    out.sort(key=lambda t: t[1].lower())
    return out


def _current_status_for_cid(cid: int) -> str:
    """Same forward-fill idea as the summary in training_dashboard."""
    appts = td.CID_TO_APPTS.get(cid, [])
    status_rows = []
    for ap in appts:
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
    if not status_rows:
        return ""
    df_s = pd.DataFrame(status_rows, columns=["Date", "Status"]).sort_values("Date")
    df_s = df_s.drop_duplicates("Date", keep="last")
    full_idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
    df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
    df_full["Status"] = df_full["Status"].ffill()
    return str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""


# ---------------------- Comments DB (separate table; same SQLite file) ----------------

import sqlite3

T1_DB_PATH = td.DB_PATH  # use the same SQLite file

def _t1_db_conn():
    conn = sqlite3.connect(T1_DB_PATH, check_same_thread=False)
    # richer schema for Tab 1 comments (does not conflict with td.comments table)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS t1_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_label TEXT,
            complaint TEXT,
            status TEXT,
            author_name TEXT,
            author_email TEXT,
            date TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def t1_db_add_comment(customer_id: int, customer_label: str, date_str: str, comment: str,
                      complaint: str, status: str,
                      author_name: str = "", author_email: str = ""):
    conn = _t1_db_conn()
    conn.execute(
        """INSERT INTO t1_comments(customer_id, customer_label, complaint, status,
                                   author_name, author_email, date, comment, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (int(customer_id) if customer_id is not None else None,
         customer_label or "", complaint or "", status or "",
         author_name or "", author_email or "",
         date_str or datetime.utcnow().strftime("%Y-%m-%d"),
         comment or "",
         datetime.utcnow().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()

def t1_db_list_comments(customer_id: int | None) -> list[dict]:
    conn = _t1_db_conn()
    cur = conn.cursor()
    if customer_id:
        cur.execute("""
            SELECT date, customer_label, complaint, status, author_name, author_email, comment
            FROM t1_comments
            WHERE customer_id = ?
            ORDER BY date ASC, id ASC
        """, (int(customer_id),))
    else:
        cur.execute("""
            SELECT date, customer_label, complaint, status, author_name, author_email, comment
            FROM t1_comments
            ORDER BY date ASC, id ASC
        """)
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "Date": r[0],
            "Athlete": r[1],
            "Complaint": r[2],
            "Status": r[3],
            "Author": (r[4] or "") if not r[5] else f"{r[4]} ({r[5]})",
            "Comment": r[6],
        })
    return out


# --------------------------------------------------------------------------------------
# Tab 1 — “Athlete List & Comments”
# --------------------------------------------------------------------------------------

tab1 = dbc.Container([
    html.H3("Athlete List & Comments", className="mt-2"),

    dbc.Row([
        dbc.Col(dcc.Dropdown(id="t1-group-dd",
                             options=td.GROUP_OPTS,
                             multi=True,
                             placeholder="Select patient group(s)…"), md=6),
        dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
    ], className="g-2 mb-2"),

    dbc.Alert(id="t1-msg", is_open=False, color="danger", duration=0, className="mb-2"),

    # Athletes table (select a row to set active athlete)
    dash_table.DataTable(
        id="t1-athlete-table",
        columns=[
            {"name": "Athlete", "id": "Athlete"},
            {"name": "Groups", "id": "Groups"},
            {"name": "Complaints", "id": "Complaints"},
        ],
        page_size=12,
        style_table={"overflowX": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
        style_cell={
            "padding": "8px",
            "fontSize": 14,
            "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
            "textAlign": "left"
        },
        style_data={"borderBottom": "1px solid #eceff4"},
        row_selectable="single",
        selected_rows=[],
    ),

    html.Hr(),

    # Active athlete + complaint + comment editor
    dbc.Row([
        dbc.Col([
            html.Div([html.Span("Active athlete: ", className="fw-semibold"),
                      html.Span(id="t1-active-athlete-label", children="—")]),
            html.Div(id="t1-active-athlete-id", style={"display": "none"}),
        ], md=6),
        dbc.Col(dcc.Dropdown(
            id="t1-complaint-dd",
            options=[],
            placeholder="Select complaint…",
            clearable=True
        ), md=6),
    ], className="g-2 mb-2"),

    dbc.Row([
        dbc.Col(dcc.DatePickerSingle(
            id="t1-comment-date",
            display_format="YYYY-MM-DD",
            date=datetime.utcnow().strftime("%Y-%m-%d")
        ), md=3),
        dbc.Col(dcc.Textarea(
            id="t1-comment-text",
            placeholder="Write a comment about this athlete + complaint…",
            style={"width": "100%", "height": "80px"}
        ), md=7),
        dbc.Col(dbc.Button("Save", id="t1-comment-save", color="success", className="w-100"), md=2),
    ], className="g-2"),

    html.Div(id="t1-comment-hint", className="text-muted", style={"fontSize": "12px"}),

    html.Hr(),

    dash_table.DataTable(
        id="t1-comments-table",
        columns=[
            {"name": "Date", "id": "Date"},
            {"name": "Complaint", "id": "Complaint"},
            {"name": "Status", "id": "Status"},
            {"name": "Author", "id": "Author"},
            {"name": "Comment", "id": "Comment"},
        ],
        page_size=10,
        style_table={"overflowX": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
        style_cell={
            "padding": "8px",
            "fontSize": 13,
            "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
            "textAlign": "left",
            "whiteSpace": "normal",
            "height": "auto",
        },
        style_data={"borderBottom": "1px solid #eceff4"},
        data=[],
    ),
], fluid=True)


# --------------------------------------------------------------------------------------
# Tab 2 — reuse your Training Dashboard
# --------------------------------------------------------------------------------------

tab2 = td.layout_body()  # exactly your existing layout (single-athlete dropdown etc.)

# Register Training Dashboard callbacks
td.register_callbacks(app)


# --------------------------------------------------------------------------------------
# App layout (two tabs)
# --------------------------------------------------------------------------------------

app.layout = html.Div([
    dbc.Container([
        dcc.Tabs(id="tabs", value="tab1", children=[
            dcc.Tab(label="Athlete List & Comments", value="tab1"),
            dcc.Tab(label="Training Dashboard", value="tab2")
        ], persistence=True, persistence_type="session"),
        html.Div(id="tab-content", className="mt-3"),
    ], fluid=True)
])


# --------------------------------------------------------------------------------------
# Tab switch renderer
# --------------------------------------------------------------------------------------
@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value")
)
def render_tab(tabval):
    if tabval == "tab1":
        return tab1
    else:
        return tab2


# --------------------------------------------------------------------------------------
# Tab 1 callbacks
# --------------------------------------------------------------------------------------

# Populate groups once (uses td.GROUP_OPTS built at import)
@app.callback(
    Output("t1-group-dd", "options"),
    Input("tab-content", "children"),
    prevent_initial_call=True
)
def t1_init_groups(_):
    # When tab content mounts, provide options
    return td.GROUP_OPTS

# Load athletes when clicking "Load"
@app.callback(
    Output("t1-athlete-table", "data"),
    Output("t1-athlete-table", "selected_rows"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True
)
def t1_load_athletes(n, groups):
    if not groups:
        return [], [], "Select at least one group.", True
    try:
        # make sure API key exists (same enforcement as td)
        if os.getenv("JUV_API_KEY") is None:
            return [], [], "Missing JUV_API_KEY (set in your environment).", True

        rows = []
        for cid, label, groups_str in _athletes_for_groups(groups):
            # light computation: count complaints; avoid status calc for speed
            try:
                complaints = td.fetch_customer_complaints(cid)
                comp_count = len(complaints)
            except Exception:
                comp_count = 0
            rows.append({
                "Athlete": label,
                "Groups": groups_str,
                "Complaints": comp_count,
                "_cid": cid  # internal column for selection
            })
        if not rows:
            return [], [], "No athletes found for those groups.", True
        # hide any previous message
        return rows, [], "", False
    except Exception as e:
        return [], [], f"Error loading athletes: {e}", True


# When a row is selected, set the “active athlete” label/id, load complaints & comments
@app.callback(
    Output("t1-active-athlete-label", "children"),
    Output("t1-active-athlete-id", "children"),
    Output("t1-complaint-dd", "options"),
    Output("t1-comments-table", "data"),
    Output("t1-comment-hint", "children"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-athlete-table", "data"),
    prevent_initial_call=True
)
def t1_pick_athlete(selected_rows, table_data):
    if not selected_rows or selected_rows == [] or not table_data:
        return "—", "", [], [], "Select an athlete above; comments will filter to that athlete."
    row = table_data[selected_rows[0]]
    label = row.get("Athlete", "—")
    cid = None
    try:
        cid = int(row.get("_cid"))
    except Exception:
        pass
    if not cid:
        return "—", "", [], [], "Select an athlete above; comments will filter to that athlete."

    # Complaint options from Juvonno (using td.fetch_customer_complaints)
    try:
        comps = td.fetch_customer_complaints(cid)
        comp_opts = [{"label": c["Title"], "value": c["Title"]} for c in comps if c.get("Title")]
    except Exception:
        comp_opts = []

    # Comments from our own t1_comments table
    comments = t1_db_list_comments(cid)

    return label, str(cid), comp_opts, comments, f"Adding comments for: {label}"


# Save a comment (uses active athlete + selected complaint)
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    Input("t1-comment-save", "n_clicks"),
    State("t1-active-athlete-id", "children"),
    State("t1-active-athlete-label", "children"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    prevent_initial_call=True
)
def t1_save_comment(n, cid_str, label, complaint, date_str, text):
    if not n:
        raise dash.exceptions.PreventUpdate
    if not cid_str or not text or not text.strip():
        raise dash.exceptions.PreventUpdate
    try:
        cid = int(cid_str)
    except Exception:
        raise dash.exceptions.PreventUpdate

    # Compute current status at save time
    status = _current_status_for_cid(cid)

    # Author (leave blank here; wire to your auth/me if you’d like)
    author_name = ""
    author_email = ""

    t1_db_add_comment(
        customer_id=cid,
        customer_label=label or f"ID {cid}",
        date_str=date_str or datetime.utcnow().strftime("%Y-%m-%d"),
        comment=text.strip(),
        complaint=complaint or "",
        status=status or "",
        author_name=author_name,
        author_email=author_email,
    )

    # Return refreshed table
    return t1_db_list_comments(cid)


if __name__ == "__main__":
    app.run(debug=False, port=8050)
