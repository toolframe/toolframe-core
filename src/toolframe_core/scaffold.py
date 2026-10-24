"""Compatibility aliases and rendering of the canonical manifest result."""

import asyncio
import json

from toolframe_sdk import PluginError

from .config import ConfigFile


def select_template(template=None, presentation=None):
    if template and presentation:
        raise PluginError(
            "malformed_input", "Use --template or --presentation, not both"
        )
    aliases = {
        "web": "toolframe-web/notes",
        "dashboard": "toolframe-streamlit-dashboard/notes",
    }
    return template or aliases.get(presentation, "toolframe-core/basic")


def new_plugin(
    home,
    identifier,
    *,
    output=None,
    presentation=None,
    template=None,
    source=None,
    parameters=None,
):
    from .authoring import new_plugin as generate

    return asyncio.run(
        generate(
            ConfigFile(home),
            identifier,
            output=output,
            template=select_template(template, presentation),
            source=source,
            parameters=parameters,
        )
    )


def describe_templates(result):
    if result.get("catalog"):
        lines = ["Official package directory (not the local template list)"]
        for package in result["catalog"]:
            lines.append(
                f"{package['distribution']} — {package['title']} [{package['source']['availability']}]"
            )
            lines.append(package["description"])
            for template in package["templates"]:
                lines.append(
                    f"  {template['id']}: {template['title']} — {template['description']}"
                )
        return "\n".join(lines)
    templates = result.get("templates", [result])
    lines = (
        ["Local templates (installed distributions and captured wheels)"]
        if "templates" in result
        else []
    )
    for item in templates:
        compatibility = item["compatibility"]
        status = "compatible" if compatibility["compatible"] else "incompatible"
        lines.extend(
            [
                f"{item['id']} ({item['distribution']}=={item['version']})",
                item["title"],
                item["description"],
                "Source: " + json.dumps(item["source"], sort_keys=True),
                "Requires Core: " + item["requires_core"],
                "Requires Python: " + (item["requires_python"] or "any"),
                f"Compatibility: {status} (Core {compatibility['core_version']}, Python {compatibility['python_version']})",
                *compatibility["reasons"],
                "Parameters: " + json.dumps(item["parameters"], sort_keys=True),
            ]
        )
    if not templates:
        lines.append("No local templates. Use --from or template list --catalog.")
    return "\n".join(lines)
