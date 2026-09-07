"""One domain namespace per application startup; only UI modules reload at render."""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def prepare_domains(root: Path, *, reset: bool = False) -> None:
    """Bind arbitrary domains-dir paths before catalog imports without reloading services."""
    root = root.resolve(strict=True)
    previous = sys.modules.get("domains")
    if not reset and list(getattr(previous, "__path__", ())) == [str(root)]:
        return
    for name in tuple(sys.modules):
        if name == "domains" or name.startswith("domains."):
            sys.modules.pop(name, None)
    module = ModuleType("domains")
    module.__package__ = "domains"
    module.__path__ = [str(root)]
    module.__spec__ = importlib.util.spec_from_loader(
        "domains", loader=None, is_package=True
    )
    sys.modules["domains"] = module
    importlib.invalidate_caches()


def reload_domain_ui(root: Path) -> None:
    """Discard widget modules/helpers while retaining contracts, models and plugin state."""
    root = root.resolve(strict=True)
    prepare_domains(root)
    for name, module in tuple(sys.modules.items()):
        parts = name.split(".")
        is_ui = len(parts) >= 3 and parts[0] == "domains" and parts[2] == "ui"
        source = getattr(module, "__file__", None)
        if source and not is_ui:
            try:
                relative = Path(source).resolve(strict=False).relative_to(root)
                is_ui = len(relative.parts) >= 2 and relative.parts[1] == "ui"
            except (OSError, ValueError):
                pass
        if is_ui:
            sys.modules.pop(name, None)
    importlib.invalidate_caches()
