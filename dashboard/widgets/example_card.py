"""Example interactive widget for the shared Lite MCP SQLite database.

This is intentionally ordinary, editable Streamlit code. It demonstrates the
public dashboard database API without hiding application behavior in a custom
widget framework.
"""

from __future__ import annotations

import math
from typing import Any

import streamlit as st
from lite_mcp_dashboard import DashboardContext, DashboardDatabaseError

TITLE = "Example entries"
ORDER = 100

_TABLE = "dashboard_example_entries"
_EDITOR_KEY = "example-entry-editor"
_EDITOR_DATA_KEY = "example-entry-editor-data"
_RESET_WIDGETS_KEY = "example-entry-reset-widgets"
_EXPECTED_DATABASE_ERRORS = frozenset(
    {
        "constraint_violation",
        "database_busy",
        "database_io",
        "result_too_large",
    }
)


class _ValidationError(Exception):
    """A value the user can correct without restarting Lite MCP."""


class _ConflictError(Exception):
    """The displayed row changed in another dashboard session."""


def _handle_database_error(error: DashboardDatabaseError) -> None:
    if error.code not in _EXPECTED_DATABASE_ERRORS:
        raise error

    retry_message = (
        "This operation can be retried."
        if error.retryable
        else "Fix the input or widget before trying again."
    )
    message = (
        f"{error.message}\n\n"
        f"Error code: `{error.code}` · Trace ID: `{error.trace_id}`\n\n"
        f"{retry_message}"
    )
    if error.recovery_hint:
        message += f" {error.recovery_hint}"
    st.error(message)


def _show_conflict(error: _ConflictError) -> None:
    st.warning(f"{error} Refresh the page and apply your change again.")


def _finish_write(message: str) -> None:
    st.session_state["example-entry-flash"] = message
    st.session_state[_RESET_WIDGETS_KEY] = True
    st.rerun()


def _reset_widget_state_if_requested() -> None:
    if not st.session_state.pop(_RESET_WIDGETS_KEY, False):
        return
    for key in (
        _EDITOR_KEY,
        _EDITOR_DATA_KEY,
        "example-entry-delete-id",
        "example-entry-delete-confirm",
    ):
        st.session_state.pop(key, None)


def _validated_label(value: Any) -> str:
    label = str(value).strip()
    if not label:
        raise _ValidationError("Label is required.")
    if len(label) > 80:
        raise _ValidationError("Label must be 80 characters or fewer.")
    return label


def _validated_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise _ValidationError("Value must be a number.") from error
    if not math.isfinite(number):
        raise _ValidationError("Value must be finite.")
    return number


def _load_entries(context: DashboardContext):
    return context.db.query(
        f"""
        SELECT id, label, value, version, created_at, updated_at
        FROM {_TABLE}
        ORDER BY id
        """,
        (),
    )


def _render_summary(entries) -> None:
    metric_col, total_col = st.columns(2)
    metric_col.metric("Entries", len(entries.index))
    total = float(entries["value"].sum()) if not entries.empty else 0.0
    total_col.metric("Total value", f"{total:,.2f}")

    if entries.empty:
        st.info("No entries yet. Use the form below to add the first one.")
        return

    st.dataframe(
        entries[["label", "value", "created_at", "updated_at"]],
        hide_index=True,
        width="stretch",
    )
    st.bar_chart(entries, x="label", y="value")


def _render_create(context: DashboardContext) -> None:
    st.subheader("Add an entry")
    with st.form("example-entry-create", clear_on_submit=True):
        label = st.text_input("Label", max_chars=80)
        value = st.number_input("Value", value=0.0)
        submitted = st.form_submit_button("Add entry", type="primary")

    if not submitted:
        return

    try:
        clean_label = _validated_label(label)
        clean_value = _validated_value(value)
        context.db.execute(
            f"INSERT INTO {_TABLE} (label, value) VALUES (?, ?)",
            (clean_label, clean_value),
        )
    except _ValidationError as error:
        st.warning(str(error))
    except DashboardDatabaseError as error:
        _handle_database_error(error)
    else:
        _finish_write("Entry added.")


def _changed_rows(original, edited) -> list[tuple[int, int, str, float]]:
    original_by_id = original.set_index("id")
    changes: list[tuple[int, int, str, float]] = []

    for row in edited.itertuples(index=False):
        entry_id = int(row.id)
        before = original_by_id.loc[entry_id]
        label = _validated_label(row.label)
        value = _validated_value(row.value)
        if label != str(before["label"]) or value != float(before["value"]):
            changes.append((entry_id, int(before["version"]), label, value))

    return changes


