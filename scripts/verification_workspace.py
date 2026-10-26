"""Repository-local scratch lifecycle for checks that also run outside tfdev.

Only directories created by this module are disposable. Reports and distributions
live outside them; explicit retention and live processes always prevent removal.
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = ".verification-workspace.json"
RETENTION_SECONDS = 7 * 86400
_session = contextvars.ContextVar("verification_workspaces", default=None)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def safe_path(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Symlinked verification path: {component}")
    return path


def identity(path):
    stat = path.lstat()
    return [stat.st_dev, stat.st_ino]


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def idle(path):
    """Unknown process state is a retention reason, never permission to delete."""
    try:
        processes = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        files = subprocess.run(
            ["lsof", "-nP", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    if processes.returncode or files.returncode or files.stderr.strip():
        return False
    value = str(path)
    return value not in processes.stdout and not any(
        name == "n" + value or name.startswith("n" + value + os.sep)
        for name in files.stdout.splitlines()
    )


def owned(path):
    safe_path(path / MARKER)
    data = json.loads((path / MARKER).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("evidence"), str):
        raise TypeError(f"Invalid workspace record: {path}")
    evidence = safe_path(Path(data["evidence"]))
    expected = (
        evidence.parent / (evidence.name + ".logs") / path.name / "workspace.json"
    )
    if (
        data.get("schema") != 1
        or data.get("owner") != str(ROOT)
        or type(data.get("pid")) is not int
        or data["pid"] <= 0
        or data.get("path") != str(path)
        or data.get("identity") != identity(path)
        or path.parent != ROOT / "build/verification"
        or not evidence.is_absolute()
        or evidence == path
        or path in evidence.parents
        or not isinstance(data.get("prefix"), str)
        or type(data.get("started")) not in (int, float)
        or not math.isfinite(data["started"])
        or data.get("status") not in {"running", "passed", "failed"}
        or (
            data.get("evidence_saved")
            and (
                type(data.get("finished")) not in (int, float)
                or not math.isfinite(data["finished"])
                or data["evidence_saved"] != str(expected)
            )
        )
    ):
        raise ValueError(f"Unrecognized verification workspace: {path}")
    return data


def prune(prefix):
    """Keep the latest failed workspace for at most seven days, unless pinned."""
    parent = safe_path(ROOT / "build/verification")
    if not parent.exists():
        return
    previous = []
    for path in parent.iterdir():
        try:
            data = owned(path)
            if data.get("prefix") != prefix or data.get("keep"):
                continue
            if data.get("status") == "running":
                if alive(data.get("pid")):
                    continue
                finish(
                    path,
                    data,
                    False,
                    "Verifier exited before recording completion",
                    prune_after=False,
                    finished_at=data["started"],
                )
                data = owned(path)
            if not data.get("evidence_saved"):
                continue
            previous.append((path, data))
        except OSError, ValueError, TypeError, AttributeError:
            continue
    previous.sort(key=lambda item: item[1]["finished"], reverse=True)
    failed_kept = False
    for path, data in previous:
        expired = time.time() - data["finished"] > RETENTION_SECONDS
        if data["status"] == "failed" and not failed_kept and not expired:
            failed_kept = True
            continue
        if not idle(path):
            continue
        try:
            current = owned(path)
            sidecar = safe_path(Path(data["evidence_saved"]))
            if current != data or not sidecar.is_file():
                continue
            shutil.rmtree(path)
            data.update(
                retained=False,
                cleaned_at=time.time(),
                cleanup_reason="expired" if expired else "superseded or completed",
            )
            write(sidecar, data)
        except OSError, ValueError, TypeError:
            continue


def create_workspace(prefix, *, evidence=None):
    session = _session.get()
    if session is None:
        raise RuntimeError("Use create_workspace inside @managed_verification")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,59}", prefix):
        raise ValueError("Use a short lowercase verification prefix")
    parent = safe_path(ROOT / "build/verification")
    parent.mkdir(parents=True, exist_ok=True)
    prune(prefix)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    evidence = (
        Path(os.path.abspath(evidence))
        if evidence is not None
        else ROOT / ".local/verification" / path.name / "result.json"
    )
    if evidence == path or path in evidence.parents:
        raise ValueError(
            "Verification evidence must be outside the disposable workspace"
        )
    safe_path(evidence)
    data = {
        "schema": 1,
        "owner": str(ROOT),
        "path": str(path),
        "identity": identity(path),
        "prefix": prefix,
        "pid": os.getpid(),
        "started": time.time(),
        "status": "running",
        "keep": os.environ.get("TOOLFRAME_KEEP_TEST_WORKSPACE") == "1",
        "evidence": str(evidence),
    }
    write(path / MARKER, data)
    session.append((path, data))
    print(f"Verification workspace: {path}", flush=True)
    return path


def finish(path, data, succeeded, error, *, prune_after=True, finished_at=None):
    if owned(path) != data:
        raise ValueError("Verification workspace changed before cleanup")
    evidence = Path(data["evidence"])
    # Existing reports are historical inputs until this invocation writes them.
    saved = evidence.is_file() and evidence.stat().st_mtime >= data["started"]
    diagnostics = safe_path(evidence.parent / (evidence.name + ".logs") / path.name)
    diagnostics.mkdir(parents=True, exist_ok=True)
    logs = []
    for source in path.glob("*.log"):
        if source.is_file() and not source.is_symlink():
            target = diagnostics / source.name
            shutil.copy2(source, target)
            target.chmod(0o600)
            logs.append(str(target))
    summary = {
        **data,
        "finished": time.time() if finished_at is None else finished_at,
        "status": "passed" if succeeded and saved else "failed",
        "error": error,
        "report_written": saved,
        "logs": logs,
        "retained": True,
    }
    sidecar = diagnostics / "workspace.json"
    summary["evidence_saved"] = str(sidecar)
    write(sidecar, summary)
    write(path / MARKER, summary)
    if summary["status"] == "passed" and not data["keep"] and idle(path):
        if owned(path) != summary:
            raise ValueError("Verification workspace changed during cleanup")
        shutil.rmtree(path)
        summary["retained"] = False
        write(sidecar, summary)
    print(
        f"Verification workspace {'retained' if summary['retained'] else 'removed'}: "
        f"{path}; lifecycle evidence: {sidecar}",
        flush=True,
    )
    if prune_after:
        prune(data["prefix"])


def managed_verification(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        session = []
        token = _session.set(session)
        succeeded, error = False, None
        try:
            result = function(*args, **kwargs)
            succeeded = (
                result is None
                or (type(result) is int and result == 0)
                or (
                    isinstance(result, dict)
                    and result.get("status") == "passed"
                    and not result.get("cleanup_errors")
                )
            )
            return result
        except BaseException as problem:
            error = f"{type(problem).__name__}: {problem}"
            raise
        finally:
            for path, data in reversed(session):
                try:
                    finish(path, data, succeeded, error)
                except (OSError, ValueError, TypeError) as problem:
                    print(
                        f"Workspace retained after cleanup error: {path}: {problem}",
                        file=sys.stderr,
                    )
            _session.reset(token)

    return wrapped


@managed_verification
def run_command(args):
    path = create_workspace(args.prefix)
    data = owned(path)
    evidence = Path(data["evidence"])
    artifacts = ROOT / "build/ci" / path.name
    artifacts.mkdir(parents=True, exist_ok=False)
    environment = {
        **os.environ,
        "TOOLFRAME_TEST_WORKSPACE": str(path),
        "TOOLFRAME_TEST_ARTIFACTS": str(artifacts),
    }
    with (path / "command.log").open("w") as log:
        process = subprocess.Popen(
            args.command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            for line in process.stdout:
                log.write(line)
                print(line, end="", flush=True)
            code = process.wait()
        except BaseException:
            # The whole child group belongs to this invocation, not a shared app.
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            raise
        finally:
            process.stdout.close()
    write(
        evidence,
        {
            "status": "passed" if code == 0 else "failed",
            "returncode": code,
            "command": args.command,
            "artifacts": str(artifacts),
        },
    )
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command.pop(0)
    if not args.command:
        parser.error("Supply a validation command after --")
    return run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
