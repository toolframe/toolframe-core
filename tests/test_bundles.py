"""Release wheels participate in plugin resolution without changing composition."""

from shutil import copy2

import pytest
from test_host import fixture_source

from toolframe_core.config import PackageSettings
from toolframe_core.packages import Packages


@pytest.mark.parametrize("file_uri", [False, True])
async def test_bundled_index_source_and_explicit_same_version_override(
    tmp_path, monkeypatch, file_uri
):
    source = fixture_source(tmp_path, "bundle-test")
    builder = Packages(tmp_path / "builder", PackageSettings())
    original = await builder.capture(str(source))
    original_wheel = builder.home / original["artifact"]
    bundled = tmp_path / "embedded"
    packaged = bundled / original_wheel.stem
    packaged.mkdir(parents=True)
    copy2(original_wheel, packaged / original_wheel.name)
    monkeypatch.setenv("TOOLFRAME_BUNDLED_WHEELHOUSE", str(bundled))

    # Offline + a fresh application means this can only come from the bundle.
    request = "pypi:fixture-bundle-test==0.1.0"
    settings = PackageSettings(offline=True)
    consumer = Packages(tmp_path / "consumer", settings)
    captured = await consumer.capture(request)
    assert captured["artifact_hash"] == original["artifact_hash"]
    assert settings.find_links == []

    fixture_source(tmp_path, "bundle-test", version="customized")
    replacement = await builder.capture(str(source))
    assert replacement["artifact_hash"] != original["artifact_hash"]
    custom = tmp_path / "wheelhouse"
    custom.mkdir()
    copy2(builder.home / replacement["artifact"], custom / original_wheel.name)
    link = custom.as_uri() if file_uri else str(custom)
    overrides = Packages(
        tmp_path / "overrides", PackageSettings(find_links=[link], offline=True)
    )
    assert str(packaged) not in overrides.flags()
    captured = await overrides.capture(request)
    assert captured["artifact_hash"] == replacement["artifact_hash"]
    assert (packaged / original_wheel.name).read_bytes() == original_wheel.read_bytes()


def test_other_bundled_dependencies_remain_when_one_wheel_is_overridden(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "embedded"
    for name in ("first", "second"):
        directory = bundled / name
        directory.mkdir(parents=True)
        (directory / f"{name}.whl").write_bytes(b"wheel")
    custom = tmp_path / "app/custom"
    custom.mkdir(parents=True)
    (custom / "first.whl").write_bytes(b"override")
    monkeypatch.setenv("TOOLFRAME_BUNDLED_WHEELHOUSE", str(bundled))
    package = Packages(tmp_path / "app", PackageSettings(find_links=["custom"]))
    assert package.bundled_links() == [str(bundled / "second")]
    assert str(custom) in package.flags()
    monkeypatch.delenv("TOOLFRAME_BUNDLED_WHEELHOUSE")
    assert package.bundled_links() == []
