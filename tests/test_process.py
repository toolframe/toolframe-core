import signal

import pytest
from toolframe_sdk import PluginError

from toolframe_core.process import group_exists, stop_group


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
