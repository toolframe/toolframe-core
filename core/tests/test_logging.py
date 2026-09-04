import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_logging
from lite_mcp_logging import (
    StructuredLogError,
    StructuredLogs,
    StructuredLogSettings,
    _lock_handle,
    _unlock_handle,
)


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


class StructuredLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.logs_dir = self.root / "logs"
        self.now = datetime(2026, 9, 2, 0, 0, 1, tzinfo=UTC)
        self.clock = MutableClock(self.now)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def settings(self, **overrides):
        values = {"logs_dir": self.logs_dir}
        values.update(overrides)
        return StructuredLogSettings(**values)

    def records(self, name):
        path = self.logs_dir / name
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_emits_fixed_schema_to_independent_daily_files(self):
        with StructuredLogs(
            self.settings(), clock=self.clock, instance_id="instance-test"
        ) as logs:
            audit = logs.emit("audit", "runtime_started", outcome="success")
            mcp = logs.emit(
                "mcp",
                "tools_call_completed",
                payload={"ok": True, "message": "寫入成功"},
                method="tools/call",
            )

        self.assertEqual(audit["schemaVersion"], 1)
        self.assertEqual(audit["instanceId"], "instance-test")
        self.assertEqual(audit["timestamp"], "2026-09-02T00:00:01.000000Z")
        self.assertIsNone(audit["sessionId"])
        self.assertIsNone(audit["error"])
        self.assertIsNone(audit["userAgent"])
        self.assertEqual((audit["sequence"], mcp["sequence"]), (1, 2))
        self.assertEqual(
            self.records("audit-2026-09-02.jsonl")[0]["event"],
            "runtime_started",
        )
        self.assertEqual(
            self.records("mcp-2026-09-02.jsonl")[0]["method"], "tools/call"
        )

    def test_same_day_restart_appends_and_gets_new_instance(self):
        with StructuredLogs(self.settings(), clock=self.clock) as first:
            first_id = first.instance_id
            first.emit("audit", "first")
        with StructuredLogs(self.settings(), clock=self.clock) as second:
            second_id = second.instance_id
            second.emit("audit", "second")

        records = self.records("audit-2026-09-02.jsonl")
        self.assertEqual([record["event"] for record in records], ["first", "second"])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual([record["sequence"] for record in records], [1, 1])

    def test_payload_middle_truncation_and_binary_normalization(self):
        with StructuredLogs(
            self.settings(payload_limit_bytes=128), clock=self.clock
        ) as logs:
            raw = "start-" + ("middle" * 100) + "-finish"
            record = logs.emit("mcp", "payload", payload={"raw": raw})
            binary = logs.emit("mcp", "binary", payload=b"\x00\xff")
            memory = logs.emit("mcp", "memory", payload=memoryview(b"ok"))

        payload = record["payload"]
        encoded = json.dumps(
            {"raw": raw}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["originalBytes"], len(encoded))
        self.assertEqual(payload["sha256"], hashlib.sha256(encoded).hexdigest())
        self.assertIn("start", payload["preview"])
        self.assertIn("finish", payload["preview"])
        self.assertEqual(binary["payload"], {"encoding": "base64", "data": "AP8="})
        self.assertEqual(memory["payload"], {"encoding": "base64", "data": "b2s="})

    def test_truncated_binary_hashes_original_bytes(self):
        raw = bytes(range(256))
        with StructuredLogs(
            self.settings(payload_limit_bytes=64), clock=self.clock
        ) as logs:
            record = logs.emit("mcp", "binary", payload=raw)

        payload = record["payload"]
        self.assertEqual(payload["encoding"], "base64")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["originalBytes"], len(raw))
        self.assertEqual(payload["sha256"], hashlib.sha256(raw).hexdigest())

    def test_traceback_has_independent_limit(self):
        with StructuredLogs(
            self.settings(payload_limit_bytes=32, traceback_limit_bytes=64),
            clock=self.clock,
        ) as logs:
            record = logs.emit(
                "audit", "failed", error={"message": "bad", "traceback": "x" * 200}
            )

        trace = record["error"]["traceback"]
        self.assertTrue(trace["truncated"])
        self.assertEqual(trace["originalBytes"], 200)

    def test_concurrent_writes_are_complete_and_globally_ordered(self):
        with StructuredLogs(self.settings(), clock=self.clock) as logs:
            threads = []
            for index in range(80):
                channel = "audit" if index % 2 else "mcp"
                thread = threading.Thread(
                    target=logs.emit,
                    args=(channel, "concurrent"),
                    kwargs={"payload": {"index": index}},
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()

        combined = self.records("audit-2026-09-02.jsonl") + self.records(
            "mcp-2026-09-02.jsonl"
        )
        self.assertEqual(len(combined), 80)
        self.assertEqual(
            sorted(record["sequence"] for record in combined), list(range(1, 81))
        )

    def test_events_roll_over_using_each_record_utc_timestamp(self):
        with StructuredLogs(self.settings(), clock=self.clock) as logs:
            logs.emit("mcp", "before")
            self.clock.value += timedelta(days=1)
            logs.emit("mcp", "after")

        self.assertEqual(self.records("mcp-2026-09-02.jsonl")[0]["event"], "before")
        self.assertEqual(self.records("mcp-2026-09-03.jsonl")[0]["event"], "after")

    def test_retention_removes_old_date_pair_but_preserves_unknown_files(self):
        self.logs_dir.mkdir()
        old = (self.now.date() - timedelta(days=14)).isoformat()
        recent = (self.now.date() - timedelta(days=13)).isoformat()
        for channel in ("audit", "mcp"):
            (self.logs_dir / f"{channel}-{old}.jsonl").write_text("{}\n")
            (self.logs_dir / f"{channel}-{recent}.jsonl").write_text("{}\n")
        unknown = self.logs_dir / "notes.jsonl"
        unknown.write_text("keep me", encoding="utf-8")

        with StructuredLogs(self.settings(), clock=self.clock):
            pass

        self.assertFalse((self.logs_dir / f"audit-{old}.jsonl").exists())
        self.assertFalse((self.logs_dir / f"mcp-{old}.jsonl").exists())
        self.assertTrue((self.logs_dir / f"audit-{recent}.jsonl").exists())
        self.assertTrue((self.logs_dir / f"mcp-{recent}.jsonl").exists())
        self.assertEqual(unknown.read_text(), "keep me")
        events = self.records("audit-2026-09-02.jsonl")
        self.assertEqual(events[-1]["event"], "log_retention_deleted")

    def test_size_cap_deletes_oldest_date_bucket_as_a_pair(self):
        self.logs_dir.mkdir()
        old = (self.now.date() - timedelta(days=2)).isoformat()
        newer = (self.now.date() - timedelta(days=1)).isoformat()
        # Together these exceed 1 MiB; the oldest pair must be removed first.
        for date_text in (old, newer):
            for channel in ("audit", "mcp"):
                (self.logs_dir / f"{channel}-{date_text}.jsonl").write_bytes(
                    (b"x" * 300_000) + b"\n"
                )

        with StructuredLogs(self.settings(max_total_mib=1), clock=self.clock) as logs:
            logs.emit("mcp", "fits_after_retention")

        self.assertFalse((self.logs_dir / f"audit-{old}.jsonl").exists())
        self.assertFalse((self.logs_dir / f"mcp-{old}.jsonl").exists())
        self.assertTrue((self.logs_dir / f"audit-{newer}.jsonl").exists())
        self.assertTrue((self.logs_dir / f"mcp-{newer}.jsonl").exists())

    def test_current_day_cap_fails_before_append(self):
        self.logs_dir.mkdir()
        path = self.logs_dir / "mcp-2026-09-02.jsonl"
        path.write_bytes((b"x" * (1024 * 1024 - 100)) + b"\n")
        with StructuredLogs(self.settings(max_total_mib=1), clock=self.clock) as logs:
            before = path.stat().st_size
            with self.assertRaisesRegex(StructuredLogError, "size cap"):
                logs.emit("mcp", "too_large", payload="y" * 1000)
            self.assertEqual(path.stat().st_size, before)

    def test_partial_tail_is_truncated_and_recovery_is_audited(self):
        self.logs_dir.mkdir()
        path = self.logs_dir / "mcp-2026-09-01.jsonl"
        path.write_bytes(b'{"complete":true}\n{"partial":')

        with StructuredLogs(self.settings(), clock=self.clock):
            pass

        self.assertEqual(path.read_bytes(), b'{"complete":true}\n')
        record = self.records("audit-2026-09-02.jsonl")[0]
        self.assertEqual(record["event"], "log_tail_recovered")
        self.assertEqual(record["payload"]["path"], path.name)
        self.assertGreater(record["payload"]["removedBytes"], 0)

    def test_process_lock_is_exclusive_and_released_on_close(self):
        first = StructuredLogs(self.settings(), clock=self.clock)
        try:
            with self.assertRaisesRegex(StructuredLogError, "already in use"):
                StructuredLogs(self.settings(), clock=self.clock)
        finally:
            first.close()
        second = StructuredLogs(self.settings(), clock=self.clock)
        second.close()

    def test_windows_lock_uses_a_real_marker_byte_and_matching_range(self):
        locking = Mock()
        platform_locking = SimpleNamespace(
            LK_NBLCK=2,
            LK_UNLCK=0,
            locking=locking,
        )
        lock_path = self.root / "windows.lock"
        with (
            lock_path.open("a+b") as handle,
            patch.object(lite_mcp_logging.os, "name", "nt"),
            patch.object(lite_mcp_logging, "_platform_locking", platform_locking),
        ):
            _lock_handle(handle)
            self.assertEqual(lock_path.read_bytes(), b"\0")
            self.assertEqual(handle.tell(), 0)
            locking.assert_called_once_with(handle.fileno(), 2, 1)

            _unlock_handle(handle)
            self.assertEqual(handle.tell(), 0)
            self.assertEqual(locking.call_args_list[-1].args, (handle.fileno(), 0, 1))

    def test_rejects_managed_symlinks_and_directories(self):
        for invalid_kind in ("symlink", "directory"):
            with self.subTest(invalid_kind=invalid_kind):
                case_dir = self.root / invalid_kind
                case_dir.mkdir()
                invalid = case_dir / "audit-2026-09-01.jsonl"
                if invalid_kind == "symlink":
                    target = case_dir / "target"
                    target.write_text("{}\n")
                    invalid.symlink_to(target)
                else:
                    invalid.mkdir()
                settings = StructuredLogSettings(logs_dir=case_dir)
                with self.assertRaisesRegex(StructuredLogError, "direct regular file"):
                    StructuredLogs(settings, clock=self.clock)

    def test_serialization_and_closed_writer_fail_with_dedicated_error(self):
        logs = StructuredLogs(self.settings(), clock=self.clock)
        with self.assertRaises(StructuredLogError):
            logs.emit("audit", "bad", payload=object())
        with self.assertRaisesRegex(
            StructuredLogError, "unknown structured log fields"
        ):
            logs.emit("audit", "unknown", notInSchema=True)
        logs.close()
        with self.assertRaisesRegex(StructuredLogError, "closed"):
            logs.emit("audit", "late")

    def test_append_failure_uses_dedicated_error(self):
        logs = StructuredLogs(self.settings(), clock=self.clock)
        logs.emit("audit", "opens_writer")
        handle = logs._handles["audit"][1]
        handle.close()
        with self.assertRaisesRegex(StructuredLogError, "could not append"):
            logs.emit("audit", "write_fails")
        # The caller would terminate after a writer failure. Clear the injected
        # broken test handle so cleanup can release the process lock normally.
        logs._handles.clear()
        logs.close()

    def test_naive_clock_and_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            self.settings(retention_days=0)
        with self.assertRaises(ValueError):
            self.settings(max_total_mib=0)
        with self.assertRaisesRegex(StructuredLogError, "naive"):
            StructuredLogs(
                self.settings(),
                clock=lambda: datetime(2026, 9, 2, 0, 0, 0),  # noqa: DTZ001
            )


if __name__ == "__main__":
    unittest.main()
