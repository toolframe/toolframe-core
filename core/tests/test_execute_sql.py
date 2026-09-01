import importlib.util
import inspect
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
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
        self._previous_database_path = os.environ.get("LITE_MCP_DB")
        os.environ["LITE_MCP_DB"] = str(self.database_path)

    def tearDown(self):
        if self._previous_database_path is None:
            os.environ.pop("LITE_MCP_DB", None)
        else:
            os.environ["LITE_MCP_DB"] = self._previous_database_path
        self._temporary_directory.cleanup()

    def _call(self, sql):
        return _MODULE.execute_sql(sql)

    def _assert_error(self, result, code, retryable=False):
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.structured_content)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], code)
        self.assertEqual(error["retryable"], retryable)
        self.assertEqual(result.meta["retryable"], retryable)
        self.assertEqual(error["traceId"], result.meta["traceId"])
        self.assertTrue(result.content[0].text)

    def test_pep723_metadata_and_single_published_tool(self):
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

        published_functions = [
            name
            for name, value in vars(_MODULE).items()
            if not name.startswith("_") and inspect.isfunction(value)
        ]
        self.assertEqual(published_functions, ["execute_sql"])
        self.assertEqual(list(inspect.signature(_MODULE.execute_sql).parameters), ["sql"])
        self.assertIs(
            inspect.signature(_MODULE.execute_sql).parameters["sql"].default,
            inspect.Parameter.empty,
        )

    def test_query_result(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, note TEXT)"
            )
            connection.execute(
                "INSERT INTO people (name, note) VALUES (?, ?)", ("Ada", "founder")
            )
            connection.commit()

        result = self._call("SELECT id, name, note FROM people")

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "kind": "query",
                "columns": ["id", "name", "note"],
                "rows": [[1, "Ada", "founder"]],
                "truncated": False,
            },
        )

    def test_mutation_persists_and_reports_rowid(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT)")
            connection.commit()

        result = self._call("INSERT INTO events (value) VALUES ('created')")

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["kind"], "mutation")
        self.assertEqual(result.structured_content["affected_rows"], 1)
        self.assertIsInstance(result.structured_content["last_insert_rowid"], int)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM events").fetchone(), ("created",))

    def test_query_is_truncated_after_one_hundred_rows(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE numbers (value INTEGER)")
            connection.executemany("INSERT INTO numbers (value) VALUES (?)", ((i,) for i in range(101)))
            connection.commit()

        result = self._call("SELECT value FROM numbers ORDER BY value")

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.structured_content["rows"]), 100)
        self.assertEqual(result.structured_content["rows"][0], [0])
        self.assertEqual(result.structured_content["rows"][-1], [99])
        self.assertTrue(result.structured_content["truncated"])

    def test_large_returning_result_is_truncated_and_committed(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE generated (value INTEGER)")
            connection.commit()

        result = self._call(
            "WITH RECURSIVE numbers(value) AS ("
            "VALUES(1) UNION ALL SELECT value + 1 FROM numbers WHERE value < 150"
            ") INSERT INTO generated(value) SELECT value FROM numbers RETURNING value"
        )

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.structured_content["rows"]), 100)
        self.assertTrue(result.structured_content["truncated"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM generated").fetchone()[0]
        self.assertEqual(count, 150)

    def test_blob_null_and_nonfinite_conversion(self):
        result = self._call("SELECT X'00FF' AS payload, NULL AS missing, 1e999 AS infinite")

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content["rows"],
            [["<blob: 2 bytes>", None, None]],
        )

    def test_empty_sql_is_nonretryable_error(self):
        self._assert_error(self._call(" \n\t"), "empty_sql")

    def test_comment_and_separator_only_sql_is_empty(self):
        for sql in (";", "-- only a comment", "/* block comment */ ; -- tail"):
            with self.subTest(sql=sql):
                self._assert_error(self._call(sql), "empty_sql")

    def test_sql_byte_limit_is_utf8_and_nonretryable(self):
        self._assert_error(self._call("'é'" * 600_000), "sql_too_long")

    def test_multi_statement_is_nonretryable_error(self):
        self._assert_error(self._call("SELECT 1; SELECT 2"), "multi_statement")

    def test_invalid_syntax_is_nonretryable_error(self):
        self._assert_error(self._call("SELEKT 1"), "syntax_error")

    def test_missing_database_environment_is_nonretryable_error(self):
        os.environ.pop("LITE_MCP_DB")

        self._assert_error(self._call("SELECT 1"), "missing_database_path")

    def test_locked_database_is_retryable_and_diagnostics_are_sanitized(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE locked_rows (value INTEGER)")
            connection.commit()

        lock = sqlite3.connect(self.database_path)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            with self.assertLogs(_MODULE._LOGGER, level="ERROR") as logs:
                result = self._call("UPDATE locked_rows SET value = 1")
        finally:
            lock.rollback()
            lock.close()

        self._assert_error(result, "database_busy", retryable=True)
        self.assertNotIn("locked_rows", "\n".join(logs.output))
        self.assertNotIn(str(self.database_path), "\n".join(logs.output))
        self.assertEqual(result.structured_content["error"]["details"]["exception_type"], "OperationalError")


if __name__ == "__main__":
    unittest.main()
