import importlib.util
import inspect
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CORE_DIRECTORY = _REPOSITORY_ROOT / "core"
if str(_CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_CORE_DIRECTORY))

import lite_mcp_db as _database_sdk

_TOOL_PATH = _REPOSITORY_ROOT / "tools" / "execute_sql.py"
_SPEC = importlib.util.spec_from_file_location("execute_sql_reference", _TOOL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load {_TOOL_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class ExecuteSqlTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "test.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE app_records "
                "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, value REAL NOT NULL)"
            )
            connection.execute("CREATE TABLE _lite_mcp_private (value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE app_sequence "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO app_sequence(value) VALUES ('seed')")
            connection.commit()
        _database_sdk._configure(self.database_path)

    def tearDown(self):
        self._temporary_directory.cleanup()

    def _call(self, sql, params=None, mode="read"):
        return _MODULE.execute_sql(sql, params, mode)

    def _assert_error(self, result, code, retryable=False):
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.structured_content)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], code)
        self.assertEqual(error["retryable"], retryable)
        self.assertEqual(result.meta["retryable"], retryable)
        self.assertEqual(error["traceId"], result.meta["traceId"])
        self.assertIn("recoveryHint", error["details"])
        self.assertTrue(result.content[0].text)

    def test_pep723_metadata_and_adapter_only_database_access(self):
        source = _TOOL_PATH.read_text(encoding="utf-8")
        metadata_lines = []
        in_metadata = False
        for line in source.splitlines():
            if line == "# /// script":
                in_metadata = True
                continue
            if in_metadata and line == "# ///":
                break
            if in_metadata:
                metadata_lines.append(line.removeprefix("# "))

        import tomllib

        metadata = tomllib.loads("\n".join(metadata_lines))
        self.assertEqual(metadata["requires-python"], ">=3.14,<3.15")
        self.assertEqual(metadata["dependencies"], ["fastmcp==4.0.0"])
        self.assertNotIn("import sqlite3", source)
        self.assertNotIn("LITE_MCP_DB", source)
        self.assertIn("from lite_mcp_db import", source)

        published_functions = [
            name
            for name, value in vars(_MODULE).items()
            if not name.startswith("_") and inspect.isfunction(value)
        ]
        self.assertEqual(published_functions, ["execute_sql"])
        signature = inspect.signature(_MODULE.execute_sql)
        self.assertEqual(list(signature.parameters), ["sql", "params", "mode"])
        self.assertIs(signature.parameters["sql"].default, inspect.Parameter.empty)
        self.assertIsNone(signature.parameters["params"].default)
        self.assertEqual(signature.parameters["mode"].default, "read")

    def test_read_mode_uses_bound_parameters_and_returns_rows(self):
        value = "Ada'); DROP TABLE app_records; --"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO app_records(name, value) VALUES (?, ?)", (value, 3.5)
            )
            connection.commit()

        result = self._call(
            "SELECT id, name, value FROM app_records WHERE name = ?", [value]
        )

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "kind": "query",
                "columns": ["id", "name", "value"],
                "rows": [[1, value, 3.5]],
            },
        )

    def test_read_mode_supports_named_parameters(self):
        result = self._call("SELECT :value AS value", {"value": "ready"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["rows"], [["ready"]])

    def test_write_mode_persists_dml_and_reports_rowid(self):
        result = self._call(
            "INSERT INTO app_records(name, value) VALUES (?, ?)",
            ["created", 8.0],
            "write",
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["kind"], "mutation")
        self.assertEqual(result.structured_content["affected_rows"], 1)
        self.assertIsInstance(result.structured_content["last_insert_rowid"], int)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT name, value FROM app_records").fetchone(),
                ("created", 8.0),
            )

    def test_write_mode_returns_bounded_returning_rows(self):
        result = self._call(
            "INSERT INTO app_records(name, value) VALUES (?, ?) RETURNING id, name",
            ["created", 8.0],
            "write",
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["rows"], [[1, "created"]])
        self.assertEqual(result.structured_content["columns"], ["id", "name"])

    def test_read_mode_rejects_mutation_without_applying_it(self):
        result = self._call(
            "INSERT INTO app_records(name, value) VALUES (?, ?)", ["blocked", 1]
        )

        self._assert_error(result, "read_only_required")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM app_records").fetchone()[0],
                0,
            )

    def test_write_mode_rejects_reads(self):
        self._assert_error(
            self._call("SELECT 1", mode="write"), "write_statement_required"
        )

    def test_write_mode_rejects_every_schema_change(self):
        statements = (
            "CREATE TABLE forbidden_table (value INTEGER)",
            "CREATE INDEX forbidden_index ON app_records(name)",
            "ALTER TABLE app_records ADD COLUMN note TEXT",
            "DROP TABLE app_records",
        )
        for sql in statements:
            with self.subTest(sql=sql):
                self._assert_error(
                    self._call(sql, mode="write"), "schema_change_forbidden"
                )

        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = connection.execute("PRAGMA table_info(app_records)").fetchall()
            self.assertEqual([column[1] for column in columns], ["id", "name", "value"])

    def test_write_mode_rejects_runtime_owned_table_mutation(self):
        self._assert_error(
            self._call(
                "INSERT INTO _lite_mcp_private(value) VALUES (?)",
                ["blocked"],
                "write",
            ),
            "protected_table_forbidden",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM _lite_mcp_private").fetchone()[
                    0
                ],
                0,
            )

        self._assert_error(
            self._call(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                [999, "app_sequence"],
                "write",
            ),
            "protected_table_forbidden",
        )

    def test_pragma_attach_and_maintenance_are_forbidden(self):
        statements = (
            "PRAGMA user_version = 2",
            "ATTACH DATABASE ':memory:' AS other",
            "ANALYZE",
            "REINDEX",
        )
        for sql in statements:
            with self.subTest(sql=sql):
                self._assert_error(self._call(sql, mode="write"), "forbidden_sql")

    def test_query_result_limit_is_an_error_instead_of_silent_truncation(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executemany(
                "INSERT INTO app_records(name, value) VALUES (?, ?)",
                ((f"row-{index}", index) for index in range(101)),
            )
            connection.commit()

        self._assert_error(
            self._call("SELECT id FROM app_records ORDER BY id"), "result_too_large"
        )

    def test_oversized_returning_result_rolls_back(self):
        result = self._call(
            """
            WITH RECURSIVE numbers(value) AS (
                VALUES(1) UNION ALL SELECT value + 1 FROM numbers WHERE value < 101
            )
            INSERT INTO app_records(name, value)
            SELECT 'generated-' || value, value FROM numbers
            RETURNING id
            """,
            mode="write",
        )

        self._assert_error(result, "result_too_large")
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM app_records").fetchone()[0]
        self.assertEqual(count, 0)

    def test_blob_null_and_nonfinite_conversion(self):
        result = self._call(
            "SELECT X'00FF' AS payload, NULL AS missing, 1e999 AS infinite"
        )

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content["rows"],
            [["<blob: 2 bytes>", None, None]],
        )

    def test_validation_errors_are_structured_and_nonretryable(self):
        cases = (
            (" \n\t", None, "read", "empty_sql"),
            ("SELECT 1; SELECT 2", None, "read", "multi_statement"),
            ("SELEKT 1", None, "read", "syntax_error"),
            ("SELECT ?", {"wrong": 1}, "read", "invalid_parameters"),
        )
        for sql, params, mode, code in cases:
            with self.subTest(sql=sql):
                self._assert_error(self._call(sql, params, mode), code)

        self._assert_error(self._call("SELECT 1", mode="admin"), "invalid_mode")

    def test_locked_database_is_retryable(self):
        lock = sqlite3.connect(self.database_path)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            result = self._call("UPDATE app_records SET value = ?", [1], "write")
        finally:
            lock.rollback()
            lock.close()

        self._assert_error(result, "database_busy", retryable=True)


if __name__ == "__main__":
    unittest.main()
