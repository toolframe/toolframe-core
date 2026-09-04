"""Editable Streamlit composition root for an app built on Toolframe.

Toolframe discovers every public ``domains/*/ui/*.py`` widget. This file owns
the page composition and may freely use or replace that registry behavior.
"""

import streamlit as st
from toolframe_dashboard import render_widgets

APP_NAME = "Unnamed"

st.set_page_config(page_title=APP_NAME, page_icon="🧰", layout="wide")
st.title(APP_NAME)
st.caption("Powered by Toolframe · Edit app/dashboard.py or a domain UI module.")
if APP_NAME == "Unnamed":
    st.warning(
        "This app is still unnamed. Ask the master development agent to suggest "
        "a business-specific name, then update the app context and peer guide."
    )
st.warning(
    "No authentication: anyone who can reach this LAN app can view its data "
    "and use the interactions it exposes.",
    icon="⚠️",
)

render_widgets()
