import dash
import flask
from dash_auth_external import DashAuthExternal
import pandas as pd
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc
import requests
import math

from layout import Footer, Navbar, Pagination, GeographyFilters

from settings import *

from utils import fetch_options, fetch_profiles, restructure_profile

# creating the instance of our auth class
auth = DashAuthExternal(AUTH_URL,
                        TOKEN_URL,
                        app_url=APP_URL,
                        client_id=CLIENT_ID,
                        client_secret=CLIENT_SECRET)
server = (
    auth.server
)  # retrieving the flask server which has our redirect rules assigned

here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"

# Initialize the Dash app
app = Dash(__name__,
           server=server,

           external_stylesheets=[dbc.themes.BOOTSTRAP,
                                 "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"])

geo_filters = GeographyFilters(app, auth, id="placename")

# Offcanvas for filters
offcanvas = dbc.Offcanvas(
    [
        dbc.Label("Role"),
        dcc.Dropdown(
            id="filter-role",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Label("Campus"),
        dcc.Dropdown(
            id="filter-campus",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Label("Organization"),
        dcc.Dropdown(
            id="filter-organization",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Container([
            html.H4("Town/City"),

            dbc.Label("Scope"),
            dcc.Dropdown(
                id="filter-placename-scope",
                options=[
                    {"label": "Birthplace", "value": "birthplace"},
                    {"label": "Residence", "value": "residence"},
                    {"label": "Either", "value": "both"}
                ],
                value="birthplace",  # ← this makes “Birthplace” the default
                multi=False,
                className="mb-3",
            ),

            geo_filters.layout,
        ], className="background-light border mb-3"),

        dbc.Button("Apply", id="apply-filters", color="primary"),
    ],
    id="offcanvas-filters",
    title="Filters",
    is_open=False,
    placement="end",
)

# Toggle button for offcanvas
toggle_button = dbc.Button(html.I(className="bi bi-filter me-1"), id="open-offcanvas", n_clicks=0, color="secondary",
                           className="")

# Data table stub for results
results_table = dash_table.DataTable(
    id="results-table",
    columns=[
        {"name": "ID", "id": "id"},
        {"name": "First Name", "id": "first_name"},
        {"name": "Last Name", "id": "last_name"},
        {"name": "Sport", "id": "sport"},
        {"name": "Email", "id": "email"},
        {"name": "Enrollment", "id": "enrollment_status"}
    ],
    page_current=0,
    page_size=20,
    page_action="none",

    style_table={"overflowX": "auto"},
    style_cell={"textAlign": "left"},
)

download_button = html.Div([
    html.Button("Download CSV", id="download-csv-btn", className="btn btn-secondary", n_clicks=0),
    dcc.Download(id="download-csv")
], className="mt-3")

app.layout = html.Div([
    dcc.Store(id='table-rows-store', data=0),
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(
        id="init-interval",
        interval=500,  # e.g., 1 second after page load
        n_intervals=0,
        max_intervals=1  # This ensures it fires only once
    ),

    Navbar([]).render(),
    dbc.Container([

        offcanvas,

        dbc.Container(
            dbc.Row(
                [dbc.Col(html.H2("Registration Search", className="mb-3")),
                 dbc.Col(toggle_button, width="auto"), ],
                justify="between",
                align="center",
            ),
        ),

        html.Div(
            "Use the filters to search the Registration Dataset",
            id="no-data-msg",
            className="alert alert-info d-block",
        ),

        dbc.Container(
            [

                results_table,
                html.Div(Pagination().render(), className="mt-2"),
                download_button,
            ],
            id="table-display-container",
            fluid=True,
            className="d-none",
        )
    ]),
    Footer().render(),
])


# Callback to toggle offcanvas
@app.callback(
    Output("offcanvas-filters", "is_open"),
    Input("open-offcanvas", "n_clicks"),
    State("offcanvas-filters", "is_open"),
)
def toggle_offcanvas(n_clicks, is_open):
    if n_clicks:
        return not is_open
    return is_open


@app.callback(
    Output("redirect-to", "href"),
    Output("filter-role", "options"),
    Output("filter-campus", "options"),
    Output("filter-organization", "options"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    """
    On timeout, load filters
    """
    try:
        token = auth.get_token()
    except Exception as e:

        ## HERE, HOW DO I FORWARD A REDIRECT TO APP_URL???

        print(e)

        auth_url = f"{AUTH_URL}?response_type=code&client_id={CLIENT_ID}&redirect_uri={APP_URL}"

        print(f"Forward to {APP_URL}")

        return APP_URL, no_update, no_update, no_update

    # campus: label=name, value=id
    campus_options = fetch_options("/api/registration/campus/", token, "name", "id")

    # sport org: label=name, value=id
    org_options = fetch_options("/api/registration/organization/", token, "name", "id")

    # role: label=verbose_name, value=id
    role_options = fetch_options("/api/registration/role/", token, "verbose_name", "id")

    return no_update, role_options, campus_options, org_options


@app.callback(
    Output("download-csv", "data"),
    Input("download-csv-btn", "n_clicks"),
    State("filter-role", "value"),
    State("filter-campus", "value"),
    State("filter-organization", "value"),
    State("filter-placename-scope", "value"),
    State(f"{geo_filters.id}-province", "value"),
    State(f"{geo_filters.id}-location", "value"),
    State(f"{geo_filters.id}-placename", "value"),
    prevent_initial_call=True,
)
def download_csv(n_clicks, role, campus, org, placename_scope, province, location, placename):
    try:
        token = auth.get_token()

        # build the common filter params
        filters = {}
        if role:   filters["role_id"] = role
        if campus: filters["campus_id"] = campus
        if org:    filters["sport_org_id"] = org
        if placename_scope: filters["geography_scope"] = placename_scope
        if province: filters["province"] = province
        if location: filters["location"] = location
        if placename: filters["placename"] = placename

        records = [restructure_profile(p) for p in fetch_profiles(token, filters)]
        df = pd.DataFrame(records)
        return dcc.send_data_frame(df.to_csv, "filtered_profiles.csv", index=False)
    except Exception as e:
        print(e)
        raise PreventUpdate


@app.callback(
    Output("results-table", "data"),
    Output("pagination", "max_value"),
    Output("pagination", "active_page"),
    Output("table-rows-store", "data"),
    Input("apply-filters", "n_clicks"),
    Input("pagination", "active_page"),
    Input("results-table", "page_size"),
    State("filter-role", "value"),
    State("filter-campus", "value"),
    State("filter-organization", "value"),
    State("filter-placename-scope", "value"),
    State(f"{geo_filters.id}-province", "value"),
    State(f"{geo_filters.id}-location", "value"),
    State(f"{geo_filters.id}-placename", "value"),
)
def apply_filters(n_clicks, active_page, page_size, role_id, campus_id, org_id, placename_scope, province, location,
                  placename):
    try:
        token = auth.get_token()
    except Exception as e:
        raise PreventUpdate
    if not n_clicks:
        raise PreventUpdate

    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    # Reset to first page on applying filters
    page = 1 if trigger_id == "apply-filters" else (active_page or 1)

    headers = {"Authorization": f"Bearer {token}"}
    # build only the params the user set
    params = {}
    params["role_id"] = role_id if role_id else None
    params["campus_id"] = campus_id if campus_id else None
    params["sport_org_id"] = org_id if org_id else None
    if placename_scope: params["geography_scope"] = placename_scope
    if province: params["province"] = province
    if location: params["location"] = location
    if placename: params["placename"] = placename

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    # fetch the filtered profiles list
    resp = requests.get(f"{SITE_URL}/api/registration/profile/", headers=headers, params=params, timeout=5)
    resp.raise_for_status()
    payload = resp.json()

    total = payload.get("count", 0)
    total_pages = math.ceil(total / page_size) if total else 1

    # Return raw JSON as a string
    people_res = [restructure_profile(p) for p in payload.get("results", [])]

    return people_res, total_pages, page, len(people_res)


@app.callback(
    Output("no-data-msg", "className"),
    Output("no-data-msg", "children"),
    Output("table-display-container", "className"),
    Input("table-rows-store", "data"),
)
def row_callback(rows):
    print(f"Rows: {rows}")

    if not rows:
        rv = "alert alert-warning d-block", "No Rows Found!", "d-none"
    else:
        rv = "d-none", "", ""

    return rv


if __name__ == "__main__":
    app.run(debug=True, port=8050)
