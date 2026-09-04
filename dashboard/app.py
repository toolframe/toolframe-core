"""Editable Streamlit entry point for an app built on Toolframe.

Add a Python module to ``widgets/`` and export ``render(context)`` to place a
new widget on this dashboard. The public ``toolframe_dashboard`` package owns
discovery, validation, ordering, and the shared runtime context.
"""

import streamlit as st
from toolframe_dashboard import render_widgets

APP_NAME = "Unnamed"

NO_AUTH_WARNING_HTML = r"""
<style>
  #toolframe-no-auth-warning[hidden] {
    display: none;
  }

  #toolframe-no-auth-warning {
    align-items: flex-start;
    background: rgba(255, 193, 7, 0.16);
    border: 1px solid rgba(255, 193, 7, 0.38);
    border-radius: 0.5rem;
    box-sizing: border-box;
    display: flex;
    gap: 0.75rem;
    justify-content: space-between;
    margin: 0 0 1rem;
    padding: 0.75rem 1rem;
    width: 100%;
  }

  #toolframe-no-auth-warning-message {
    align-items: flex-start;
    display: flex;
    gap: 0.65rem;
    line-height: 1.5;
  }

  #toolframe-no-auth-warning-icon {
    flex: 0 0 auto;
  }

  #toolframe-no-auth-warning-dismiss {
    background: transparent;
    border: 1px solid currentColor;
    border-radius: 0.35rem;
    color: inherit;
    cursor: pointer;
    flex: 0 0 auto;
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    opacity: 0.78;
    padding: 0.25rem 0.55rem;
  }

  #toolframe-no-auth-warning-dismiss:hover {
    opacity: 1;
  }

  #toolframe-no-auth-warning-dismiss:focus-visible {
    outline: 2px solid currentColor;
    outline-offset: 2px;
  }

  @media (max-width: 640px) {
    #toolframe-no-auth-warning {
      flex-direction: column;
    }
  }
</style>
<div
  id="toolframe-no-auth-warning"
  data-storage-key="toolframe.no-auth-warning.dismissed"
  role="alert"
  hidden
>
  <div id="toolframe-no-auth-warning-message">
    <span id="toolframe-no-auth-warning-icon" aria-hidden="true">⚠️</span>
    <span>
      This dashboard has no authentication. Anyone who can reach this app on
      your LAN can view its data and use the interactions you expose.
    </span>
  </div>
  <button
    id="toolframe-no-auth-warning-dismiss"
    type="button"
    title="Hide this warning permanently in this browser"
  >
    Don't show again
  </button>
</div>
<script>
  (() => {
    const warning = document.getElementById("toolframe-no-auth-warning");
    const dismiss = document.getElementById("toolframe-no-auth-warning-dismiss");
    if (!warning || !dismiss) return;

    const storageKey = warning.dataset.storageKey;
    let wasDismissed = false;
    try {
      wasDismissed = window.localStorage.getItem(storageKey) === "true";
    } catch (_error) {
      // Keep the warning visible when browser storage is unavailable.
    }

    if (wasDismissed) {
      warning.remove();
      return;
    }

    warning.hidden = false;
    dismiss.addEventListener("click", () => {
      try {
        window.localStorage.setItem(storageKey, "true");
      } catch (_error) {
        // The button still closes the current warning for this page load.
      }
      warning.remove();
    });
  })();
</script>
"""

st.set_page_config(page_title=APP_NAME, page_icon="🧰", layout="wide")

st.title(APP_NAME)
st.caption("Powered by Toolframe · Edit dashboard/app.py or add Python widgets.")
if APP_NAME == "Unnamed":
    st.warning(
        "This app is still unnamed. Ask your development agent to suggest a "
        "business-specific name, then replace APP_NAME in dashboard/app.py."
    )
st.html(NO_AUTH_WARNING_HTML, unsafe_allow_javascript=True)

render_widgets()
