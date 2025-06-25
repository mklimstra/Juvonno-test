import dash_bootstrap_components as dbc

class Pagination:

    def render(self, id="pagination"):
        return dbc.Pagination(
            id=id,
            max_value=1,
            active_page=1,
            fully_expanded=False,
            previous_next=True,
        )