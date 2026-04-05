import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table, Input, Output, State, callback
import plotly.graph_objects as go
import pandas as pd
import numpy as np

import timescaledb_model as tsdb
from etl import store_files

from loguru import logger

external_stylesheets = [dbc.themes.BOOTSTRAP]
app = dash.Dash(
    __name__,
    title="Bourse Dashboard",
    suppress_callback_exceptions=True,
    external_stylesheets=external_stylesheets,
)
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", "database", "password")

# =====================================================================
# Helper functions to query the database
# =====================================================================


def get_companies():
    """Get list of companies from the database."""
    df = db.df_query("SELECT id, name, symbol FROM companies ORDER BY name")
    return df


def get_daystocks(cids, start_date, end_date):
    """Get daily stock data for given company ids and date range."""
    if not cids:
        return pd.DataFrame()
    cid_list = ",".join(str(c) for c in cids)
    query = """
        SELECT d.date, d.cid, d.open, d.close, d.high, d.low, d.volume, d.mean, d.std, c.name
        FROM daystocks d
        JOIN companies c ON c.id = d.cid
        WHERE d.cid IN (%s) AND d.date >= '%s' AND d.date <= '%s'
        ORDER BY d.date
    """ % (cid_list, start_date, end_date)
    df = db.df_query(query, parse_dates=["date"])
    return df


def get_stocks_intraday(cids, start_date, end_date):
    """Get intraday stock data."""
    if not cids:
        return pd.DataFrame()
    cid_list = ",".join(str(c) for c in cids)
    query = """
        SELECT s.date, s.cid, s.value, s.volume, c.name
        FROM stocks s
        JOIN companies c ON c.id = s.cid
        WHERE s.cid IN (%s) AND s.date >= '%s' AND s.date <= '%s'
        ORDER BY s.date
    """ % (cid_list, start_date, end_date)
    df = db.df_query(query, parse_dates=["date"])
    return df


# =====================================================================
# Layout
# =====================================================================

app.layout = dbc.Container(
    [
        dbc.Row(
            dbc.Col(html.H1("Bourse Dashboard", className="text-center my-4")),
        ),
        # Controls row
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.Label("Actions", className="fw-bold"),
                        dcc.Dropdown(
                            id="stock-selector",
                            multi=True,
                            placeholder="Choisir une ou plusieurs actions...",
                        ),
                    ],
                    md=4,
                ),
                dbc.Col(
                    [
                        html.Label("Periode", className="fw-bold"),
                        dcc.DatePickerRange(
                            id="date-range",
                            display_format="YYYY-MM-DD",
                            start_date_placeholder_text="Debut",
                            end_date_placeholder_text="Fin",
                        ),
                    ],
                    md=3,
                ),
                dbc.Col(
                    [
                        html.Label("Type de graphique", className="fw-bold"),
                        dbc.RadioItems(
                            id="chart-type",
                            options=[
                                {"label": "Ligne", "value": "line"},
                                {"label": "Chandeliers", "value": "candlestick"},
                            ],
                            value="line",
                            inline=True,
                        ),
                    ],
                    md=2,
                ),
                dbc.Col(
                    [
                        html.Label("Echelle", className="fw-bold"),
                        dbc.RadioItems(
                            id="scale-type",
                            options=[
                                {"label": "Lineaire", "value": "linear"},
                                {"label": "Logarithmique", "value": "log"},
                            ],
                            value="log",
                            inline=True,
                        ),
                    ],
                    md=2,
                ),
            ],
            className="mb-4",
        ),
        # Tabs
        dbc.Tabs(
            [
                dbc.Tab(label="Cours", tab_id="tab-cours"),
                dbc.Tab(label="Bollinger", tab_id="tab-bollinger"),
                dbc.Tab(label="Donnees brutes", tab_id="tab-data"),
                dbc.Tab(label="Performance comparee", tab_id="tab-performance"),
            ],
            id="tabs",
            active_tab="tab-cours",
            className="mb-3",
        ),
        html.Div(id="tab-content"),
    ],
    fluid=True,
)


# =====================================================================
# Callbacks
# =====================================================================


