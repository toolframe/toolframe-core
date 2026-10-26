"""Resolve real wheel metadata with uv, including index withdrawal and isolation."""

import base64
import hashlib
import html
import threading
import zipfile
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dependencies import wheelhouse
from packaging.utils import canonicalize_name
from toolframe_sdk import PluginError
from toolframe_sdk import __version__ as SDK_VERSION

from toolframe_core.config import PackageSettings, PluginRequest
from toolframe_core.packages import Packages
from toolframe_core.selection import prepare_selection


def make_wheel(
    directory,
    name,
    version,
    *,
    requires=(),
    extras=(),
    python=">=3.14",
    tag="py3-none-any",
    plugin=None,
    definition=None,
    assets=None,
):
    directory.mkdir(parents=True, exist_ok=True)
    normalized = name.replace("-", "_")
    dist = f"{normalized}-{version}.dist-info"
    metadata = (
        f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: {python}\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requires)
        + "".join(f"Provides-Extra: {item}\n" for item in extras)
        + "\n"
    )
    contents = {
        f"{dist}/METADATA": metadata,
        f"{dist}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n",
        **(assets or {}),
    }
    if plugin:
        contents[f"{dist}/entry_points.txt"] = (
            f"[toolframe.plugins]\n{plugin} = {normalized}:plugin\n"
        )
        definition = definition or (
            "authorization=AuthorizationSpec(scopes={'fixture.use': ScopeSpec(title='Use')}), "
            f"services={{'{plugin}.service': ServiceSpec(methods={{'echo': MethodSpec("
            "audience='business', required_scopes=('fixture.use',), read_only=True)})}, "
        )
        definition = definition.replace("fixture.use", f"{plugin}.use")
        if "authorization=" not in definition:
            definition = "authorization=AuthorizationSpec(), " + definition
        contents[f"{normalized}/__init__.py"] = f"""from toolframe_sdk import (
    Plugin, PluginDefinition, AuthorizationSpec, ScopeSpec, ServiceSpec, MethodSpec, Resource
)
plugin = PluginDefinition(id={plugin!r}, factory='{normalized}:Example', {definition})
class Example(Plugin):
    async def invoke(self, service, method, params):
        return {{'version': {version!r}}}
def page(context):
    pass
"""
    record = []
    target = directory / f"{normalized}-{version}-{tag}.whl"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in contents.items():
            data = content.encode()
            archive.writestr(path, data)
            sha = (
                base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                .decode()
                .rstrip("=")
            )
            record.append(f"{path},sha256={sha},{len(data)}")
        record.append(f"{dist}/RECORD,,")
        archive.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    return target


@pytest.fixture
def index(tmp_path, monkeypatch):
    """A real Simple API; file URLs keep capture within the local fixture boundary."""
    releases = {}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            name = self.path.rstrip("/").rsplit("/", 1)[-1]
            body = "<html>" + "".join(releases.get(name, [])) + "</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = tmp_path / "index-wheels"

    def publish(name, version, *, yanked=False, **kwargs):
        artifact = make_wheel(directory, name, version, **kwargs)
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        releases.setdefault(canonicalize_name(name), []).append(
            f'<a href="{artifact.as_uri()}#sha256={checksum}"'
            + (' data-yanked="withdrawn"' if yanked else "")
            + f' data-requires-python="{html.escape(kwargs.get("python", ">=3.14"), quote=True)}">'
            + artifact.name
            + "</a>"
        )
        return artifact

    sdk = publish("toolframe-sdk", SDK_VERSION)
    monkeypatch.setenv("TOOLFRAME_SDK_WHEEL", str(sdk))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    settings = PackageSettings(
        index_url=f"http://127.0.0.1:{server.server_port}/simple"
    )
    packages = Packages(tmp_path / "app", settings)
    try:
        yield packages, publish, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def test_latest_compatible_stable_skips_sdk_python_platform_and_yanked(index):
    packages, publish, _ = index
    publish = partial(publish, "fixture-choice", plugin="choice")
    publish("1.0", requires=[f"toolframe-sdk=={SDK_VERSION}"])
    publish("1.1", requires=[f"toolframe-sdk=={SDK_VERSION}"])
    publish("1.2", requires=["toolframe-sdk>=99"])
    publish("1.3", python=">=99")
    publish("1.4", tag="cp39-cp39-win32")
    publish("1.5", yanked=True)
    publish("1.6rc1")
    selected = await packages.capture("pypi:fixture-choice>=1,<2")
    assert selected["version"] == "1.1"
    assert selected["source"]["request"] == "pypi:fixture-choice>=1,<2"
    assert (
        hashlib.sha256((packages.home / selected["artifact"]).read_bytes()).hexdigest()
        == selected["artifact_hash"]
    )


@pytest.mark.parametrize(
    "requirement", ["fixture-choice>=2,<3", "fixture-choice>=1.6,<2"]
)
async def test_no_compatible_stable_version_reports_request_and_runtime(
    index, requirement
):
    packages, publish, _ = index
    publish("fixture-choice", "1.6rc1", plugin="choice")
    publish("fixture-choice", "2.0", plugin="choice", requires=["toolframe-sdk>=99"])
    with pytest.raises(PluginError) as error:
        await packages.capture("pypi:" + requirement)
    assert error.value.code == "no_compatible_version"
    assert requirement.split(">=")[0] in error.value.message
    assert error.value.details["sdk"] == SDK_VERSION


