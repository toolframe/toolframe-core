# ruff: noqa: F811 -- shared pytest fixture imported from test_host.
import json

import httpx
import pytest
from test_host import fixture_source, host  # noqa: F401

from toolframe_core.api import create_app
from toolframe_core.redaction import redact, redact_toml, secret_values


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