@callback(
    Output("stock-selector", "options"),
    Output("date-range", "min_date_allowed"),
    Output("date-range", "max_date_allowed"),
    Output("date-range", "start_date"),
    Output("date-range", "end_date"),
    Input("stock-selector", "id"),  # fires once on load
)
def init_controls(_):
    """Populate the stock selector and date range on page load."""
    companies = get_companies()
    logger.info(f"Found {len(companies)} companies in database")
    options = [{"label": row["name"], "value": row["id"]} for _, row in companies.iterrows()]

    # Try daystocks first, fallback to stocks
    dates = db.df_query("SELECT MIN(date) as min_d, MAX(date) as max_d FROM daystocks")
    if dates.empty or pd.isna(dates["min_d"].iloc[0]):
        dates = db.df_query("SELECT MIN(date) as min_d, MAX(date) as max_d FROM stocks")

    if dates.empty or pd.isna(dates["min_d"].iloc[0]):
        logger.warning("No date data found in daystocks or stocks")
        return options, None, None, None, None

    min_d = pd.to_datetime(dates["min_d"].iloc[0]).date()
    max_d = pd.to_datetime(dates["max_d"].iloc[0]).date()
    logger.info(f"Date range: {min_d} to {max_d}")
    return options, str(min_d), str(max_d), str(min_d), str(max_d)


@callback(
    Output("tab-content", "children"),
    Input("tabs", "active_tab"),
    Input("stock-selector", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
    Input("scale-type", "value"),
)
def render_tab(active_tab, selected_stocks, start_date, end_date, chart_type, scale_type):
    """Render the content of the active tab."""
    if not selected_stocks or not start_date or not end_date:
        return dbc.Alert("Selectionnez au moins une action et une periode.", color="info")

    cids = selected_stocks if isinstance(selected_stocks, list) else [selected_stocks]

    if active_tab == "tab-cours":
        return render_cours(cids, start_date, end_date, chart_type, scale_type)
    elif active_tab == "tab-bollinger":
        return render_bollinger(cids, start_date, end_date, scale_type)
    elif active_tab == "tab-data":
        return render_data_table(cids, start_date, end_date)
    elif active_tab == "tab-performance":
        return render_performance(cids, start_date, end_date)

    return html.Div()


# =====================================================================
# Tab renderers
# =====================================================================


def render_cours(cids, start_date, end_date, chart_type, scale_type):
    """Render the stock price chart (line or candlestick)."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("Aucune donnee pour cette selection.", color="warning")

    fig = go.Figure()

    for name, group in df.groupby("name"):
        group = group.sort_values("date")
        if chart_type == "candlestick":
            fig.add_trace(
                go.Candlestick(
                    x=group["date"],
                    open=group["open"],
                    high=group["high"],
                    low=group["low"],
                    close=group["close"],
                    name=name,
                )
            )
        else:
            fig.add_trace(
                go.Scatter(
                    x=group["date"],
                    y=group["close"],
                    mode="lines",
                    name=name,
                )
            )

    fig.update_layout(
        title="Cours des actions",
        xaxis_title="Date",
        yaxis_title="Prix",
        yaxis_type=scale_type,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis_rangeslider_visible=False,
        height=600,
    )

    return dcc.Graph(figure=fig)


def render_bollinger(cids, start_date, end_date, scale_type):
    """Render Bollinger Bands for the first selected stock."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("Aucune donnee pour cette selection.", color="warning")

    # Build a selector for which stock to show Bollinger bands
    stock_names = df[["cid", "name"]].drop_duplicates()
    tabs_content = []

    for _, stock_row in stock_names.iterrows():
        cid = stock_row["cid"]
        name = stock_row["name"]
        group = df[df["cid"] == cid].sort_values("date").copy()

        # Compute Bollinger Bands (20-day SMA, 2 std dev)
        window = 20
        group["sma"] = group["close"].rolling(window=window).mean()
        group["std_val"] = group["close"].rolling(window=window).std()
        group["upper"] = group["sma"] + 2 * group["std_val"]
        group["lower"] = group["sma"] - 2 * group["std_val"]

        fig = go.Figure()

        # Upper band
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["upper"],
                mode="lines",
                line=dict(width=1, color="rgba(100,100,100,0.3)"),
                name="Bande superieure",
            )
        )
        # Lower band (fill between)
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["lower"],
                mode="lines",
                line=dict(width=1, color="rgba(100,100,100,0.3)"),
                fill="tonexty",
                fillcolor="rgba(100,149,237,0.15)",
                name="Bande inferieure",
            )
        )
        # SMA
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["sma"],
                mode="lines",
                line=dict(width=1.5, color="orange", dash="dash"),
                name=f"SMA {window}j",
            )
        )
        # Close price
        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=group["close"],
                mode="lines",
                line=dict(width=2, color="blue"),
                name="Cours",
            )
        )

        fig.update_layout(
            title=f"Bandes de Bollinger - {name}",
            xaxis_title="Date",
            yaxis_title="Prix",
            yaxis_type=scale_type,
            hovermode="x unified",
            height=500,
        )

        tabs_content.append(dbc.Tab(label=name, children=[dcc.Graph(figure=fig)]))

    if len(tabs_content) == 1:
        return tabs_content[0].children

    return dbc.Tabs(tabs_content)


