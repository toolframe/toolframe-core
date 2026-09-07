import streamlit as st
from toolframe_dashboard import render_widgets

st.set_page_config(page_title="Stockroom", layout="wide")
st.title("Stockroom")
st.caption("Record a delivery once and share its stock totals with the team.")
render_widgets()
