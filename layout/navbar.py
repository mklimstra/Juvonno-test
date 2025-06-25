from dash import html
import dash_bootstrap_components as dbc

class Navbar():

    def __init__(self, buttons=[], id="navbar"):
        self.buttons = buttons
        self.navlinks = []
        self.id= id

    def nav_item(self, label, url):
        return dbc.NavItem(dbc.NavLink(label, href=url))

    def render(self):
        return dbc.Navbar(
            dbc.Container(
                [
                    # Brand / logo
                    dbc.NavbarBrand(
                        [
                            html.Img(src="assets/img/csi-pacific-logo-reverse.png", height="40px"),
                            html.Span("CSI Pacific APPS Registration", className="ms-2 h5 mb-0")
                        ],
                        href="#",
                        className="d-flex align-items-center text-white text-decoration-none"
                    ),

                    # Nav links
                    # dbc.Nav(
                    #     self.navlinks,
                    #     className="col-12 col-lg-auto me-lg-auto mb-2 justify-content-center mb-md-0",
                    #     navbar=True,
                    # ),

                    # Search form
                    # dbc.Form(
                    #     dbc.Input(type="search", placeholder="Search...", className="form-control-dark text-bg-dark"),
                    #     className="col-12 col-lg-auto mb-3 mb-lg-0 me-lg-3",
                    #     navbar=True,
                    # ),

                    # Buttons
                    html.Div(
                        self.buttons,
                        className="text-end",
                    ),
                ]
            ),
            color="dark",
            dark=True,
            className="p-3",
            id=self.id,
        )