def render_data_table(cids, start_date, end_date):
    """Render raw data table with daily stats: min, max, open, close, mean, std."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("Aucune donnee pour cette selection.", color="warning")

    # Build the table: one row per day per stock
    df = df.sort_values(["date", "name"])
    table_df = pd.DataFrame(
        {
            "Date": df["date"].dt.strftime("%Y-%m-%d"),
            "Action": df["name"],
            "Ouverture": df["open"].round(4),
            "Fermeture": df["close"].round(4),
            "Min": df["low"].round(4),
            "Max": df["high"].round(4),
            "Moyenne": df["mean"].round(4),
            "Ecart-type": df["std"].round(4),
            "Volume": df["volume"].astype(int),
        }
    )

    return dash_table.DataTable(
        data=table_df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in table_df.columns],
        page_size=25,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
        style_cell={"textAlign": "center", "padding": "8px", "fontSize": "14px"},
        style_header={"backgroundColor": "#f8f9fa", "fontWeight": "bold"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#f2f2f2"}
        ],
        export_format="csv",
    )


def render_performance(cids, start_date, end_date):
    """Custom feature: normalized performance comparison (% change from start)."""
    df = get_daystocks(cids, start_date, end_date)
    if df.empty:
        return dbc.Alert("Aucune donnee pour cette selection.", color="warning")

    fig = go.Figure()

    for name, group in df.groupby("name"):
        group = group.sort_values("date")
        first_close = group["close"].iloc[0]
        if first_close == 0 or pd.isna(first_close):
            continue
        perf = ((group["close"] / first_close) - 1) * 100

        fig.add_trace(
            go.Scatter(
                x=group["date"],
                y=perf,
                mode="lines",
                name=name,
            )
        )

    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    fig.update_layout(
        title="Performance comparee (% depuis le debut de la periode)",
        xaxis_title="Date",
        yaxis_title="Variation (%)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=600,
    )

    # Also show a volume subplot
    vol_fig = go.Figure()
    for name, group in df.groupby("name"):
        group = group.sort_values("date")
        vol_fig.add_trace(
            go.Bar(
                x=group["date"],
                y=group["volume"],
                name=name,
                opacity=0.7,
            )
        )

    vol_fig.update_layout(
        title="Volume d'echanges",
        xaxis_title="Date",
        yaxis_title="Volume",
        barmode="group",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=400,
    )

    return html.Div([dcc.Graph(figure=fig), dcc.Graph(figure=vol_fig)])


# =====================================================================
# Main
# =====================================================================

if __name__ == "__main__":
    logger.info("Importing data into the database")
    store_files("2020-05-01", "2022-09-16", "euronext", db)
    store_files("2020-01-01", "2022-01-01", "bourso", db)
    logger.info("Import done")
    logger.info("Starting dashboard server")
    app.run(host="0.0.0.0", port=8050, debug=False)
