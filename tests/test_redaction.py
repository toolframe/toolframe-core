# ruff: noqa: F811 -- shared pytest fixture imported from test_host.
import json

import httpx
import pytest
import tomlkit
from test_auth import app_host, client, credential, operate  # noqa: F401
from test_host import fixture_source, host  # noqa: F401

from toolframe_core.api import create_app
from toolframe_core.redaction import redact, redact_toml, secret_values


async def test_composition_export_redacts_rotated_schema_secrets_before_refresh(
    app_host, tmp_path
):
    host = app_host
    source = fixture_source(tmp_path, "alpha")
    module = source / "fixture/__init__.py"
    schema = {
        "$defs": {"private": {"type": "string", "writeOnly": True}},
        "properties": {
            "opaque": {"$ref": "#/$defs/private"},
            "pin": {"type": "integer", "x-secret": True},
            "value": {"type": "string", "format": "password"},
            "label": {"type": "string"},
            "api_token": {"type": "string"},
            "previous": {"type": "string"},
        },
    }
    module.write_text(
        module.read_text().replace(
            "id='alpha',", f"id='alpha',config_schema={schema!r},"
        )
    )
    old = {
        "opaque": "old-opaque",
        "pin": 481516,
        "value": "old-value",
        "api_token": "old-key",
    }
    new = {
        "opaque": "new-opaque",
        "pin": 234267,
        "value": "new-value",
        "api_token": "new-key",
    }
    await operate(
        host, "add", source=str(source), enable=True, config={**old, "label": "visible"}
    )
    await credential(host, scopes=["system.plugins.read"])
    async with client(host) as reader, client(host) as anonymous:
        denied = await anonymous.get("/_toolframe/v1/composition")
        assert denied.status_code == 401
        login = await reader.post(
            "/_toolframe/auth/login", json={"password": "test-password-read"}
        )
        assert login.status_code == 303
        denied = await reader.post(
            "/_toolframe/v1/operations",
            json={"action": "reload", "params": {"id": "alpha"}},
        )
        assert denied.status_code == 403

        async def exported():
            original = host.config.path.read_bytes()
            revision = host.revision
            loaded = host.composition.model_dump()
            response = await reader.get("/_toolframe/v1/composition")
            assert response.status_code == 200, response.text
            assert response.json()["secrets"] == "redacted"
            assert host.config.path.read_bytes() == original
            assert host.revision == revision and host.composition.model_dump() == loaded
            text = response.json()["toml"]
            config = tomlkit.parse(text)["plugins"]["alpha"]["config"]
            assert config["label"] == "visible"
            for field in old:
                assert config[field] == "[REDACTED]", (field, config[field])
            return text, config

        await exported()
        document = tomlkit.parse(host.config.path.read_text())
        document["plugins"]["alpha"]["config"].update(
            {**new, "previous": old["opaque"]}
        )
        document.add(tomlkit.comment("Preserve export formatting"))
        host.config.path.write_text(tomlkit.dumps(document))
        assert host.composition.plugins["alpha"].config["opaque"] == old["opaque"]
        text, config = await exported()
        assert config["previous"] == "[REDACTED]"
        assert "# Preserve export formatting" in text
        assert all(str(value) not in text for value in [*old.values(), *new.values()])
        await host.refresh()
        await exported()


def test_schema_secrets_urls_and_short_credentials_are_redacted_without_changing_digests():
    schema = {
        "$defs": {"private": {"type": "string", "writeOnly": True}},
        "properties": {
            "opaque": {"$ref": "#/$defs/private"},
            "pin": {"type": "integer", "x-secret": True},
            "nested": {"items": {"properties": {"value": {"format": "password"}}}},
            "public": True,
        },
    }
    values = {
        "opaque": "x",
        "pin": 1234,
        "nested": [{"value": "test-credential"}],
        "public": "visible",
    }
    secrets = secret_values(values, schema)
    assert secrets == {"x", "1234", "test-credential"}
    digest = "1234" * 16
    report = redact(
        {
            "config": values,
            "digest": digest,
            "message": "Rejected x and test-credential",
            "source": "https://user:pass@host/wheel.whl?token=private",
        },
        secrets,
    )
    assert report["config"]["opaque"] == "[REDACTED]"
    assert report["config"]["pin"] == "[REDACTED]"
    assert report["config"]["public"] == "visible"
    assert report["digest"] == digest
    assert "test-credential" not in report["message"]
    assert "user:pass" not in report["source"] and "private" not in report["source"]
    text = '# retained comment\n[config]\nopaque="x"\npublic="visible"\n'
    exported = redact_toml(text, lambda value: redact(value, secrets))
    assert "# retained comment" in exported and 'opaque="[REDACTED]"' in exported


@pytest.mark.asyncio
async def test_management_redacts_config_errors_operations_export_and_worker_logs(
    host, tmp_path
):
    source = fixture_source(tmp_path, "alpha")
    module = source / "fixture/__init__.py"
    module.write_text(
        module.read_text()
        .replace(
            "id='alpha',",
            "id='alpha',config_schema={'type':'object','properties':{'opaque':{'type':'string','writeOnly':True},'label':{'type':'string'}},'additionalProperties':False},",
        )
        .replace(
            "        return {'version': 'old'}",
            "        print(self.context.config['opaque'], flush=True)\n        raise RuntimeError('rejected '+self.context.config['opaque'])",
        )
    )
    secret = "test-only-private-credential"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(host, manage_lifespan=False)),
        base_url="http://127.0.0.1:9876",
        headers={"Authorization": f"Bearer {host.token}"},
    ) as client:
        added = await client.post(
            "/_toolframe/v1/operations",
            json={
                "action": "add",
                "params": {
                    "source": str(source),
                    "enable": True,
                    "config": {"opaque": secret, "label": "visible"},
                },
            },
        )
        assert added.status_code == 202 and secret not in added.text
        result = await host.wait_operation(added.json()["id"])
        assert result["status"] == "succeeded", result
        original = host.config.path.read_text()
        assert secret in original
        failed = await client.post(
            "/_toolframe/v1/services/alpha/alpha.service/echo", json={}
        )
        assert failed.status_code >= 400 and secret not in failed.text
        for endpoint in (
            "status",
            "operations",
            "operations/" + result["id"],
            "plugins/alpha/logs",
            "composition",
        ):
            response = await client.get("/_toolframe/v1/" + endpoint)
            assert response.status_code == 200
            assert secret not in response.text and host.token not in response.text
        status = (await client.get("/_toolframe/v1/status")).json()
        assert status["plugins"][0]["desired"]["config"] == {
            "opaque": "[REDACTED]",
            "label": "visible",
        }
        assert host.config.path.read_text() == original
        assert (host.home / "data/core.sqlite").stat().st_mode & 0o777 == 0o600
        assert "visible" in json.dumps(status)
