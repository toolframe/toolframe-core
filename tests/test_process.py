import signal

import pytest
from toolframe_sdk import PluginError

from toolframe_core.process import child_environment, group_exists, stop_group


def test_core_database_credentials_are_not_inherited_by_workers(monkeypatch):
    monkeypatch.setenv("TOOLFRAME_CORE_DATABASE_URL", "private-core-dsn")
    monkeypatch.setenv("TOOLFRAME_CORE_SETUP_DSN", "private-admin-dsn")
    monkeypatch.setenv("TOOLFRAME_POSTGRES_DSN", "plugin-dsn")
    environment = child_environment()
    assert not any(key.startswith("TOOLFRAME_CORE_") for key in environment)
    assert environment["TOOLFRAME_POSTGRES_DSN"] == "plugin-dsn"


def test_build_and_preflight_exclude_all_telemetry_configuration(monkeypatch):
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=secret")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private=value")
    monkeypatch.setenv("OTEL_UNRECOGNIZED_FUTURE_OPTION", "private")
    environment = child_environment()
    assert "TOOLFRAME_TRACING_ENABLED" not in environment
    assert not any(key.startswith("OTEL_") for key in environment)


@pytest.mark.parametrize("disappears", [True, False])
async def test_force_stop_requires_absence_after_permission_error(
    monkeypatch, disappears
):
    probes = 0
    signals = []

    def killpg(pid, sig):
        nonlocal probes
        assert pid == 12345
        if sig == 0:
            probes += 1
            if disappears and probes >= 5:
                raise ProcessLookupError()
        else:
            signals.append(sig)
        raise PermissionError()

    monkeypatch.setattr("toolframe_core.process.os.killpg", killpg)
    assert group_exists(12345)
    if disappears:
        await stop_group(12345)
        assert probes >= 5
        assert signals == [signal.SIGTERM]
    else:
        with pytest.raises(PluginError) as failure:
            await stop_group(12345)
        assert failure.value.code == "cleanup_failed"
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert group_exists(12345)
