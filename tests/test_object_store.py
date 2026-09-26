"""Host allocation, authorization and lifecycle for independent object storage."""

# ruff: noqa: F811 -- share the real-worker fixture with host tests.
from pathlib import Path

import pytest
from test_host import fixture_source, host, operate  # noqa: F401
from toolframe_sdk import PluginError, Principal


def provider_source(directory):
    source = fixture_source(directory, "objects")
    (source / "fixture/__init__.py").write_text("""from pathlib import Path
from toolframe_sdk import AuthorizationSpec, Plugin, PluginDefinition, ServiceSpec, MethodSpec
class Objects(Plugin):
    async def activate(self):
        self.directory = Path(self.context.state_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
    async def invoke(self, service, method, params):
        return {"directory":str(self.directory),"owner":self.context.call_info["owner"]}
plugin = PluginDefinition(id="objects",kind="provider",provider_kind="object-store",factory="fixture:Objects",
    authorization=AuthorizationSpec(), services={"toolframe.object_store":ServiceSpec(methods={"head":MethodSpec(audience="internal",read_only=True)})})
""")
    return source


def consumer_source(directory):
    source = fixture_source(directory, "gallery")
    (
        source / "fixture/__init__.py"
    ).write_text("""from toolframe_sdk import AuthorizationSpec, ScopeSpec, Plugin, PluginDefinition, Requirement, ServiceSpec, MethodSpec
class Gallery(Plugin):
    async def invoke(self, service, method, params):
        return await self.context.object_store().head("photo")
plugin=PluginDefinition(id="gallery",factory="fixture:Gallery",
    authorization=AuthorizationSpec(scopes={"gallery.read":ScopeSpec(title="Read")}),
    requires={"objects":Requirement(service="toolframe.object_store")},
    services={"gallery.images":ServiceSpec(methods={"read":MethodSpec(audience="business",required_scopes=("gallery.read",),read_only=True)})})
""")
    return source


async def test_object_provider_state_owner_scope_and_reload(host, tmp_path):
    for source in (provider_source(tmp_path), consumer_source(tmp_path)):
        result = await operate(host, "add", source=str(source), enable=True)
        assert result["status"] == "succeeded", result
    result = await host.call_service(
        "gallery",
        "gallery.images",
        "read",
        {},
        principal=Principal(kind="password", scopes=("gallery.read",)),
    )
    assert result == {
        "directory": str(host.home / "data/objects/objects"),
        "owner": "gallery",
    }
    marker = Path(result["directory"]) / "retained"
    marker.write_bytes(b"keep")
    with pytest.raises(PluginError) as error:
        await host.call_service(
            "gallery",
            "gallery.images",
            "read",
            {},
            principal=Principal(kind="password"),
        )
    assert error.value.code == "forbidden"
    assert (await operate(host, "reload", id="objects"))["status"] == "succeeded"
    assert marker.read_bytes() == b"keep"
    result = await host.call_service("gallery", "gallery.images", "read", {})
    assert result["owner"] == "gallery"
    assert (await operate(host, "disable", id="objects"))["status"] == "succeeded"
    assert marker.read_bytes() == b"keep"
    with pytest.raises(PluginError):
        await host.call_service("gallery", "gallery.images", "read", {})