def _editor_data(entries):
    if _EDITOR_DATA_KEY not in st.session_state:
        st.session_state[_EDITOR_DATA_KEY] = entries.copy(deep=True)
    return st.session_state[_EDITOR_DATA_KEY]


def _render_update(context: DashboardContext, entries) -> None:
    if entries.empty:
        return

    st.subheader("Edit entries")
    st.caption("Changes are written only after you select Save changes.")
    edited = st.data_editor(
        entries,
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        disabled=["id", "version", "created_at", "updated_at"],
        column_config={
            "id": st.column_config.NumberColumn("ID", format="%d"),
            "label": st.column_config.TextColumn("Label", max_chars=80, required=True),
            "value": st.column_config.NumberColumn("Value", required=True),
            "version": None,
            "created_at": st.column_config.TextColumn("Created", disabled=True),
            "updated_at": st.column_config.TextColumn("Updated", disabled=True),
        },
        key=_EDITOR_KEY,
    )

    save_col, reload_col = st.columns([1, 1])
    save_requested = save_col.button("Save changes", key="example-entry-save")
    reload_requested = reload_col.button(
        "Reload editor",
        help="Discard unsaved edits and load the latest rows from data/data.db.",
        key="example-entry-reload",
    )
    if reload_requested:
        st.session_state[_RESET_WIDGETS_KEY] = True
        st.rerun()
    if not save_requested:
        return

    try:
        changes = _changed_rows(entries, edited)
        if not changes:
            raise _ValidationError("There are no changes to save.")

        with context.db.transaction() as transaction:
            for entry_id, version, label, value in changes:
                result = transaction.execute(
                    f"""
                    UPDATE {_TABLE}
                    SET label = ?, value = ?, version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND version = ?
                    """,
                    (label, value, entry_id, version),
                )
                if result.affected_rows != 1:
                    raise _ConflictError(
                        f"Entry {entry_id} was changed or deleted by another session."
                    )
    except _ValidationError as error:
        st.warning(str(error))
    except _ConflictError as error:
        _show_conflict(error)
    except DashboardDatabaseError as error:
        _handle_database_error(error)
    else:
        _finish_write(
            f"Saved {len(changes)} changed entr{'y' if len(changes) == 1 else 'ies'}."
        )


def _render_delete(context: DashboardContext, entries) -> None:
    if entries.empty:
        return

    st.subheader("Delete an entry")
    rows_by_id = entries.set_index("id")
    entry_ids = [int(entry_id) for entry_id in entries["id"].tolist()]
    selected_id = st.selectbox(
        "Entry",
        entry_ids,
        format_func=lambda entry_id: f"{entry_id}: {rows_by_id.loc[entry_id, 'label']}",
        key="example-entry-delete-id",
    )
    confirmed = st.checkbox(
        "I understand this permanently deletes the selected entry.",
        key="example-entry-delete-confirm",
    )

    if not st.button(
        "Delete entry",
        disabled=not confirmed,
        key="example-entry-delete",
    ):
        return

    try:
        version = int(rows_by_id.loc[selected_id, "version"])
        result = context.db.execute(
            f"DELETE FROM {_TABLE} WHERE id = ? AND version = ?",
            (selected_id, version),
        )
        if result.affected_rows != 1:
            raise _ConflictError(
                f"Entry {selected_id} was changed or deleted by another session."
            )
    except _ConflictError as error:
        _show_conflict(error)
    except DashboardDatabaseError as error:
        _handle_database_error(error)
    else:
        _finish_write("Entry deleted.")


def render(context: DashboardContext) -> None:
    """Render the example widget using only the public dashboard context."""

    _reset_widget_state_if_requested()

    st.header(TITLE)
    st.write(
        "A small CRUD example backed by `data/data.db`. It uses parameterized SQL, "
        "explicit saves, and optimistic concurrency."
    )

    flash = st.session_state.pop("example-entry-flash", None)
    if flash:
        st.success(flash)

    try:
        entries = _load_entries(context)
    except DashboardDatabaseError as error:
        _handle_database_error(error)
        return

    _render_summary(entries)
    st.divider()
    _render_create(context)
    editable_entries = _editor_data(entries)
    _render_update(context, editable_entries)
    _render_delete(context, editable_entries)
