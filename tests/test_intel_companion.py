"""Pinned companion reuse must preserve identity and fail without source fallback."""

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "intel_companion", ROOT / "scripts/ci/intel_companion.py"
)
companion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(companion)


def wheel_bytes(recipe, *, name="cryptography", version=None, tag=None):
    stream = io.BytesIO()
    prefix = f"cryptography-{recipe['cryptography']['version']}.dist-info/"
    with zipfile.ZipFile(stream, "w") as wheel:
        wheel.writestr(
            prefix + "METADATA",
            f"Metadata-Version: 2.4\nName: {name}\n"
            f"Version: {version or recipe['cryptography']['version']}\n",
        )
        wheel.writestr(
            prefix + "WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            f"Tag: {tag or companion.wheel_tag(recipe)}\n",
        )
    return stream.getvalue()


@pytest.fixture
def manifest():
    value = companion.load_manifest()
    data = wheel_bytes(value["recipe"])
    value["artifact"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    return value, data


def test_download_and_reuse_never_compile(tmp_path, monkeypatch, manifest):
    value, data = manifest
    request = Mock(return_value=io.BytesIO(data))
    command = Mock(side_effect=AssertionError("Fetching a wheel must not compile"))
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    monkeypatch.setattr(companion.subprocess, "run", command)
    wheel = companion.fetch_companion(value, tmp_path)
    assert wheel.read_bytes() == data
    assert companion.fetch_companion(value, tmp_path) == wheel
    # Even the explicit update entry reuses the published artifact for this recipe.
    assert companion.build_companion(value, tmp_path) == wheel
    request.assert_called_once_with(value["artifact"]["url"], timeout=30)
    command.assert_not_called()
    assert list(tmp_path.iterdir()) == [wheel]


def test_pinned_manifest_matches_its_recipe():
    value = companion.load_manifest()
    artifact = companion.published_artifact(value)
    assert artifact["recipe_sha256"] == companion.recipe_identity(value["recipe"])
    assert artifact["filename"] == companion.wheel_name(value["recipe"])


def test_corrupt_download_is_rejected_without_retry_or_build(
    tmp_path, monkeypatch, manifest
):
    value, data = manifest
    request = Mock(return_value=io.BytesIO(data + b"corrupted"))
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    with pytest.raises(ValueError, match="Checksum mismatch"):
        companion.fetch_companion(value, tmp_path)
    assert request.call_count == 1
    assert not list(tmp_path.iterdir())


def test_corrupt_existing_wheel_is_not_silently_used_or_replaced(
    tmp_path, monkeypatch, manifest
):
    value, _ = manifest
    wheel = tmp_path / value["artifact"]["filename"]
    wheel.write_bytes(b"corrupt")
    request = Mock(side_effect=AssertionError("Do not hide a corrupt cached wheel"))
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    with pytest.raises(ValueError, match="Checksum mismatch"):
        companion.fetch_companion(value, tmp_path)
    assert wheel.read_bytes() == b"corrupt"
    request.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": "unrelated"},
        {"version": "50.0.1"},
        {"tag": "cp314-abi3-macosx_12_0_arm64"},
        {"tag": "cp313-abi3-macosx_12_0_x86_64"},
    ],
)
def test_matching_digest_does_not_accept_wrong_metadata(
    tmp_path, monkeypatch, manifest, metadata
):
    value, _ = manifest
    data = wheel_bytes(value["recipe"], **metadata)
    value["artifact"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    monkeypatch.setattr(
        companion.urllib.request, "urlopen", Mock(return_value=io.BytesIO(data))
    )
    with pytest.raises(ValueError, match="does not match"):
        companion.fetch_companion(value, tmp_path)
    assert not list(tmp_path.iterdir())


def test_transient_download_failure_has_bounded_retries(
    tmp_path, monkeypatch, manifest
):
    value, _ = manifest
    request = Mock(side_effect=urllib.error.URLError("connection unavailable"))
    sleep = Mock()
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    monkeypatch.setattr(companion.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        companion.fetch_companion(value, tmp_path)
    assert request.call_count == 3
    assert [call.args for call in sleep.call_args_list] == [(1,), (2,)]
    assert not list(tmp_path.iterdir())


def test_retry_can_recover_without_leaving_partial_files(
    tmp_path, monkeypatch, manifest
):
    value, data = manifest
    request = Mock(side_effect=[TimeoutError(), io.BytesIO(data)])
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    monkeypatch.setattr(companion.time, "sleep", Mock())
    wheel = companion.fetch_companion(value, tmp_path)
    assert wheel.read_bytes() == data
    assert list(tmp_path.iterdir()) == [wheel]


def test_missing_published_asset_is_not_a_build_request(
    tmp_path, monkeypatch, manifest
):
    value, _ = manifest
    error = urllib.error.HTTPError(value["artifact"]["url"], 404, "Not Found", {}, None)
    request = Mock(side_effect=error)
    monkeypatch.setattr(companion.urllib.request, "urlopen", request)
    with pytest.raises(RuntimeError, match="404"):
        companion.build_companion(value, tmp_path)
    assert request.call_count == 1


@pytest.mark.parametrize(
    "field", ["openssl", "cryptography", "python", "macos_deployment_target"]
)
def test_dependency_identity_change_requires_a_new_pin(
    tmp_path, monkeypatch, manifest, field
):
    value, _ = manifest
    if field in ("openssl", "cryptography"):
        value["recipe"][field]["version"] += ".1"
    else:
        value["recipe"][field] += ".1"
    monkeypatch.setattr(
        companion.urllib.request,
        "urlopen",
        Mock(side_effect=AssertionError("Do not fetch stale identity")),
    )
    with pytest.raises(ValueError, match="No published wheel matches"):
        companion.fetch_companion(value, tmp_path)


def test_artifact_origin_and_filename_are_fixed(tmp_path, monkeypatch, manifest):
    value, _ = manifest
    value["artifact"]["url"] = "https://example.com/" + value["artifact"]["filename"]
    with pytest.raises(ValueError, match="fixed Toolframe Release"):
        companion.fetch_companion(value, tmp_path)
    value["artifact"]["filename"] = "../unrelated.whl"
    with pytest.raises(ValueError, match="filename"):
        companion.fetch_companion(value, tmp_path)


@pytest.mark.parametrize("smoke_success", [True, False])
def test_explicit_update_pins_only_a_tested_new_recipe_then_reuses_candidate(
    tmp_path, monkeypatch, manifest, smoke_success
):
    value, _ = manifest
    value["recipe"]["cryptography"]["version"] = "50.0.3"
    recipe = value["recipe"]
    output = tmp_path / "candidate"
    data = wheel_bytes(recipe)
    url = (
        companion.RELEASE_URL
        + "native-cryptography-50-0-3/"
        + companion.wheel_name(recipe)
    )
    for name in ("openssl", "cryptography"):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            member = tarfile.TarInfo(f"{name}-{recipe[name]['version']}")
            member.type = tarfile.DIRTYPE
            bundle.addfile(member)
        recipe[name]["sha256"] = hashlib.sha256(archive.getvalue()).hexdigest()
        recipe[name]["fixture"] = archive.getvalue()
    sources = {
        recipe[name]["url"]: recipe[name].pop("fixture")
        for name in ("openssl", "cryptography")
    }
    monkeypatch.setattr(companion.sys, "platform", "darwin")
    monkeypatch.setattr(
        companion.urllib.request, "urlopen", lambda url, **_: io.BytesIO(sources[url])
    )
    monkeypatch.setattr(
        companion.subprocess, "check_output", lambda *_, **__: "uv 0.12.8 (test)"
    )

    def run(command, **kwargs):
        if command[:2] == ["uv", "build"]:
            assert kwargs["env"]["OPENSSL_STATIC"] == "1"
            assert kwargs["env"]["CARGO_BUILD_TARGET"] == "x86_64-apple-darwin"
            (output / companion.wheel_name(recipe)).write_bytes(data)
        if "-I" in command and not smoke_success:
            raise subprocess.CalledProcessError(1, command)

    command = Mock(side_effect=run)
    monkeypatch.setattr(companion.subprocess, "run", command)
    if not smoke_success:
        with pytest.raises(subprocess.CalledProcessError):
            companion.build_companion(value, output, url)
        assert not (output / "intel-companion.json").exists()
        with pytest.raises(ValueError, match="Unrecorded wheel"):
            companion.build_companion(value, output, url)
        return
    wheel = companion.build_companion(value, output, url)
    assert wheel.read_bytes() == data
    saved = companion.load_manifest(output / "intel-companion.json")
    companion.verify_wheel(wheel, saved)
    calls = command.call_count
    commands = [call.args[0] for call in command.call_args_list]
    assert sum(cmd[:2] == ["uv", "build"] for cmd in commands) == 1
    assert any(cmd[:3] == ["uv", "pip", "install"] for cmd in commands)
    assert any("-I" in cmd and recipe["openssl"]["version"] in cmd for cmd in commands)
    assert companion.build_companion(value, output, url) == wheel
    assert command.call_count == calls


def test_core_version_changes_republish_identical_companion_bytes(
    tmp_path, monkeypatch, manifest
):
    value, data = manifest
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "build_launcher", ROOT / "scripts/build-launcher.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / value["artifact"]["filename"]
    wheel.write_bytes(data)
    for version in ("0.6.3", "0.6.4"):
        source = tmp_path / version
        (source / "src").mkdir(parents=True)
        (source / "packaging/launcher").mkdir(parents=True)
        (source / "scripts/ci").mkdir(parents=True)
        (source / "Cargo.toml").write_text(f'[package]\nversion = "{version}"\n')
        for name in (
            "Cargo.lock",
            "uv-release-manifest.txt",
            "companion-wheels.txt",
            "LICENSE",
        ):
            (source / name).write_text("")
        (source / "packaging/launcher/NOTICE").write_text("notice")
        (source / "scripts/ci/intel-companion.json").write_text(json.dumps(value))
        monkeypatch.setattr(launcher, "ROOT", source)
        args = SimpleNamespace(
            stage=source / "build",
            output=source / "dist",
            target=value["recipe"]["target"],
            native_wheelhouse=wheelhouse,
        )
        launcher.prepare(args)
        native = (
            args.output
            / "wheels"
            / f"toolframe-{version}-py3-none-macosx_12_0_x86_64.whl"
        )
        with zipfile.ZipFile(native, "w") as bundle:
            bundle.writestr(
                f"toolframe-{version}.data/scripts/toolframe", b"native-command"
            )
        launcher.finish(args)
        release = args.output / "release"
        assert (release / wheel.name).read_bytes() == data
        report = json.loads((release / f"manifest-{args.target}.json").read_text())
        assert report["companions"] == [
            {
                "filename": wheel.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "url": companion.RELEASE_URL + f"v{version}/{wheel.name}",
            }
        ]
        with tarfile.open(next(release.glob("*.tar.gz"))) as archive:
            assert archive.extractfile("toolframe").read() == b"native-command"
        assert report["binary_sha256"] == hashlib.sha256(b"native-command").hexdigest()
