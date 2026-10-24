"""Fixed authoring protocol runner, executed by the selected isolated interpreter.

Kept stdlib-only so authoring-only distributions need no Toolframe runtime entry.
"""

import importlib
import json
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    module, name = request["entrypoint"].split(":")
    entry = getattr(importlib.import_module(module), name)
    if request["operation"] == "generate":
        entry(
            destination=Path(request["destination"]), parameters=request["parameters"]
        )
    elif request["operation"] == "prepare":
        entry(
            source=Path(request["source"]),
            destination=Path(request["destination"]),
            cache=Path(request["cache"]),
            offline=request["offline"],
        )
    else:
        raise ValueError("Unknown authoring operation")


if __name__ == "__main__":
    main()
