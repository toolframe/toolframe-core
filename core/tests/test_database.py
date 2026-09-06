import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import toolframe_db as database


class DatabaseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "data.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE app_items "
                "(id INTEGER PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE _toolframe_runtime_state (value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE app_sequence "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO app_sequence(value) VALUES ('seed')")
            connection.commit()
        self.adapter = database._configure(
            self.database_path,
            logger=logging.getLogger(f"database-adapter-test.{id(self)}"),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_configure_publishes_one_process_adapter(self):
        self.assertIs(database.get_database(), self.adapter)
        self.assertEqual(self.adapter.path, self.database_path.resolve())

    def test_query_and_execute_use_bound_parameters(self):
        hostile = "ready'); DROP TABLE app_items; --"
        result = self.adapter.execute(
            "INSERT INTO app_items(label, status) VALUES (?, ?)",
            (hostile, "pending"),
        )
        frame = self.adapter.query(
            "SELECT id, label, status FROM app_items WHERE label = ?", (hostile,)
        )

        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(result.last_insert_rowid, 1)
        self.assertEqual(
            frame.to_dict("records"),
            [{"id": 1, "label": hostile, "status": "pending"}],
        )

    def test_schema_changes_are_rejected_without_partial_effects(self):
        statements = (
            "CREATE TABLE app_forbidden (value INTEGER)",
            "CREATE INDEX app_items_status_idx ON app_items(status)",
            "ALTER TABLE app_items ADD COLUMN note TEXT",
            "DROP TABLE app_items",
        )
        for sql in statements:
            with self.subTest(sql=sql):
                with self.assertRaises(database.DatabaseError) as raised:
                    self.adapter.execute(sql)
                self.assertEqual(raised.exception.code, "schema_change_forbidden")
                self.assertFalse(raised.exception.retryable)
                self.assertIn("migration", raised.exception.recovery_hint.casefold())

        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = connection.execute("PRAGMA table_info(app_items)").fetchall()
        self.assertEqual([column[1] for column in columns], ["id", "label", "status"])

    def test_runtime_owned_tables_cannot_be_mutated(self):
        with self.assertRaises(database.DatabaseError) as raised:
            self.adapter.execute(
                "INSERT INTO _toolframe_runtime_state(value) VALUES (?)", ("blocked",)
            )

        self.assertEqual(raised.exception.code, "protected_table_forbidden")
        self.assertFalse(raised.exception.retryable)
        with self.assertRaises(database.DatabaseError) as read:
            self.adapter.query("SELECT COUNT(*) AS count FROM _toolframe_runtime_state")
        self.assertEqual(read.exception.code, "protected_table_forbidden")
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM _toolframe_runtime_state"
            ).fetchone()[0]
            self.assertEqual(count, 0)

        with self.assertRaises(database.DatabaseError) as sqlite_owned:
            self.adapter.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (999, "app_sequence"),
            )
        self.assertEqual(sqlite_owned.exception.code, "protected_table_forbidden")

    def test_query_is_bounded_and_execute_is_dml_only(self):
        with self.assertRaises(database.DatabaseError) as pragma:
            self.adapter.query("PRAGMA table_info(app_items)")
        self.assertEqual(pragma.exception.code, "read_only_required")

        with self.assertRaises(database.DatabaseError) as read:
            self.adapter.execute("SELECT 1")
        self.assertEqual(read.exception.code, "write_statement_required")

    def test_unconfigured_access_has_a_startup_order_diagnostic(self):
        configured = database._configured_database
        try:
            database._configured_database = None
            with self.assertRaisesRegex(RuntimeError, "before importing tools"):
                database.get_database()
        finally:
            database._configured_database = configured


if __name__ == "__main__":
    unittest.main()
