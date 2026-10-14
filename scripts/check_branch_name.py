"""Validate a working branch using the repository's cchk.toml policy."""

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", nargs="?", help="actual source branch in CI")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    policy = tomllib.loads((root / "cchk.toml").read_text())["branch"]
    branch = args.branch
    if branch is None:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            print(
                "Pass the actual source branch when HEAD is detached.", file=sys.stderr
            )
            return 1
        branch = result.stdout.strip()

    if branch in policy["allow_branch_names"]:
        return 0
    prefix, separator, description = branch.partition("/")
    pattern = (
        r"[a-z0-9]+(?:[.-][a-z0-9]+)*"
        if prefix == "release"
        else r"[a-z0-9]+(?:-[a-z0-9]+)*"
    )
    if (
        separator
        and prefix in policy["allow_branch_types"]
        and re.fullmatch(pattern, description)
    ):
        return 0

    allowed = ", ".join(policy["allow_branch_types"])
    print(
        f"Invalid branch {branch!r}. Use <type>/<description> with type: {allowed}. "
        "Use lowercase letters and digits separated by single hyphens; "
        "release versions may also contain dots.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
