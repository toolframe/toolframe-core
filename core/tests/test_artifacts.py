import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_artifacts as artifacts


class RecordingAuditSink:
    def __init__(self):
        self.events = []

    def emit(self, channel, event, **fields):
        self.events.append({"channel": channel, "event": event, **fields})


class FailingAuditSink:
    def emit(self, *_args, **_kwargs):
        raise RuntimeError("audit unavailable")


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name) / "workspace"
        self.workspace.mkdir()
        self.sink = RecordingAuditSink()
        self.store = artifacts.ArtifactStore(
            self.workspace, max_artifact_bytes=32, audit_sink=self.sink
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_write_read_lookup_list_and_persisted_metadata(self):
        artifact = self.store.write_text(
            "reports/summary.csv", "name,total\none,1\n", mime_type="text/csv"
        )

        self.assertEqual(self.store.read("reports/summary.csv"), b"name,total\none,1\n")
        self.assertEqual(
            self.store.read_by_id(artifact.artifact_id), b"name,total\none,1\n"
        )
        self.assertEqual(self.store.get("reports/summary.csv"), artifact)
        self.assertEqual(self.store.get_by_id(artifact.artifact_id), artifact)
        self.assertEqual(self.store.list(prefix="reports"), (artifact,))
        self.assertEqual(len(artifact.artifact_id), 32)
        self.assertEqual(artifact.byte_size, 17)
        self.assertEqual(
            artifact.sha256,
            "961586074a558e239634d7557fa40f8af284284035eb1fa7b33a3c0a35d24a94",
        )
        reference = artifact.to_reference()
        self.assertEqual(reference["path"], "reports/summary.csv")
        self.assertEqual(reference["mimeType"], "text/csv")
        self.assertTrue(reference["createdAt"].endswith("Z"))

        metadata_path = (
            self.workspace / "artifacts" / ".lite-mcp" / f"{artifact.artifact_id}.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["schemaVersion"], 1)
        self.assertEqual(metadata["artifactId"], artifact.artifact_id)
        self.assertEqual(
            [event["event"] for event in self.sink.events],
            ["artifact_write_started", "artifact_write_completed"],
        )
        self.assertEqual(
            self.sink.events[0]["operationId"], self.sink.events[1]["operationId"]
        )

    def test_streaming_write_guesses_mime_and_enforces_create_once(self):
        artifact = self.store.write("image.png", io.BytesIO(b"png"))
        self.assertEqual(artifact.mime_type, "image/png")

        with self.assertRaises(artifacts.ArtifactExistsError) as captured:
            self.store.write("image.png", b"replacement")

        self.assertEqual(captured.exception.code, "artifact_exists")
        self.assertEqual(self.store.read("image.png"), b"png")

        with self.assertRaises(artifacts.ArtifactMetadataError):
            self.store.write("bad.txt", b"bad", mime_type="text/plain; charset=utf-8")
        with self.assertRaises(artifacts.ArtifactMetadataError):
            self.store.write("bad-data.txt", "not bytes")

    def test_unmanaged_collision_is_not_overwritten(self):
        occupied = self.workspace / "artifacts" / "manual.txt"
        occupied.write_bytes(b"user-owned")

        with self.assertRaises(artifacts.ArtifactExistsError):
            self.store.write("manual.txt", b"managed")

        self.assertEqual(occupied.read_bytes(), b"user-owned")

    def test_concurrent_create_has_one_deterministic_winner(self):
        def create(payload):
            try:
                return self.store.write("race.bin", payload)
            except artifacts.ArtifactExistsError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (b"first", b"second")))

        winners = [item for item in results if isinstance(item, artifacts.Artifact)]
        collisions = [
            item for item in results if isinstance(item, artifacts.ArtifactExistsError)
        ]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(collisions), 1)
        self.assertIn(self.store.read("race.bin"), {b"first", b"second"})

    def test_rejects_unsafe_and_ambiguous_paths(self):
        for path in (
            "",
            "/absolute.txt",
            "../escape.txt",
            "a/../escape.txt",
            "a//b.txt",
            "a/./b.txt",
            "a\\b.txt",
            ".lite-mcp/index.json",
            ".lite-mcp-temporary",
        ):
            with self.subTest(path=path), self.assertRaises(artifacts.ArtifactError):
                self.store.write(path, b"content")

        self.assertEqual(self.sink.events[-1]["event"], "artifact_write_rejected")
        self.assertEqual(
            self.sink.events[-1]["payload"]["code"], "invalid_artifact_path"
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlinked_path_components_and_files(self):
        outside = self.workspace.parent / "outside"
        outside.mkdir()
        (self.workspace / "artifacts" / "linked").symlink_to(
            outside, target_is_directory=True
        )
        (self.workspace / "artifacts" / "file-link").symlink_to(outside / "file")

        with self.assertRaises(artifacts.ArtifactPathError):
            self.store.write("linked/escape.txt", b"content")
        with self.assertRaises(artifacts.ArtifactPathError):
            self.store.write("file-link", b"content")
        self.assertFalse((outside / "escape.txt").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlinked_artifact_root(self):
        other_workspace = self.workspace.parent / "other-workspace"
        other_workspace.mkdir()
        (other_workspace / "artifacts-target").mkdir()
        (other_workspace / "artifacts").symlink_to(
            other_workspace / "artifacts-target", target_is_directory=True
        )

        with self.assertRaises(artifacts.ArtifactStorageError):
            artifacts.ArtifactStore(other_workspace)

    def test_size_limit_removes_temporary_files(self):
        with self.assertRaises(artifacts.ArtifactLimitError) as captured:
            self.store.write("large.bin", io.BytesIO(b"x" * 33))

        self.assertEqual(captured.exception.details["limitBytes"], 32)
        self.assertFalse((self.workspace / "artifacts" / "large.bin").exists())
        self.assertEqual(
            list((self.workspace / "artifacts").glob(".lite-mcp-*.tmp")), []
        )

    def test_content_tampering_is_detected(self):
        artifact = self.store.write("data.bin", b"original")
        (self.workspace / "artifacts" / "data.bin").write_bytes(b"modified")

        with self.assertRaises(artifacts.ArtifactIntegrityError) as captured:
            self.store.read_by_id(artifact.artifact_id)

        self.assertEqual(captured.exception.code, "artifact_integrity_error")

    def test_metadata_lookup_detects_size_mismatch(self):
        self.store.write("data.bin", b"original")
        (self.workspace / "artifacts" / "data.bin").write_bytes(b"short")

        with self.assertRaises(artifacts.ArtifactIntegrityError):
            self.store.get("data.bin")

    def test_non_regular_managed_content_is_detected_before_read(self):
        artifact = self.store.write("data.bin", b"original")
        content_path = self.workspace / "artifacts" / "data.bin"
        content_path.unlink()
        content_path.mkdir()

        with self.assertRaises(artifacts.ArtifactIntegrityError):
            self.store.read_by_id(artifact.artifact_id)

    def test_malformed_or_symlinked_metadata_fails_closed(self):
        metadata_root = self.workspace / "artifacts" / ".lite-mcp"
        (metadata_root / f"{'a' * 32}.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(artifacts.ArtifactIntegrityError):
            self.store.list()

    def test_oversized_metadata_fails_closed_without_unbounded_read(self):
        metadata_root = self.workspace / "artifacts" / ".lite-mcp"
        (metadata_root / f"{'b' * 32}.json").write_bytes(
            b"{" + b" " * artifacts.MAX_METADATA_BYTES + b"}"
        )

        with self.assertRaises(artifacts.ArtifactIntegrityError):
            self.store.list()

    def test_metadata_publish_failure_rolls_back_content(self):
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("metadata publish failed")
            return real_link(source, destination, **kwargs)

        with (
            patch("lite_mcp_artifacts.os.link", side_effect=fail_second_link),
            self.assertRaises(artifacts.ArtifactStorageError),
        ):
            self.store.write("rollback.bin", b"content")

        self.assertFalse((self.workspace / "artifacts" / "rollback.bin").exists())
        self.assertEqual(self.store.list(), ())

    def test_audit_start_failure_prevents_mutation(self):
        store = artifacts.ArtifactStore(
            self.workspace, max_artifact_bytes=32, audit_sink=FailingAuditSink()
        )

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            store.write("blocked.bin", b"content")

        self.assertFalse((self.workspace / "artifacts" / "blocked.bin").exists())

    def test_listing_is_sorted_prefixed_and_bounded(self):
        second = self.store.write("b/two.txt", b"two")
        first = self.store.write("a/one.txt", b"one")
        nested = self.store.write("a/nested/three.txt", b"three")

        self.assertEqual(self.store.list(limit=3), (nested, first, second))
        self.assertEqual(self.store.list(prefix="a"), (nested, first))
        self.assertEqual(self.store.list(prefix="b"), (second,))
        with self.assertRaises(artifacts.ArtifactLimitError):
            self.store.list(limit=2)
        with self.assertRaises(artifacts.ArtifactLimitError):
            self.store.list(limit=0)


class ArtifactConfigurationTests(unittest.TestCase):
    def test_process_global_store_supports_tool_import_seam(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            configured = artifacts.configure_artifact_store(workspace)

            self.assertIs(artifacts.get_artifact_store(), configured)


if __name__ == "__main__":
    unittest.main()
