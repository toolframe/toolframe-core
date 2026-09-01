"""Editable Streamlit entry point for Lite MCP.

Add a Python module to ``widgets/`` and export ``render(context)`` to place a
new widget on this dashboard. The public ``lite_mcp_dashboard`` package owns
discovery, validation, ordering, and the shared runtime context.
"""

import streamlit as st
from lite_mcp_dashboard import render_widgets

st.set_page_config(page_title="Lite MCP", page_icon="🧰", layout="wide")

st.title("Lite MCP Dashboard")
st.caption("Edit dashboard/app.py or add Python modules under dashboard/widgets/.")
st.warning(
    "This dashboard has no authentication. Anyone who can reach this Lite MCP "
    "server on your LAN can view its data and use the interactions you expose."
)

render_widgets()
