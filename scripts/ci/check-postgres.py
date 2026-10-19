"""Require and run real PostgreSQL Core integration, without silent skips."""

import importlib.util
import os
import subprocess
import sys


def main():
    if not os.environ.get("TOOLFRAME_CORE_TEST_ADMIN_DSN"):
        raise SystemExit(
            "Set TOOLFRAME_CORE_TEST_ADMIN_DSN to an isolated PostgreSQL server"
        )
    if importlib.util.find_spec("psycopg") is None:
        raise SystemExit("Install the Core postgres extra before running this check")
    if importlib.util.find_spec("toolframe_sql_postgres") is None:
        raise SystemExit(
            "Install the pinned PostgreSQL storage provider before running this check"
        )
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "tests/test_core_state.py",
            "tests/test_process.py",
            "tests/test_storage_bindings.py",
            "tests/test_mixed_storage.py",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