@pytest.mark.parametrize(
    "version,yanked,bounds",
    [("1.0rc1", False, ""), ("1.1", True, ""), ("1.0rc1", False, ",>=0.9,<2")],
)
async def test_explicit_pin_keeps_prerelease_or_yanked_choice(
    index, version, yanked, bounds
):
    packages, publish, _ = index
    publish("fixture-choice", version, plugin="choice", yanked=yanked)
    source = f"pypi:fixture-choice=={version}{bounds}"
    selected = await packages.capture(source)
    assert selected["version"] == version
    assert selected["source"]["request"] == source


async def test_backend_and_requested_extras_participate_in_backtracking(index):
    packages, publish, _ = index
    publish("fixture-choice", "1.0", plugin="choice", extras=["backend", "special"])
    publish(
        "fixture-choice",
        "1.1",
        plugin="choice",
        extras=["backend"],
        requires=[
            'toolframe-sdk>=99; extra == "backend"',
        ],
    )
    publish(
        "fixture-choice",
        "1.2",
        plugin="choice",
        extras=["special"],
        requires=[
            'toolframe-sdk>=99; extra == "special"',
        ],
    )
    assert (await packages.capture("pypi:fixture-choice[special]>=1,<2"))[
        "version"
    ] == "1.0"
    assert (await packages.capture("pypi:fixture-choice>=1,<2"))["version"] == "1.2"


async def test_core_requirement_cannot_downgrade_the_host(index):
    packages, publish, _ = index
    publish("fixture-choice", "1.0", plugin="choice")
    publish("fixture-choice", "1.1", plugin="choice", requires=["toolframe-core>=99"])
    assert (await packages.capture("pypi:fixture-choice>=1,<2"))["version"] == "1.0"


async def test_stable_plugin_can_require_an_explicit_prerelease_dependency(index):
    packages, publish, _ = index
    publish("fixture-support", "1.0b1")
    publish(
        "fixture-choice", "1.0", plugin="choice", requires=["fixture-support==1.0b1"]
    )
    publish("fixture-choice", "1.1rc1", plugin="choice")
    assert (await packages.capture("pypi:fixture-choice>=1,<2"))["version"] == "1.0"


async def test_offline_capture_uses_verified_local_artifacts_or_fails(index):
    packages, publish, requests = index
    publish("fixture-choice", "1.0", plugin="choice")
    source = "pypi:fixture-choice>=1,<2"
    selected = await packages.capture(source)
    packages.settings.offline = True
    before = list(requests)
    assert (await packages.capture(source))["artifact_hash"] == selected[
        "artifact_hash"
    ]
    assert requests == before
    (packages.home / selected["artifact"]).write_bytes(b"modified")
    with pytest.raises(PluginError) as error:
        await packages.capture(source)
    assert error.value.code == "artifact_hash_mismatch"
    with pytest.raises(PluginError) as error:
        await packages.capture("pypi:fixture-absent>=1,<2")
    assert error.value.code == "no_compatible_version"
    assert "offline" in error.value.message
    assert requests == before


async def test_workers_resolve_independently_and_ui_can_select_an_older_plugin(
    tmp_path,
):
    local = tmp_path / "wheels"
    for version in ("1.0", "2.0", "3.0"):
        make_wheel(local, "fixture-shared", version)
    make_wheel(
        local,
        "fixture-independent",
        "1.0",
        plugin="independent",
        requires=["fixture-shared==3.0"],
    )
    make_wheel(
        local,
        "fixture-dashboard",
        "1.0",
        plugin="dashboard",
        extras=["ui"],
        requires=['fixture-shared==1.0; extra == "ui"'],
        definition="kind='provider', provider_kind='dashboard',",
    )
    for version, dependency in (("1.0", "1.0"), ("1.1", "2.0")):
        make_wheel(
            local,
            "fixture-page",
            version,
            plugin="page",
            extras=["backend", "ui"],
            requires=[
                f'fixture-shared=={dependency}; extra == "ui"',
                'fixture-shared==2.0; extra == "backend"',
            ],
            definition="authorization=AuthorizationSpec(scopes={'page.view':ScopeSpec(title='View')}), "
            "resources=(Resource(id='main',kind='dashboard.page',required_scopes=('page.view',),data={'entrypoint':'fixture_page:page'}),),",
        )
    packages = Packages(
        tmp_path / "app", PackageSettings(find_links=[str(local), str(wheelhouse())])
    )
    sources = {
        "dashboard": "pypi:fixture-dashboard>=1,<2",
        "page": "pypi:fixture-page>=1,<2",
        "independent": "pypi:fixture-independent>=1,<2",
    }
    selected = await prepare_selection(
        packages,
        {
            key: PluginRequest(source=value, enabled=True)
            for key, value in sources.items()
        },
        {},
        pin=True,
    )
    assert selected["page"]["version"] == "1.0"
    assert selected["page"]["source"]["requirement"] == sources["page"]
    assert selected["page"]["source"]["request"] == "pypi:fixture-page==1.0"
    assert 'version = "2.0"' in selected["page"]["environment"]["lock"]
    assert 'version = "1.0"' in selected["dashboard"]["ui_environment"]["lock"]
    assert (
        selected["page"]["environment"]["id"]
        != selected["dashboard"]["environment"]["id"]
    )
    assert 'version = "3.0"' in selected["independent"]["environment"]["lock"]
    assert "independent" not in selected["dashboard"]["ui_cohort"]
