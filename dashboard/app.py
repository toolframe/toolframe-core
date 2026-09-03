"""Editable Streamlit entry point for an app built on Lite MCP.

Add a Python module to ``widgets/`` and export ``render(context)`` to place a
new widget on this dashboard. The public ``lite_mcp_dashboard`` package owns
discovery, validation, ordering, and the shared runtime context.
"""

import streamlit as st
from lite_mcp_dashboard import render_widgets

APP_NAME = "Unnamed"

st.set_page_config(page_title=APP_NAME, page_icon="🧰", layout="wide")

st.title(APP_NAME)
st.caption("Powered by Lite MCP · Edit dashboard/app.py or add Python widgets.")
if APP_NAME == "Unnamed":
    st.warning(
        "This app is still unnamed. Ask your development agent to suggest a "
        "business-specific name, then replace APP_NAME in dashboard/app.py."
    )
st.warning(
    "This dashboard has no authentication. Anyone who can reach this app on "
    "your LAN can view its data and use the interactions you expose."
)

render_widgets()
