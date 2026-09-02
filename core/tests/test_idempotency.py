import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_idempotency as idempotency


class RecordingAuditSink:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, channel, event, *, level="INFO", **fields):
        record = {"channel": channel, "event": event, "level": level, **fields}
        with self.lock:
            self.events.append(record)
        return record


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "data.db"
        migration = (
            REPOSITORY_ROOT / "migrations" / "0001_idempotency.sql"
        ).read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(migration)
            connection.execute(
                "CREATE TABLE business_events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.commit()
        self.audit = RecordingAuditSink()
        self.store = idempotency.IdempotencyStore(
            self.database_path, audit_sink=self.audit
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_success_is_committed_once_and_replayed_from_stored_json(self):
        calls = 0

        def mutation(transaction):
            nonlocal calls
            calls += 1
            cursor = transaction.execute(
                "INSERT INTO business_events(value) VALUES (?)", ("created",)
            )
            return {"event_id": cursor.lastrowid, "status": "created"}

        first = self.store.run_once(
            "events.create", "request-123", {"value": "created"}, mutation
        )
        replay = self.store.run_once(
            "events.create", "request-123", {"value": "created"}, mutation
        )

        self.assertEqual(calls, 1)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.value, replay.value)
        self.assertEqual(first.original_operation_id, replay.original_operation_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM business_events").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM _lite_mcp_idempotency"
                ).fetchone()[0],
                1,
            )

    def test_same_key_with_different_input_is_nonretryable_mismatch(self):
        self.store.run_once(
            "events.create",
            "request-123",
            {"value": "first"},
            lambda transaction: {"ok": True},
        )

        with self.assertRaises(idempotency.IdempotencyError) as raised:
            self.store.run_once(
                "events.create",
                "request-123",
                {"value": "different"},
                lambda transaction: self.fail("mismatched callback must not run"),
            )

        self.assertEqual(raised.exception.code, "idempotency_key_mismatch")
        self.assertFalse(raised.exception.retryable)

    def test_callback_failure_rolls_back_business_write_and_does_not_claim_key(self):
        def failing(transaction):
            transaction.execute(
                "INSERT INTO business_events(value) VALUES (?)", ("rolled-back",)
            )
            raise RuntimeError("business failure")

        with self.assertRaisesRegex(RuntimeError, "business failure"):
            self.store.run_once(
                "events.create", "retryable-business-key", {"value": 1}, failing
            )

        succeeded = self.store.run_once(
            "events.create",
            "retryable-business-key",
            {"value": 1},
            lambda transaction: {
                "event_id": transaction.execute(
                    "INSERT INTO business_events(value) VALUES (?)", ("committed",)
                ).lastrowid
            },
        )

        self.assertFalse(succeeded.replayed)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM business_events ORDER BY id"
                ).fetchall(),
                [("committed",)],
            )

    def test_oversized_result_rolls_back_business_write_and_receipt(self):
        limited = idempotency.IdempotencyStore(
            self.database_path, result_limit_bytes=16
        )

        def mutation(transaction):
            transaction.execute(
                "INSERT INTO business_events(value) VALUES (?)", ("too-large",)
            )
            return {"value": "x" * 32}

        with self.assertRaises(idempotency.IdempotencyError) as raised:
            limited.run_once("events.create", "large", {}, mutation)

        self.assertEqual(raised.exception.code, "idempotency_result_too_large")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM business_events").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM _lite_mcp_idempotency"
                ).fetchone()[0],
                0,
            )

    def test_concurrent_same_key_executes_callback_once_and_waiter_replays(self):
        start = threading.Barrier(2)
        callback_calls = 0
        callback_lock = threading.Lock()

        def invoke():
            start.wait(timeout=2)

            def mutation(transaction):
                nonlocal callback_calls
                with callback_lock:
                    callback_calls += 1
                transaction.execute(
                    "INSERT INTO business_events(value) VALUES (?)", ("once",)
                )
                time.sleep(0.1)
                return {"status": "done"}

            return self.store.run_once(
                "events.create", "concurrent", {"value": "same"}, mutation
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: invoke(), range(2)))

        self.assertEqual(callback_calls, 1)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].value, results[1].value)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM business_events").fetchone()[0],
                1,
            )

    def test_audit_events_keep_outer_operation_correlation(self):
        with idempotency._audit_context(
            operationId="mcp-operation", protocolRequestId=42, tool="create_event"
        ):
            self.store.run_once(
                "events.create", "audited", {}, lambda transaction: {"ok": True}
            )

        events = self.audit.events[-2:]
        self.assertEqual(
            [event["event"] for event in events],
            ["idempotency_started", "idempotency_completed"],
        )
        for event in events:
            self.assertEqual(event["operationId"], "mcp-operation")
            self.assertEqual(event["protocolRequestId"], 42)
            self.assertEqual(event["tool"], "create_event")
            self.assertEqual(events[0]["transactionId"], events[1]["transactionId"])


if __name__ == "__main__":
    unittest.main()
