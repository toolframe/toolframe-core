"""Safe new-application defaults without reinterpreting existing compositions."""

import pytest
import tomlkit
from toolframe_sdk import PluginError

from toolframe_core.access import Identity
from toolframe_core.config import ConfigFile, atomic_write
from toolframe_core.host import Host
from toolframe_core.templates import initialize_template


def test_new_and_legacy_configuration_have_distinct_defaults(tmp_path):
    config = ConfigFile(tmp_path)
    assert config.initialize().access.failure_mode == "closed"
    assert config.read()[0].access.failure_mode == "closed"
    document = tomlkit.parse(config.path.read_text())
    del document["access"]["failure_mode"]
    atomic_write(config.path, tomlkit.dumps(document))
    before = config.path.read_bytes()
    assert config.read()[0].access.failure_mode == "public"
    assert config.path.read_bytes() == before


@pytest.mark.parametrize(
    "explicit,expected", [(None, "closed"), ("public", "public"), ("closed", "closed")]
)
async def test_new_template_preserves_explicit_failure_policy(
    tmp_path, explicit, expected
):
    template = tmp_path / "template.toml"
    source = '[application]\nid="template"\n[access]\nmode="protected"\n'
    if explicit:
        source += f'failure_mode="{explicit}"\n'
    template.write_text(source)
    config = ConfigFile(tmp_path / "app")
    await initialize_template(config, str(template))
    assert config.read()[0].access.failure_mode == expected


@pytest.mark.parametrize(
    "previous,explicit,expected",
    [
        ("public", None, "closed"),
        ("protected", None, "public"),
        ("public", "public", "public"),
        ("public", "closed", "closed"),
    ],
)
async def test_selecting_protected_preserves_prior_policy(
    tmp_path, previous, explicit, expected
):
    config = ConfigFile(tmp_path)
    config.initialize()
    document = tomlkit.parse(config.path.read_text())
    document["access"]["mode"] = previous
    del document["access"]["failure_mode"]
    if explicit:
        document["access"]["failure_mode"] = explicit
    atomic_write(config.path, tomlkit.dumps(document))
    host = await Host.create(tmp_path)
    await host.start()
    try:
        await host.access.manage(
            Identity(local=True),
            "policy",
            {
                "mode": "protected",
                "expected_revision": config.revision,
            },
        )
        assert config.read()[0].access.failure_mode == expected
        assert host.composition.access.failure_mode == expected
    finally:
        await host.close()


async def test_failure_policy_validation_authority_and_revision(tmp_path):
    config = ConfigFile(tmp_path)
    config.initialize()
    host = await Host.create(tmp_path)
    await host.start()
    try:
        before = config.path.read_bytes()
        for value in (None, [], True, "open"):
            with pytest.raises(PluginError) as error:
                await host.access.manage(
                    Identity(local=True),
                    "policy",
                    {
                        "mode": "protected",
                        "failure_mode": value,
                        "expected_revision": config.revision,
                    },
                )
            assert error.value.code == "invalid_request"
        for identity, revision in (
            (Identity(), config.revision),
            (Identity(local=True), "stale"),
        ):
            with pytest.raises(PluginError):
                await host.access.manage(
                    identity,
                    "policy",
                    {
                        "mode": "protected",
                        "failure_mode": "public",
                        "expected_revision": revision,
                    },
                )
        assert config.path.read_bytes() == before
    finally:
        await host.close()
