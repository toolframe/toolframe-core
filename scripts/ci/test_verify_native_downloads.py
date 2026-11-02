"""Offline fixture regressions for public native release verification."""

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "verify_native_downloads",
    Path(__file__).with_name("verify-native-downloads.py"),
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

REPOSITORY = "toolframe/toolframe-core"
COMMIT = "a" * 40
TAG = "v1.2.3"
TARGET = "aarch64-unknown-linux-gnu"
BINARY = b"fixture native executable"


class PublicNativeDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "candidate"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.candidate = {
            "schema": 1,
            "identity": {"version": "1.2.3"},
            "producer": {
                "repository": REPOSITORY,
                "ref": f"refs/tags/{TAG}",
                "commit": COMMIT,
                "run_id": 42,
                "event": "workflow_dispatch",
            },
            "files": {},
        }
        self.manifest = {
            "version": "1.2.3",
            "target": TARGET,
            "binary_sha256": hashlib.sha256(BINARY).hexdigest(),
            "companions": [],
        }
        self.group = self.bundle / f"launcher-release-{TARGET}"
        self.group.mkdir(parents=True)
        self.archive = self.group / f"toolframe-1.2.3-{TARGET}.tar.gz"
        self.make_archive()
        self.manifest_path = self.group / f"manifest-{TARGET}.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        for target in verify.TARGETS:
            group = self.bundle / f"launcher-release-{target}"
            group.mkdir(exist_ok=True)
            sums = group / f"SHA256SUMS-{target}.txt"
            sums.write_text(
                f"{verify.digest(self.archive)}  {self.archive.name}\n"
                if target == TARGET
                else ""
            )
        self.seal()

    def make_archive(self, mode="regular"):
        with tarfile.open(self.archive, "w:gz") as bundle:
            entries = [("toolframe", BINARY), ("LICENSE", b"L"), ("NOTICE", b"N")]
            if mode == "duplicate":
                entries.append(("toolframe", BINARY))
            if mode == "traversal":
                entries[-1] = ("../NOTICE", b"N")
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                if mode == "symlink" and name == "toolframe":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                bundle.addfile(member, io.BytesIO(data) if member.isfile() else None)

    def seal(self):
        self.candidate["files"] = {
            path.relative_to(self.bundle).as_posix(): {
                "sha256": verify.digest(path),
                "size": path.stat().st_size,
            }
            for path in self.bundle.rglob("*")
            if path.is_file() and path.name != "candidate.json"
        }
        (self.bundle / "candidate.json").write_text(json.dumps(self.candidate))

    def contract(self, **kwargs):
        arguments = {
            "bundle": self.bundle,
            "repository": REPOSITORY,
            "tag": TAG,
            "commit": COMMIT,
            "run_id": 42,
            "target": TARGET,
        }
        arguments.update(kwargs)
        return verify.contract(**arguments)

    def public_files(self):
        manifest, records = self.contract()
        for source in self.group.iterdir():
            (self.downloads / source.name).write_bytes(source.read_bytes())
        (self.downloads / "SHA256SUMS").write_bytes(
            b"".join(
                (
                    self.bundle
                    / f"launcher-release-{target}"
                    / f"SHA256SUMS-{target}.txt"
                ).read_bytes()
                for target in sorted(verify.TARGETS)
            )
        )
        return manifest, records

    def test_accepts_exact_bytes_and_pins_every_attestation_identity(self):
        manifest, records = self.public_files()
        calls = []

        def run(command, **kwargs):
            self.assertTrue(kwargs["check"])
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="[]")

        results = verify.verify_assets(
            self.downloads, records, REPOSITORY, TAG, COMMIT, run=run
        )
        self.assertEqual(set(results), set(records))
        self.assertEqual(len(calls), len(records))
        for command in calls:
            for flag, value in (
                ("--repo", REPOSITORY),
                ("--signer-workflow", f"{REPOSITORY}/{verify.WORKFLOW}"),
                ("--source-ref", f"refs/tags/{TAG}"),
                ("--source-digest", COMMIT),
                ("--predicate-type", "https://slsa.dev/provenance/v1"),
            ):
                self.assertEqual(command[command.index(flag) + 1], value)
            self.assertIn("--deny-self-hosted-runners", command)
        executable = verify.stage_binary(
            self.downloads / self.archive.name, manifest, self.root / "bin"
        )
        self.assertEqual(executable.read_bytes(), BINARY)
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)

    def test_rejects_wrong_publication_identity(self):
        for change in (
            {"repository": "other/repository"},
            {"tag": "v9.9.9"},
            {"commit": "b" * 40},
            {"run_id": 43},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.contract(**change)

    def test_rejects_wrong_manifest_platform_or_version(self):
        for field, value in (("target", "x86_64-apple-darwin"), ("version", "9.9.9")):
            with self.subTest(field=field):
                changed = {**self.manifest, field: value}
                self.manifest_path.write_text(json.dumps(changed))
                self.seal()
                with self.assertRaisesRegex(ValueError, "manifest"):
                    self.contract()

    def test_rejects_tampered_comparison_bundle(self):
        self.manifest_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "sealed hash"):
            self.contract()

    def test_rejects_changed_public_archive_manifest_or_checksums(self):
        _, records = self.public_files()
        for filename in records:
            with self.subTest(filename=filename):
                path = self.downloads / filename
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    verify.verify_assets(
                        self.downloads,
                        records,
                        REPOSITORY,
                        TAG,
                        COMMIT,
                        run=lambda *args, **kwargs: subprocess.CompletedProcess(
                            args, 0, stdout="[]"
                        ),
                    )
                path.write_bytes(original)

    def test_rejects_missing_or_extra_public_assets(self):
        _, records = self.public_files()
        extra = self.downloads / "unexpected"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "asset set"):
            verify.verify_assets(self.downloads, records, REPOSITORY, TAG, COMMIT)
        extra.unlink()
        (self.downloads / self.archive.name).unlink()
        with self.assertRaisesRegex(ValueError, "asset set"):
            verify.verify_assets(self.downloads, records, REPOSITORY, TAG, COMMIT)

    def test_attestation_failure_is_fatal(self):
        _, records = self.public_files()

        def rejected(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, stderr="untrusted")

        with self.assertRaises(subprocess.CalledProcessError):
            verify.verify_assets(
                self.downloads, records, REPOSITORY, TAG, COMMIT, run=rejected
            )
        self.assertFalse((self.root / "bin").exists())

    def test_rejects_unsafe_archive_members_before_staging(self):
        for mode in ("duplicate", "traversal", "symlink"):
            with self.subTest(mode=mode):
                self.make_archive(mode)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    verify.stage_binary(self.archive, self.manifest, self.root / "bin")
                self.assertFalse((self.root / "bin").exists())

    def test_rejects_wrong_executable_hash_before_staging(self):
        manifest = {**self.manifest, "binary_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "Executable checksum"):
            verify.stage_binary(self.archive, manifest, self.root / "bin")
        self.assertFalse((self.root / "bin").exists())

    def test_rejects_wrong_runner_platform(self):
        with (
            patch.object(verify.platform, "system", return_value="Darwin"),
            patch.object(verify.platform, "machine", return_value="arm64"),
        ):
            with self.assertRaisesRegex(ValueError, "Runner platform"):
                verify.check_platform(TARGET)
            self.assertEqual(
                verify.check_platform("aarch64-apple-darwin"), ("Darwin", "arm64")
            )


if __name__ == "__main__":
    unittest.main()
