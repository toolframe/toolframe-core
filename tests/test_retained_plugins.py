"""Installed plugins survive bundle/source replacement without implicit updates."""

# ruff: noqa: F811 -- shared pytest fixture imported from test_host.
from shutil import copy2, rmtree

import pytest
from test_host import fixture_source, host, operate  # noqa: F401
from toolframe_sdk import PluginError

from toolframe_core.packages import pinned_index_source


@pytest.mark.parametrize("reviewed", [False, True])
async def test_pinned_plugin_survives_bundle_replacement(
    host, tmp_path, monkeypatch, reviewed
):
    source = fixture_source(tmp_path, "retained")
    artifact = await host.packages.capture(str(source))
    wheel = host.home / artifact["artifact"]
    bundle = tmp_path / "old-bundle" / wheel.stem
    bundle.mkdir(parents=True)
    copy2(wheel, bundle / wheel.name)
    monkeypatch.setenv("TOOLFRAME_BUNDLED_WHEELHOUSE", str(bundle.parent))
    request = "pypi:fixture-retained==0.1.0"
    added = await operate(host, "add", source=request, enable=True)
    assert added["status"] == "succeeded", added
    installed = host.runtimes["retained"].candidate["id"]
    rmtree(bundle.parent)
    monkeypatch.setenv("TOOLFRAME_BUNDLED_WHEELHOUSE", str(tmp_path / "new-bundle"))
    resolutions = []
    capture = host.packages.capture

    async def unavailable(source):
        resolutions.append(source)
        raise PluginError(
            "source_unavailable", "The old release is no longer available"
        )

    monkeypatch.setattr(host.packages, "capture", unavailable)
    assert not host.packages.settings.offline

    async def apply(action):
        params = {} if action == "apply" else {"id": "retained"}
        if reviewed and action != "validate":
            revision = host.config.revision
            pending = host.submit("plan", {"action": action, **params})
            plan = await host.wait_operation(pending["id"])
            assert plan["status"] == "succeeded", plan
            assert host.config.revision == revision
            params["plan_id"] = plan["result"]["plan_id"]
        result = await operate(host, action, **params)
        assert result["status"] == "succeeded", result

    await apply("disable")
    assert "retained" not in host.runtimes
    await apply("enable")
    await apply("reload")
    await apply("validate")
    await apply("apply")
    assert host.runtimes["retained"].candidate["id"] == installed
    assert await host.call_service("retained", "retained.service", "echo", {}) == {
        "version": "old"
    }
    assert resolutions == []

    # An explicit update still consults the source and reports failure honestly.
    generation = host.runtimes["retained"].generation
    update = await operate(host, "update", id="retained")
    assert update["error"]["code"] == "source_unavailable"
    assert resolutions == [request]
    assert host.runtimes["retained"].generation == generation

    monkeypatch.setattr(host.packages, "capture", capture)
    fixture_source(tmp_path, "retained", version="updated")
    replacement = await capture(str(source))
    new_bundle = tmp_path / "new-bundle" / wheel.stem
    new_bundle.mkdir(parents=True)
    copy2(host.home / replacement["artifact"], new_bundle / wheel.name)
    updated = await operate(host, "update", id="retained")
    assert updated["status"] == "succeeded", updated
    assert updated["result"]["candidate"] != installed
    assert host.runtimes["retained"].generation == generation
    await apply("reload")
    assert await host.call_service("retained", "retained.service", "echo", {}) == {
        "version": "updated"
    }


async def test_enable_uses_installed_local_snapshot_until_explicit_refresh(
    host, tmp_path
):
    source = fixture_source(tmp_path, "local")
    added = await operate(host, "add", source=str(source), enable=True)
    assert added["status"] == "succeeded", added
    assert (await operate(host, "disable", id="local"))["status"] == "succeeded"
    fixture_source(tmp_path, "local", version="edited")
    enabled = await operate(host, "enable", id="local")
    assert enabled["status"] == "succeeded", enabled
    assert await host.call_service("local", "local.service", "echo", {}) == {
        "version": "old"
    }
    reloaded = await operate(host, "reload", id="local")
    assert reloaded["status"] == "succeeded", reloaded
    assert await host.call_service("local", "local.service", "echo", {}) == {
        "version": "edited"
    }
    assert (await operate(host, "disable", id="local"))["status"] == "succeeded"
    rmtree(source)
    assert (await operate(host, "enable", id="local"))["status"] == "succeeded"


async def test_retained_preflight_validates_new_config_and_changed_source(
    host, tmp_path
):
    source = fixture_source(tmp_path, "configured")
    module = source / "fixture/__init__.py"
    module.write_text(
        module.read_text().replace(
            "id='configured',",
            "id='configured', config_schema={'type':'object','properties':{'count':{'type':'integer'}}},",
        )
    )
    added = await operate(host, "add", source=str(source))
    assert added["status"] == "succeeded", added
    request = host.composition.plugins["configured"]
    host.config.put_plugin(
        "configured",
        request.model_copy(update={"config": {"count": "invalid"}}),
        host.config.revision,
    )
    invalid = await operate(host, "enable", id="configured")
    assert invalid["error"]["code"] == "config_invalid"
    assert not host.composition.plugins["configured"].enabled
    replacement = fixture_source(
        tmp_path / "replacement", "configured", version="new source"
    )
    host.config.put_plugin(
        "configured",
        request.model_copy(update={"source": str(replacement)}),
        host.config.revision,
    )
    enabled = await operate(host, "enable", id="configured")
    assert enabled["status"] == "succeeded", enabled
    assert await host.call_service("configured", "configured.service", "echo", {}) == {
        "version": "new source"
    }


@pytest.mark.parametrize("damage", ["modified", "missing"])
async def test_damaged_retained_wheel_does_not_enable_or_change_intent(
    host, tmp_path, damage
):
    source = fixture_source(tmp_path, "damaged")
    added = await operate(host, "add", source=str(source))
    assert added["status"] == "succeeded", added
    candidate = host.state.candidate(added["result"]["candidate"])
    wheel = host.home / candidate["artifact"]
    if damage == "modified":
        wheel.write_bytes(b"damaged wheel")
    else:
        wheel.unlink()
    revision = host.config.revision
    enabled = await operate(host, "enable", id="damaged")
    assert enabled["error"]["code"] == (
        "artifact_hash_mismatch" if damage == "modified" else "artifact_missing"
    )
    assert host.config.revision == revision
    assert not host.composition.plugins["damaged"].enabled
    assert not host.runtimes


@pytest.mark.parametrize(
    ("source", "pinned"),
    [
        ("pypi:example==1.2.3", True),
        ("pypi:example[backend]==1.2.3rc1", True),
        ("pypi:example", False),
        ("pypi:example>=1.2.3", False),
        ("pypi:example==1.2.*", False),
        ("pypi:example @ https://example.org/plugin.whl", False),
        ("path:plugin", False),
        ("git+https://example.org/plugin@main", False),
    ],
)
def test_only_exact_index_versions_use_retained_wheels_on_reload(source, pinned):
    assert pinned_index_source(source) is pinned
