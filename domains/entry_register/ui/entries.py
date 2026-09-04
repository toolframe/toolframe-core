"""Human decision surface for the example Entry Register domain."""

from __future__ import annotations

from typing import Any

import streamlit as st
from toolframe_dashboard import (
    DashboardContext,
    DashboardDatabaseError,
    DashboardInteractionError,
    render_error_card,
)

from domains.entry_register.model.entries import (
    EntryConflictError,
    EntryValidationError,
    add_entry,
    list_entries,
    update_entry,
    validate_label,
    validate_value,
)

TITLE = "Example entries"
ORDER = 100

_EDITOR_KEY = "entry-register-editor"
_EDITOR_DATA_KEY = "entry-register-editor-data"
_RESET_KEY = "entry-register-reset"
_FLASH_KEY = "entry-register-flash"


def _interaction_error(error: Exception) -> DashboardInteractionError:
    if isinstance(error, EntryConflictError):
        return DashboardInteractionError(
            "entry_register_version_conflict",
            str(error),
            recovery_hint="Reload the card and apply the change to the latest entry.",
        )
    return DashboardInteractionError(
        "entry_register_invalid_input",
        str(error),
        recovery_hint="Correct the highlighted entry values and try again.",
    )


def _finish_write(message: str) -> None:
    st.session_state[_FLASH_KEY] = message
    st.session_state[_RESET_KEY] = True
    st.rerun()


def _reset_editor_if_requested() -> None:
    if st.session_state.pop(_RESET_KEY, False):
        st.session_state.pop(_EDITOR_KEY, None)
        st.session_state.pop(_EDITOR_DATA_KEY, None)


def _render_summary(entries: Any) -> None:
    count_column, total_column = st.columns(2)
    count_column.metric("Entries", len(entries.index))
    total = float(entries["value"].sum()) if not entries.empty else 0.0
    total_column.metric("Total value", f"{total:,.2f}")
    if entries.empty:
        st.info("No entries yet. Use the form below to add the first one.")
        return
    st.dataframe(
        entries[["label", "value", "created_at", "updated_at"]],
        hide_index=True,
        width="stretch",
    )


def _render_create(context: DashboardContext) -> None:
    st.subheader("Add an entry")
    with st.form("entry-register-create", clear_on_submit=True):
        label = st.text_input("Label", max_chars=80)
        value = st.number_input("Value", value=0.0)
        submitted = st.form_submit_button("Add entry", type="primary")
    if not submitted:
        return
    try:
        entry_id = add_entry(context.db, label=label, value=value)
    except EntryValidationError as error:
        render_error_card(_interaction_error(error))
    except DashboardDatabaseError as error:
        render_error_card(error)
    else:
        _finish_write(f"Entry {entry_id} added.")


def _changed_rows(original: Any, edited: Any) -> list[tuple[int, int, str, float]]:
    original_by_id = original.set_index("id")
    changes: list[tuple[int, int, str, float]] = []
    for row in edited.itertuples(index=False):
        entry_id = int(row.id)
        before = original_by_id.loc[entry_id]
        label = validate_label(row.label)
        value = validate_value(row.value)
        if label != str(before["label"]) or value != float(before["value"]):
            changes.append((entry_id, int(before["version"]), label, value))
    return changes


def _render_editor(context: DashboardContext, entries: Any) -> None:
    if entries.empty:
        return
    if _EDITOR_DATA_KEY not in st.session_state:
        st.session_state[_EDITOR_DATA_KEY] = entries.copy(deep=True)
    original = st.session_state[_EDITOR_DATA_KEY]

    st.subheader("Edit entries")
    edited = st.data_editor(
        original,
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        disabled=["id", "version", "created_at", "updated_at"],
        key=_EDITOR_KEY,
    )
    save_column, reload_column = st.columns(2)
    save_requested = save_column.button("Save changes")
    reload_requested = reload_column.button("Reload editor")
    if reload_requested:
        st.session_state[_RESET_KEY] = True
        st.rerun()
    if not save_requested:
        return

    try:
        changes = _changed_rows(original, edited)
        if not changes:
            raise EntryValidationError("There are no changes to save.")
        with context.db.transaction() as transaction:
            for entry_id, version, label, value in changes:
                update_entry(
                    transaction,
                    entry_id=entry_id,
                    expected_version=version,
                    label=label,
                    value=value,
                )
    except (EntryValidationError, EntryConflictError) as error:
        render_error_card(_interaction_error(error))
    except DashboardDatabaseError as error:
        render_error_card(error)
    else:
        _finish_write(f"Saved {len(changes)} changed entries.")


def render(context: DashboardContext) -> None:
    """Render one bounded example register using the domain model."""

    _reset_editor_if_requested()
    st.header(TITLE)
    st.caption(
        "A small example domain with parameterized writes and version-guarded edits."
    )
    if flash := st.session_state.pop(_FLASH_KEY, None):
        st.success(flash)
    try:
        entries = list_entries(context.db, limit=1_000)
    except DashboardDatabaseError as error:
        render_error_card(error)
        return
    _render_summary(entries)
    st.divider()
    _render_create(context)
    _render_editor(context, entries)
