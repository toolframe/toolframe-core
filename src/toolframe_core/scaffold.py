"""Render the packaged Notes example into an exclusively created source directory."""

from __future__ import annotations

import ast
import io
import json
import re
import shutil
import tokenize
from importlib.resources import files
from pathlib import Path

import tomlkit
from toolframe_sdk import PluginError

from .config import validate_plugin_id
from .skills import bundled_skill


def _files(directory, prefix=Path()):
    # Traversable also works when core's resources are not ordinary source files.
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = prefix / child.name
        if child.is_dir():
            yield from _files(child, relative)
        else:
            yield relative, child.read_bytes()


def _python(source, rename):
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.STRING:
            value = ast.literal_eval(token.string)
            if isinstance(value, str) and (replacement := rename(value)) != value:
                token = token._replace(string=json.dumps(replacement))
        elif token.type == tokenize.NAME:
            token = token._replace(string=rename(token.string))
        tokens.append(token)
    return tokenize.untokenize(tokens)


def _render(identifier, package, module, presentation="dashboard"):
    template = dict(
        _files(
            files("toolframe_core").joinpath("web_template")
            if presentation == "web"
            else bundled_skill().joinpath("assets", "sample-plugin")
        )
    )
    document = tomlkit.parse(template[Path("pyproject.toml")].decode("utf-8"))
    project = document["project"]
    entries = project["entry-points"]["toolframe.plugins"]
    if len(entries) != 1:
        raise ValueError("Bundled template must declare exactly one plugin entry point")
    original_id, entry = next(iter(entries.items()))
    original_module = entry.split(".", 1)[0]
    wheel = document["tool"]["hatch"]["build"]["targets"]["wheel"]
    if wheel["packages"] != [f"src/{original_module}"]:
        raise ValueError("Bundled template must declare one src-layout Python package")

    identities = {
        original_id: identifier,
        project["name"]: package,
        original_module: module,
    }
    prefixes = {
        f"{original_id}.": f"{identifier}.",  # Service namespace.
        f"{original_id}-": f"{identifier}-",  # Streamlit widget keys.
        f"{original_id}__": f"{identifier}__",  # Qualified MCP tool names.
        f"{original_module}.": f"{module}.",  # Imports and module:factory references.
    }

    def rename(value):
        if value in identities:
            return identities[value]
        for before, after in prefixes.items():
            if value.startswith(before):
                return after + value[len(before) :]
        return value

    project["name"] = package
    project["entry-points"]["toolframe.plugins"] = {identifier: rename(entry)}
    wheel["packages"] = [f"src/{module}"]
    template[Path("pyproject.toml")] = tomlkit.dumps(document).encode("utf-8")

    rendered = {}
    for relative, content in template.items():
        if relative.parts[:2] == ("src", original_module):
            relative = Path("src", module, *relative.parts[2:])
        if relative.suffix == ".py":
            source = _python(content.decode("utf-8"), rename)
            compile(source, str(relative), "exec")
            content = source.encode("utf-8")
        elif relative.suffix == ".md":
            # Only code spans name callable tools/packages; prose stays verbatim.
            source = re.sub(
                r"`([^`\n]+)`",
                lambda match: f"`{rename(match[1])}`",
                content.decode("utf-8"),
            )
            content = source.encode("utf-8")
        elif presentation == "web" and relative.suffix in {".toml", ".html", ".js"}:
            content = (
                content.decode("utf-8")
                .replace(original_module, module)
                .replace(original_id, identifier)
                .encode("utf-8")
            )
        if presentation == "web" and relative.suffix == ".md":
            content = (
                content.decode("utf-8")
                .replace(original_module, module)
                .replace(original_id, identifier)
                .encode("utf-8")
            )
        rendered[relative] = content
    return rendered


def new_plugin(
    home: Path, identifier: str, *, output: Path | None = None, presentation="dashboard"
):
    validate_plugin_id(identifier)
    package = f"toolframe-{identifier}"
    # The existing ID grammar permits a trailing '-', unlike distribution names.
    if package.endswith("-"):
        package += "plugin"
    module = package.replace("-", "_")
    # Do not resolve the last component: that would follow a destination symlink.
    destination = (
        output if output is not None else home / "plugins" / identifier
    ).absolute()
    conflict = PluginError(
        "scaffold_conflict",
        f"Refusing to overwrite existing target: {destination}",
        {"path": str(destination)},
    )
    if destination.exists() or destination.is_symlink():
        raise conflict
    rendered = _render(identifier, package, module, presentation)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # mkdir is exclusive, including an empty directory or dangling symlink
        # created after the first check. Only our successfully created tree is ours.
        destination.mkdir()
    except FileExistsError as error:
        raise conflict from error
    try:
        for relative, content in rendered.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(content)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {
        "plugin_id": identifier,
        "package_name": package,
        "python_module": module,
        "path": str(destination),
        "status": "created",
    }
