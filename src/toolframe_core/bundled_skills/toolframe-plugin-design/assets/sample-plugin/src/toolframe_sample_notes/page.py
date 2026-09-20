from toolframe_sdk import PageContext, PluginError


def render(context: PageContext):
    import streamlit as st

    service = context.services.require("sample-notes.notes", version=1)
    st.title("Notes")
    with st.form("sample-notes-capture"):
        body = st.text_area("New note", max_chars=1000, key="sample-notes-body")
        if st.form_submit_button(
            "Capture", disabled=not context.can("sample-notes.write")
        ):
            try:
                service.call("capture", {"body": body})
            except PluginError as error:
                st.error(error.message)
            else:
                st.rerun()
    try:
        result = service.call("list_notes", {"limit": 20})
    except PluginError as error:
        st.error(error.message)
        return
    st.dataframe(result["notes"], hide_index=True, width="stretch")
