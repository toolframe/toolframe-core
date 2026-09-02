import importlib.util
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pandas as pd

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_dashboard as dashboard


class FakeAuditSink:
    def __init__(self):
        self.events = []

    def emit(self, channel, event, level="INFO", payload=None, **fields):
        record = {
            "channel": channel,
            "event": event,
            "level": level,
            "payload": payload,
            **fields,
        }
        self.events.append(record)
        return record


class FailingAuditSink:
    def emit(self, channel, event, level="INFO", payload=None, **fields):
        raise RuntimeError("audit unavailable")


class DashboardDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "data.db"
        sqlite3.connect(self.database_path).close()
        self.logger = logging.getLogger(f"dashboard-sdk-test.{id(self)}")
        self.database = dashboard.DashboardDatabase(
            self.database_path, logger=self.logger
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_query_uses_bound_parameters_and_returns_dataframe(self):
        self.database.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
        value = "Ada'); DROP TABLE people; --"
        self.database.execute("INSERT INTO people(name) VALUES (?)", (value,))

        frame = self.database.query(
            "SELECT id, name FROM people WHERE name = ?", (value,)
        )

        self.assertIsInstance(frame, pd.DataFrame)
        self.assertEqual(frame.to_dict("records"), [{"id": 1, "name": value}])
        self.assertEqual(
            self.database.query("SELECT COUNT(*) AS count FROM people").iloc[0][
                "count"
            ],
            1,
        )

    def test_query_rejects_writes_without_applying_them(self):
        self.database.execute("CREATE TABLE values_table (value INTEGER)")

        with self.assertRaises(dashboard.DashboardDatabaseError) as raised:
            self.database.query(
                "INSERT INTO values_table(value) VALUES (1) RETURNING value"
            )

        self.assertEqual(raised.exception.code, "read_only_required")
        count = self.database.query("SELECT COUNT(*) AS count FROM values_table").iloc[
            0
        ]["count"]
        self.assertEqual(count, 0)

    def test_execute_returns_mutation_and_returning_metadata(self):
        self.database.execute(
            "CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)"
        )

        result = self.database.execute(
            "INSERT INTO entries(value) VALUES (?) RETURNING id, value",
            ("created",),
        )

        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(result.last_insert_rowid, 1)
        self.assertEqual(
            result.rows.to_dict("records"), [{"id": 1, "value": "created"}]
        )

    def test_execute_rejects_reads_transactions_pragma_attach_and_multiple_statements(
        self,
    ):
        rejected = {
            "SELECT 1": "write_statement_required",
            "EXPLAIN INSERT INTO absent(value) VALUES (1)": "write_statement_required",
            "ANALYZE": "forbidden_sql",
            "REINDEX": "forbidden_sql",
            "BEGIN": "forbidden_sql",
            "PRAGMA user_version = 2": "forbidden_sql",
            "ATTACH DATABASE ':memory:' AS other": "forbidden_sql",
            "CREATE TABLE first_table (value); CREATE TABLE second_table (value)": "multi_statement",
        }
        for sql, code in rejected.items():
            with self.subTest(sql=sql):
                with self.assertRaises(dashboard.DashboardDatabaseError) as raised:
                    self.database.execute(sql)
                self.assertEqual(raised.exception.code, code)

        tables = self.database.query(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE '%_table'"
        )
        self.assertTrue(tables.empty)

    def test_regular_ddl_and_trigger_with_internal_semicolons_are_allowed(self):
        self.database.execute("CREATE TABLE source_rows (value INTEGER)")
        self.database.execute("CREATE TABLE audit_rows (value INTEGER)")
        self.database.execute("CREATE INDEX source_value_index ON source_rows(value)")
        self.database.execute(
            """
            CREATE TRIGGER audit_insert AFTER INSERT ON source_rows BEGIN
                INSERT INTO audit_rows(value) VALUES (NEW.value);
                UPDATE audit_rows SET value = value + 1 WHERE rowid = last_insert_rowid();
            END
            """
        )

        result = self.database.execute(
            "INSERT INTO source_rows(value) VALUES (?)", (4,)
        )

        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(
            self.database.query("SELECT value FROM audit_rows").iloc[0]["value"], 5
        )
        self.database.execute("DROP INDEX source_value_index")

    def test_result_row_and_byte_caps_raise_without_truncation(self):
        self.database.execute("CREATE TABLE bounded (value TEXT)")
        for value in ("a", "b", "c"):
            self.database.execute("INSERT INTO bounded(value) VALUES (?)", (value,))

        with self.assertRaises(dashboard.DashboardDatabaseError) as rows_error:
            self.database.query("SELECT value FROM bounded", max_rows=2)
        self.assertEqual(rows_error.exception.code, "result_too_large")

        with self.assertRaises(dashboard.DashboardDatabaseError) as bytes_error:
            self.database.query("SELECT value FROM bounded", max_bytes=1)
        self.assertEqual(bytes_error.exception.code, "result_too_large")

        with self.assertRaises(dashboard.DashboardDatabaseError) as invalid_limit:
            self.database.query("SELECT value FROM bounded", max_rows=10_001)
        self.assertEqual(invalid_limit.exception.code, "invalid_limit")

    def test_oversized_returning_result_rolls_back(self):
        small_database = dashboard.DashboardDatabase(
            self.database_path,
            logger=self.logger,
            max_rows=2,
        )
        small_database.execute("CREATE TABLE generated (value INTEGER)")

        with self.assertRaises(dashboard.DashboardDatabaseError) as raised:
            small_database.execute(
                """
                WITH RECURSIVE numbers(value) AS (
                    VALUES(1) UNION ALL SELECT value + 1 FROM numbers WHERE value < 3
                )
                INSERT INTO generated(value) SELECT value FROM numbers RETURNING value
                """
            )

        self.assertEqual(raised.exception.code, "result_too_large")
        count = small_database.query("SELECT COUNT(*) AS count FROM generated").iloc[0][
            "count"
        ]
        self.assertEqual(count, 0)

    def test_transaction_commits_and_rolls_back_atomically(self):
        self.database.execute("CREATE TABLE ledger (value INTEGER UNIQUE)")
        with self.database.transaction() as transaction:
            transaction.execute("INSERT INTO ledger(value) VALUES (?)", (1,))
            transaction.execute("INSERT INTO ledger(value) VALUES (?)", (2,))
            frame = transaction.query("SELECT SUM(value) AS total FROM ledger")
            self.assertEqual(frame.iloc[0]["total"], 3)

        with (
            self.assertRaises(RuntimeError),
            self.database.transaction() as transaction,
        ):
            transaction.execute("INSERT INTO ledger(value) VALUES (?)", (3,))
            raise RuntimeError("abort")

        values = self.database.query("SELECT value FROM ledger ORDER BY value")[
            "value"
        ].tolist()
        self.assertEqual(values, [1, 2])

    def test_nested_transaction_is_rejected(self):
        with (
            self.database.transaction(),
            self.assertRaises(dashboard.DashboardDatabaseError) as raised,
            self.database.transaction(),
        ):
            pass
        self.assertEqual(raised.exception.code, "nested_transaction")

    def test_constraint_and_busy_errors_are_typed_sanitized_and_logged_without_params(
        self,
    ):
        self.database.execute("CREATE TABLE unique_rows (value TEXT UNIQUE)")
        secret = "secret-bound-value"
        self.database.execute("INSERT INTO unique_rows(value) VALUES (?)", (secret,))
        with (
            self.assertLogs(self.logger, level="ERROR") as captured,
            self.assertRaises(dashboard.DashboardDatabaseError) as constraint,
        ):
            self.database.execute(
                "INSERT INTO unique_rows(value) VALUES (?)", (secret,)
            )

        error = constraint.exception
        self.assertEqual(error.code, "constraint_violation")
        self.assertFalse(error.retryable)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(dict(error.details)))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertIn("INSERT INTO unique_rows", "\n".join(captured.output))
        self.assertTrue(error.trace_id)

        lock = sqlite3.connect(self.database_path, isolation_level=None)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with (
                patch.object(dashboard, "_BUSY_TIMEOUT_MS", 1),
                self.assertRaises(dashboard.DashboardDatabaseError) as busy,
            ):
                self.database.execute(
                    "INSERT INTO unique_rows(value) VALUES (?)", ("new",)
                )
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(busy.exception.code, "database_busy")
        self.assertTrue(busy.exception.retryable)

        transient_io = dashboard._classify_error(
            sqlite3.OperationalError("disk I/O error"),
            None,
        )
        self.assertEqual(transient_io.code, "database_io")
        self.assertTrue(transient_io.retryable)
        self.assertNotIn(str(self.database_path), transient_io.message)

        full_disk = dashboard._classify_error(
            sqlite3.OperationalError("database or disk is full"),
            None,
        )
        self.assertEqual(full_disk.code, "database_io")
        self.assertTrue(full_disk.retryable)
        self.assertIn("free disk space", full_disk.recovery_hint)

    def test_invalid_parameters_and_multi_statement_values(self):
        with self.assertRaises(dashboard.DashboardDatabaseError) as invalid:
            self.database.query("SELECT ?", "not-a-parameter-sequence")
        self.assertEqual(invalid.exception.code, "invalid_parameters")

        invalid_values = (
            (float("nan"),),
            (float("inf"),),
            (2**63,),
            (object(),),
            {1: "non-text-key"},
        )
        for params in invalid_values:
            with self.subTest(params_type=type(params).__name__):
                with self.assertRaises(
                    dashboard.DashboardDatabaseError
                ) as invalid_value:
                    self.database.query("SELECT 1", params)
                self.assertEqual(invalid_value.exception.code, "invalid_parameters")
                self.assertFalse(invalid_value.exception.retryable)

        tiny_database = dashboard.DashboardDatabase(
            self.database_path,
            logger=self.logger,
            max_bytes=3,
        )
        with self.assertRaises(dashboard.DashboardDatabaseError) as oversized:
            tiny_database.query("SELECT ?", ("four",))
        self.assertEqual(oversized.exception.code, "invalid_parameters")

        value = "one; SELECT 2"
        frame = self.database.query("SELECT ? AS value", (value,))
        self.assertEqual(frame.iloc[0]["value"], value)

    def test_standalone_execute_opens_exactly_one_connection(self):
        with patch.object(
            self.database, "_connect", wraps=self.database._connect
        ) as connect:
            self.database.execute("CREATE TABLE single_connection (value INTEGER)")

        connect.assert_called_once_with(read_only=False)


class DashboardAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "data.db"
        sqlite3.connect(self.database_path).close()
        self.sink = FakeAuditSink()
        self.database = dashboard.DashboardDatabase(
            self.database_path, audit_sink=self.sink
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_execute_audits_correlated_start_and_post_commit_completion(self):
        self.database.execute(
            "CREATE TABLE audited (id INTEGER PRIMARY KEY, value TEXT)"
        )
        result = self.database.execute(
            "INSERT INTO audited(value) VALUES (?) RETURNING id, value", ("one",)
        )

        start, completion = self.sink.events[-2:]
        self.assertEqual(start["event"], "dashboard_mutation_started")
        self.assertEqual(completion["event"], "dashboard_mutation_completed")
        self.assertEqual(start["operationId"], completion["operationId"])
        self.assertEqual(start["payload"]["parameters"], ["one"])
        self.assertIsNone(start["sourceIp"])
        self.assertIsNone(start["widget"])
        self.assertEqual(completion["outcome"], "success")
        self.assertEqual(completion["payload"]["affectedRows"], 1)
        self.assertEqual(completion["payload"]["lastInsertRowId"], 1)
        self.assertEqual(completion["payload"]["rows"], [{"id": 1, "value": "one"}])
        self.assertEqual(result.affected_rows, 1)

    def test_execute_rejections_and_query_write_attempt_are_audited(self):
        for sql in ("SELECT 1", "PRAGMA user_version = 2", "SELECT 1; SELECT 2"):
            with self.subTest(sql=sql):
                before = len(self.sink.events)
                with self.assertRaises(dashboard.DashboardDatabaseError):
                    self.database.execute(sql)
                start, completion = self.sink.events[before:]
                self.assertEqual(start["event"], "dashboard_mutation_started")
                self.assertEqual(completion["outcome"], "rejected")
                self.assertEqual(start["operationId"], completion["operationId"])

        self.database.execute("CREATE TABLE query_guard (value INTEGER)")
        before = len(self.sink.events)
        with self.assertRaises(dashboard.DashboardDatabaseError):
            self.database.query("INSERT INTO query_guard(value) VALUES (1)")
        self.assertEqual(len(self.sink.events), before + 1)
        self.assertEqual(self.sink.events[-1]["event"], "dashboard_mutation_rejected")
        self.assertEqual(self.sink.events[-1]["outcome"], "rejected")

        before = len(self.sink.events)
        self.database.query("SELECT COUNT(*) AS count FROM query_guard")
        self.assertEqual(len(self.sink.events), before)

    def test_trigger_writes_stay_on_the_originating_statement(self):
        self.database.execute("CREATE TABLE source_rows (value INTEGER)")
        self.database.execute("CREATE TABLE audit_rows (value INTEGER)")
        self.database.execute(
            """
            CREATE TRIGGER audit_insert AFTER INSERT ON source_rows BEGIN
                INSERT INTO audit_rows(value) VALUES (NEW.value);
            END
            """
        )
        self.sink.events.clear()

        self.database.execute("INSERT INTO source_rows(value) VALUES (?)", (4,))

        self.assertEqual(len(self.sink.events), 2)
        self.assertTrue(self.sink.events[-1]["payload"]["triggeredWrites"])

    def test_transaction_audits_all_statements_and_commit_in_order(self):
        self.database.execute("CREATE TABLE ledger (value INTEGER)")
        self.sink.events.clear()

        with self.database.transaction() as transaction:
            transaction.execute("INSERT INTO ledger(value) VALUES (?)", (1,))
            transaction.query("SELECT value FROM ledger")

        self.assertEqual(
            [event["event"] for event in self.sink.events],
            [
                "dashboard_transaction_started",
                "dashboard_transaction_statement_started",
                "dashboard_transaction_statement_completed",
                "dashboard_transaction_statement_started",
                "dashboard_transaction_statement_completed",
                "dashboard_transaction_committed",
            ],
        )
        transaction_ids = {event["transactionId"] for event in self.sink.events}
        self.assertEqual(len(transaction_ids), 1)
        self.assertEqual(
            self.sink.events[0]["operationId"],
            self.sink.events[-1]["operationId"],
        )

    def test_transaction_failure_records_statement_and_rollback(self):
        self.database.execute("CREATE TABLE unique_rows (value INTEGER UNIQUE)")
        self.sink.events.clear()

        with (
            self.assertRaises(dashboard.DashboardDatabaseError),
            self.database.transaction() as transaction,
        ):
            transaction.execute("INSERT INTO unique_rows(value) VALUES (1)")
            transaction.execute("INSERT INTO unique_rows(value) VALUES (1)")

        self.assertEqual(self.sink.events[-2]["outcome"], "failed")
        self.assertEqual(
            self.sink.events[-1]["event"], "dashboard_transaction_rolled_back"
        )
        self.assertEqual(self.sink.events[-1]["outcome"], "failed")
        self.assertGreaterEqual(self.sink.events[-1]["durationMs"], 0)
        count = self.database.query("SELECT COUNT(*) AS count FROM unique_rows").iloc[
            0
        ]["count"]
        self.assertEqual(count, 0)

    def test_caller_exception_cancels_and_rolls_back_transaction(self):
        self.database.execute("CREATE TABLE cancelled_rows (value INTEGER)")
        self.sink.events.clear()

        with (
            self.assertRaisesRegex(RuntimeError, "caller stopped"),
            self.database.transaction() as transaction,
        ):
            transaction.execute("INSERT INTO cancelled_rows(value) VALUES (1)")
            raise RuntimeError("caller stopped")

        completion = self.sink.events[-1]
        self.assertEqual(completion["event"], "dashboard_transaction_rolled_back")
        self.assertEqual(completion["outcome"], "cancelled")
        self.assertGreaterEqual(completion["durationMs"], 0)

    def test_start_audit_failure_prevents_sql_execution(self):
        database = dashboard.DashboardDatabase(
            self.database_path, audit_sink=FailingAuditSink()
        )

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            database.execute("CREATE TABLE must_not_exist (value INTEGER)")

        connection = sqlite3.connect(self.database_path)
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'must_not_exist'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(tables, [])

    def test_render_uses_public_streamlit_metadata_and_widget_identity(self):
        module = ModuleType("audited_widget")
        module.render = lambda context: context.db.execute(
            "CREATE TABLE rendered (value INTEGER)"
        )
        widget = dashboard.DashboardWidget(
            widget_id="identity",
            title="Identity",
            order=1,
            path=Path("identity.py"),
            module_name="audited_widget",
            module=module,
        )
        context = dashboard.DashboardContext(
            db=self.database,
            workspace=self.database_path.parent,
            logger=logging.getLogger("dashboard-audit-render"),
            widget_id="",
            runtime={},
        )
        streamlit = SimpleNamespace(
            session_state={},
            context=SimpleNamespace(
                ip_address="192.168.1.42",
                headers={"User-Agent": "dashboard-test-client"},
            ),
        )

        with patch.dict(sys.modules, {"streamlit": streamlit}):
            dashboard.render_widget(widget, context)

        start, completion = self.sink.events
        for event in (start, completion):
            self.assertEqual(event["widget"], "identity")
            self.assertEqual(event["sourceIp"], "192.168.1.42")
            self.assertEqual(event["userAgent"], "dashboard-test-client")
            self.assertTrue(event["dashboardSessionId"])
            self.assertTrue(event["requestId"])
        self.assertEqual(start["dashboardSessionId"], completion["dashboardSessionId"])


class WidgetDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "dashboard"
        self.widgets = self.root / "widgets"
        self.widgets.mkdir(parents=True)
        (self.root / "app.py").write_text(
            "from lite_mcp_dashboard import render_widgets\nrender_widgets()\n",
            encoding="utf-8",
        )
        self.database_path = self.root.parent / "data.db"
        sqlite3.connect(self.database_path).close()
        dashboard._configure(
            database_path=self.database_path,
            dashboard_dir=self.root,
            workspace=self.root.parent / "workspace",
            logger=logging.getLogger("widget-test"),
            runtime={"tools": ["execute_sql"], "nested": {"ready": True}},
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_widget(self, name: str, source: str) -> Path:
        path = self.widgets / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_configuration_exposes_managed_artifact_store(self):
        context = dashboard.get_context()

        self.assertIsNotNone(context.artifacts)
        self.assertEqual(
            context.artifacts.root,
            (self.root.parent / "workspace" / "artifacts").resolve(),
        )

    def test_discovery_contract_order_and_ignore_rules(self):
        self.write_widget(
            "later.py",
            "TITLE = 'Later'\nORDER = 20\ndef render(context):\n    pass\n",
        )
        self.write_widget("first.py", "ORDER = 10\ndef render(context):\n    pass\n")
        self.write_widget("_draft.py", "raise RuntimeError('must not import')\n")
        self.write_widget(".hidden.py", "raise RuntimeError('must not import')\n")
        nested = self.widgets / "nested"
        nested.mkdir()
        (nested / "ignored.py").write_text("raise RuntimeError('must not import')\n")
        symlink = self.widgets / "linked.py"
        try:
            symlink.symlink_to(self.widgets / "_draft.py")
        except OSError:
            pass

        widgets = dashboard.preflight_dashboard()

        self.assertEqual([widget.widget_id for widget in widgets], ["first", "later"])
        self.assertEqual(widgets[0].title, "First")
        self.assertEqual(widgets[1].title, "Later")
        self.assertEqual(
            widgets[0].module_name, dashboard.discover_widgets()[0].module_name
        )

    def test_invalid_widget_contracts_fail_preflight(self):
        invalid_sources = {
            "missing.py": "value = 1\n",
            "async_render.py": "async def render(context):\n    pass\n",
            "two_args.py": "def render(context, extra):\n    pass\n",
            "bad_title.py": "TITLE = ''\ndef render(context):\n    pass\n",
            "bad_order.py": "ORDER = True\ndef render(context):\n    pass\n",
        }
        for filename, source in invalid_sources.items():
            with self.subTest(filename=filename):
                for child in self.widgets.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                self.write_widget(filename, source)
                with self.assertRaises(RuntimeError):
                    dashboard.preflight_dashboard()

    def test_public_widget_stems_need_not_be_python_identifiers(self):
        for order, filename in enumerate(("foo-bar.py", "123.py", "with.py")):
            self.write_widget(
                filename,
                f"ORDER = {order}\ndef render(context):\n    pass\n",
            )

        widgets = dashboard.preflight_dashboard()

        self.assertEqual(
            [widget.widget_id for widget in widgets],
            ["foo-bar", "123", "with"],
        )
        self.assertTrue(
            all(
                widget.module_name.rsplit(".", 1)[-1].isidentifier()
                for widget in widgets
            )
        )

    def test_widget_runtime_error_has_filename_and_phase_context(self):
        self.write_widget("broken.py", "raise RuntimeError('boom')\n")

        with self.assertRaisesRegex(
            RuntimeError,
            r"broken\.py failed import/contract validation: RuntimeError: boom",
        ):
            dashboard.preflight_dashboard()

    def test_symlinked_dashboard_and_widgets_directories_are_rejected(self):
        linked_dashboard = self.root.parent / "linked-dashboard"
        linked_dashboard.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "dashboard directory"):
            dashboard.preflight_dashboard(linked_dashboard)

        real_widgets = self.root.parent / "real-widgets"
        real_widgets.mkdir()
        linked_widgets = self.root.parent / "linked-widgets"
        linked_widgets.symlink_to(real_widgets, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "widgets directory"):
            dashboard.discover_widgets(linked_widgets)

    def test_duplicate_explicit_order_fails(self):
        self.write_widget("one.py", "ORDER = 1\ndef render(context):\n    pass\n")
        self.write_widget("two.py", "ORDER = 1\ndef render(context):\n    pass\n")

        with self.assertRaisesRegex(RuntimeError, "ORDER values must be unique"):
            dashboard.discover_widgets()

    def test_source_changes_reload_with_stable_module_name(self):
        path = self.write_widget(
            "changing.py",
            "VALUE = 'before'\ndef render(context):\n    context.logger.info(VALUE)\n",
        )
        first = dashboard.discover_widgets()[0]
        path.write_text(
            "VALUE = 'after!'\ndef render(context):\n    context.logger.info(VALUE)\n",
            encoding="utf-8",
        )

        second = dashboard.discover_widgets()[0]

        self.assertEqual(first.module_name, second.module_name)
        self.assertEqual(second.module.VALUE, "after!")

    def test_render_passes_immutable_widget_context_and_propagates_errors(self):
        self.write_widget(
            "capture.py",
            """
def render(context):
    assert context.widget_id == 'capture'
    assert context.runtime['nested']['ready'] is True
    context.runtime['changed'] = True
""",
        )
        widget = dashboard.discover_widgets()[0]

        with self.assertRaises(TypeError):
            dashboard.render_widget(widget)

        self.write_widget(
            "capture.py", "def render(context):\n    raise ValueError('fatal')\n"
        )
        widget = dashboard.discover_widgets()[0]
        with self.assertRaisesRegex(ValueError, "fatal"):
            dashboard.render_widget(widget)

    def test_components_are_not_auto_executed(self):
        components = self.root / "components"
        components.mkdir()
        (components / "unused.py").write_text("raise RuntimeError('must not import')\n")
        self.write_widget("valid.py", "def render(context):\n    pass\n")

        widgets = dashboard.preflight_dashboard()

        self.assertEqual([widget.widget_id for widget in widgets], ["valid"])


class DefaultWidgetContractTests(unittest.TestCase):
    def test_example_widget_handles_expected_database_conditions(self):
        path = REPOSITORY_ROOT / "dashboard" / "widgets" / "example_card.py"
        spec = importlib.util.spec_from_file_location("default_example_card", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(
            {
                "constraint_violation",
                "database_busy",
                "database_io",
                "result_too_large",
            }.issubset(module._EXPECTED_DATABASE_ERRORS)
        )
        self.assertNotIn("CREATE TABLE", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
