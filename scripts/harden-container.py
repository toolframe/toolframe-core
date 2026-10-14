"""Remove unused pip bootstrap copies from the container's managed Python.

Toolframe prepares package environments with uv. Keeping pip and ensurepip adds
an independent set of vendored dependencies that Core never updates or uses.
"""

import shutil
import sys
import sysconfig
from pathlib import Path


def main():
    prefix = Path(sys.prefix).resolve()
    if not prefix.is_relative_to(Path("/opt/toolframe/python")):
        raise SystemExit("This script only supports container-managed Python")
    purelib = Path(sysconfig.get_path("purelib"))
    stdlib = Path(sysconfig.get_path("stdlib"))
    scripts = Path(sysconfig.get_path("scripts"))
    paths = [purelib / "pip", stdlib / "ensurepip", *purelib.glob("pip-*.dist-info")]
    paths.extend(
        scripts / name
        for name in (
            "pip",
            "pip3",
            f"pip{sys.version_info.major}.{sys.version_info.minor}",
        )
    )
    for path in paths:
        if not path.resolve().is_relative_to(prefix):
            raise SystemExit(
                f"Refusing to remove a path outside managed Python: {path}"
            )
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
