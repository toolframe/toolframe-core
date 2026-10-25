"""Publish the existing application lock from journal candidate selections."""

import json

from .config import atomic_write


async def write_lock(home, state, composition, revision):
    plugins = {}
    for name in composition.plugins:
        row = await state.plugin(name)
        candidates = {}
        for selection in ("current_candidate", "last_candidate", "previous_candidate"):
            if row[selection]:
                candidate = await state.candidate(row[selection])
                candidates[selection] = {
                    key: candidate[key]
                    for key in (
                        "id",
                        "version",
                        "artifact",
                        "artifact_hash",
                        "source",
                        "environment",
                        "ui_environment",
                        "web_environment",
                    )
                    if key in candidate
                }
        plugins[name] = candidates
    atomic_write(
        home / "toolframe.lock",
        json.dumps(
            {"format": 1, "desired_revision": revision, "plugins": plugins}, indent=2
        )
        + "\n",
    )
    await state.set_meta("desired_revision", revision)
    await state.set_meta("last_valid_composition", composition.model_dump())
