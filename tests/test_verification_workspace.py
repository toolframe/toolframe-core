"""Disposable check environments preserve evidence and never remove active work."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "verification_workspace_contract", REPOSITORY / "scripts/verification_workspace.py"
)
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class VerificationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        scratch = REPOSITORY / "build/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = self.root / ".local/result.json"
        self.patch_root = patch.object(workspace, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.patch_idle = patch.object(workspace, "idle", return_value=True)
        self.idle = self.patch_idle.start()
        self.addCleanup(self.patch_idle.stop)
        self.keep = patch.dict(os.environ, {"TOOLFRAME_KEEP_TEST_WORKSPACE": "0"})
        self.keep.start()
        self.addCleanup(self.keep.stop)
        self.created = []

    def check(self, *, fail=False, report=True):
        @workspace.managed_verification
        def run():
            path = workspace.create_workspace("check-", evidence=self.evidence)
            self.created.append(path)
            (path / "command.log").write_text("diagnostic evidence")
            (path / "large-environment").write_text("disposable")
            if fail:
                raise RuntimeError("check failed")
            if report:
                workspace.write(self.evidence, {"passed": True})

        return run()

    def summaries(self):
        return list(
            (self.evidence.parent / (self.evidence.name + ".logs")).glob(
                "*/workspace.json"
            )
        )

    def test_success_removes_environment_after_saving_report_and_logs(self):
        self.check()
        self.assertFalse(self.created[-1].exists())
        self.assertTrue(json.loads(self.evidence.read_text())["passed"])
        summary = json.loads(self.summaries()[0].read_text())
        self.assertFalse(summary["retained"])
        self.assertEqual(Path(summary["logs"][0]).read_text(), "diagnostic evidence")

    def test_failed_check_keeps_latest_workspace_and_removes_previous_failure(self):
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "check failed"):
                self.check(fail=True)
        self.assertFalse(self.created[0].exists())
        self.assertTrue(self.created[1].exists())
        self.assertEqual(len(self.summaries()), 2)
        self.assertTrue(
            all(json.loads(file.read_text())["error"] for file in self.summaries())
        )

    def test_failed_workspace_expires_without_removing_its_evidence(self):
        with self.assertRaises(RuntimeError):
            self.check(fail=True)
        path = self.created[-1]
        data = workspace.owned(path)
        data["finished"] = time.time() - 8 * 86400
        workspace.write(path / workspace.MARKER, data)
        workspace.prune("check-")
        self.assertFalse(path.exists())
        self.assertTrue(Path(data["evidence_saved"]).is_file())
        self.assertFalse(
            json.loads(Path(data["evidence_saved"]).read_text())["retained"]
        )

    def test_modified_evidence_destination_cannot_overwrite_user_data(self):
        with self.assertRaises(RuntimeError):
            self.check(fail=True)
        path = self.created[-1]
        outside = self.root / "application-data.json"
        outside.write_text("user data")
        data = workspace.owned(path)
        data.update(evidence_saved=str(outside), finished=0)
        workspace.write(path / workspace.MARKER, data)
        workspace.prune("check-")
        self.assertTrue(path.exists())
        self.assertEqual(outside.read_text(), "user data")

    def test_abandoned_run_saves_diagnostics_before_expiry(self):
        with self.assertRaises(RuntimeError):
            self.check(fail=True)
        path = self.created[-1]
        data = workspace.owned(path)
        data.update(status="running", started=time.time() - 8 * 86400)
        data.pop("evidence_saved")
        data.pop("finished")
        workspace.write(path / workspace.MARKER, data)
        with patch.object(workspace, "alive", return_value=False):
            workspace.prune("check-")
        self.assertFalse(path.exists())
        summary = json.loads(self.summaries()[0].read_text())
        self.assertIn("exited before", summary["error"])
        self.assertFalse(summary["retained"])
        self.assertEqual(Path(summary["logs"][0]).read_text(), "diagnostic evidence")

    def test_abandoned_parent_does_not_allow_deleting_a_live_child_environment(self):
        with self.assertRaises(RuntimeError):
            self.check(fail=True)
        path = self.created[-1]
        data = workspace.owned(path)
        data.update(status="running", started=time.time() - 8 * 86400)
        data.pop("evidence_saved")
        data.pop("finished")
        workspace.write(path / workspace.MARKER, data)
        self.idle.return_value = False
        with patch.object(workspace, "alive", return_value=False):
            workspace.prune("check-")
        self.assertTrue(path.exists())

    def test_debug_retention_wins_over_success_and_expiry(self):
        with patch.dict(os.environ, {"TOOLFRAME_KEEP_TEST_WORKSPACE": "1"}):
            self.check()
        path = self.created[-1]
        data = workspace.owned(path)
        data["finished"] = 0
        workspace.write(path / workspace.MARKER, data)
        workspace.prune("check-")
        self.assertTrue(path.exists())

    def test_active_or_uninspectable_environment_survives_success_and_pruning(self):
        self.idle.return_value = False
        self.check()
        path = self.created[-1]
        workspace.prune("check-")
        self.assertTrue(path.exists())
        self.idle.return_value = True
        workspace.prune("check-")
        self.assertFalse(path.exists())

    def test_missing_or_unchanged_report_does_not_allow_success_cleanup(self):
        workspace.write(self.evidence, {"old": True})
        os.utime(self.evidence, (0, 0))
        self.check(report=False)
        self.assertTrue(self.created[-1].exists())
        self.assertEqual(json.loads(self.evidence.read_text()), {"old": True})

    def test_unknown_and_symlinked_directories_are_preserved(self):
        parent = self.root / "build/verification"
        parent.mkdir(parents=True)
        app = parent / "user-application"
        app.mkdir()
        (app / "data.db").write_text("keep")
        (parent / "linked-app").symlink_to(app, target_is_directory=True)
        workspace.prune("check-")
        self.assertEqual((app / "data.db").read_text(), "keep")
        self.assertTrue((parent / "linked-app").is_symlink())

    def test_application_data_cannot_replace_the_owned_directory(self):
        @workspace.managed_verification
        def run():
            path = workspace.create_workspace("check-", evidence=self.evidence)
            self.created.append(path)
            path.rename(path.with_name("original-run"))
            path.mkdir()
            (path / "data.db").write_text("application data")
            workspace.write(self.evidence, {"passed": True})

        run()
        self.assertEqual((self.created[-1] / "data.db").read_text(), "application data")

    def test_symlinked_build_parent_is_rejected(self):
        external = self.root / "application"
        external.mkdir()
        (self.root / "build").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlinked"):
            self.check()
        self.assertEqual(list(external.iterdir()), [])

    def test_signal_interruption_keeps_diagnostics(self):
        @workspace.managed_verification
        def interrupted():
            path = workspace.create_workspace("check-", evidence=self.evidence)
            self.created.append(path)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            interrupted()
        self.assertTrue(self.created[-1].exists())
        self.assertIn(
            "KeyboardInterrupt", json.loads(self.summaries()[0].read_text())["error"]
        )

    def test_live_pid_and_explicit_output_outside_workspace_are_protected(self):
        self.check(fail=False)
        outside = self.root / "application/data.db"
        outside.parent.mkdir()
        outside.write_text("keep")
        with self.assertRaises(RuntimeError):
            self.check(fail=True)
        path = self.created[-1]
        data = workspace.owned(path)
        data.update(status="running", pid=os.getpid(), finished=0)
        workspace.write(path / workspace.MARKER, data)
        workspace.prune("check-")
        self.assertTrue(path.exists())
        self.assertEqual(outside.read_text(), "keep")

    def test_unknown_process_state_preserves_the_workspace(self):
        self.patch_idle.stop()
        with patch.object(
            workspace.subprocess, "run", side_effect=FileNotFoundError("lsof")
        ):
            self.assertFalse(workspace.idle(self.root))

    @unittest.skipUnless(shutil.which("lsof"), "live protection requires lsof")
    def test_process_using_a_workspace_is_detected(self):
        self.patch_idle.stop()
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
            ],
            cwd=self.root,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertFalse(workspace.idle(self.root))
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
