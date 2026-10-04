import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    PluginDefinition,
    PluginError,
    Requirement,
    RuntimeContext,
    ServiceSpec,
)

from toolframe_core.config import RuntimeSettings
from toolframe_core.graph import Graph
from toolframe_core.host import Host, Runtime


class Leases:
    def __init__(self):
        self.counts = {}

    @contextmanager
    def lease(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        try:
            yield
        finally:
            self.counts[name] -= 1


@pytest.fixture
async def task_host():
    host = Host()
    definitions = {
        "jobs": PluginDefinition(
            id="jobs",
            authorization=AuthorizationSpec(),
            factory="fixture:Jobs",
            requires={"backend": Requirement(service="backend")},
        ),
        "backend": PluginDefinition(
            id="backend",
            authorization=AuthorizationSpec(),
            factory="fixture:Backend",
            services={
                "backend": ServiceSpec(
                    methods={"read": MethodSpec(audience="internal", read_only=True)}
                )
            },
        ),
    }
    candidates = {
        name: {"definition": definition.model_dump(mode="json")}
        for name, definition in definitions.items()
    }
    host.graph = Graph(candidates, {})
    host.composition = SimpleNamespace(
        runtime=RuntimeSettings(), application=SimpleNamespace(id="task-test")
    )
    host.state = SimpleNamespace(failed=False)
    host.access = SimpleNamespace(catalog=dict)
    host.manager = Leases()
    host.runtimes = {
        name: Runtime(
            name,
            "g1",
            candidate,
            worker=SimpleNamespace(process=SimpleNamespace(returncode=None)),
            health={"runtime": "ready"},
            observed=time.time(),
            admitted=True,
        )
        for name, candidate in candidates.items()
    }
    host.gated = set()
    host.roots = {}
    host.failed_roots = set()
    host.cleanup_blocked = set()
    host.changed = asyncio.Event()
    yield host
    for root in list(host.roots.values()):
        await host.end_root(root)


@pytest.mark.parametrize(
    "timeout",
    [
        True,
        False,
        None,
        "10",
        0,
        -1,
        float("nan"),
        float("inf"),
        -float("inf"),
        10**400,
    ],
)
async def test_invalid_worker_budget_does_not_acquire_leases(task_host, timeout):
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(
            task_host.runtimes["jobs"], "task.begin", {"timeout": timeout}
        )
    assert error.value.code == "invalid_task_timeout"
    assert not task_host.roots
    assert not task_host.manager.counts
    # A bad request does not poison this worker's subsequent admission.
    admitted = await task_host.worker_call(
        task_host.runtimes["jobs"], "task.begin", {"timeout": 0.5}
    )
    await task_host.worker_call(task_host.runtimes["jobs"], "task.end", admitted)
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}


async def test_default_and_operator_policy(task_host):
    runtime = task_host.runtimes["jobs"]
    before = time.time()
    admitted = await task_host.worker_call(runtime, "task.begin", {})
    assert before + 30 <= admitted["deadline"] <= time.time() + 30
    assert admitted["chain"] == ["jobs"]
    assert task_host.manager.counts == {"jobs": 1, "backend": 1}
    await task_host.worker_call(runtime, "task.end", admitted)
    task_host.composition.runtime.max_task_timeout = 60
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(runtime, "task.begin", {"timeout": 60.1})
    assert error.value.code == "task_timeout_exceeded"
    assert error.value.details == {"max_task_timeout": 60}
    assert not task_host.roots
    before = time.time()
    admitted = await task_host.worker_call(runtime, "task.begin", {"timeout": 60})
    assert before + 60 <= admitted["deadline"] <= time.time() + 60
    await task_host.worker_call(runtime, "task.end", admitted)
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}


async def test_waiting_for_quiesce_then_resume_or_stale_generation(task_host):
    runtime = task_host.runtimes["jobs"]
    task_host.gated.add("jobs")
    admission = asyncio.create_task(
        task_host.worker_call(runtime, "task.begin", {"timeout": 0.1})
    )
    await asyncio.sleep(0)
    assert not admission.done()
    assert not task_host.roots
    task_host.gated.clear()
    task_host.changed.set()
    admitted = await admission
    await task_host.worker_call(runtime, "task.end", admitted)
    task_host.gated.add("jobs")
    admission = asyncio.create_task(task_host.worker_call(runtime, "task.begin", {}))
    await asyncio.sleep(0)
    replacement = Runtime(
        "jobs",
        "g2",
        runtime.candidate,
        worker=runtime.worker,
        admitted=True,
        observed=time.time(),
        health={"runtime": "ready"},
    )
    task_host.runtimes["jobs"] = replacement
    task_host.gated.clear()
    task_host.changed.set()
    with pytest.raises(PluginError) as error:
        await admission
    assert error.value.code == "stale_generation"
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(runtime, "task.begin", {})
    assert error.value.code == "stale_generation"
    admitted = await task_host.worker_call(replacement, "task.begin", {})
    await task_host.worker_call(replacement, "task.end", admitted)
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}


async def test_cancelled_gate_wait_never_admits(task_host):
    task_host.gated.add("jobs")
    admission = asyncio.create_task(
        task_host.worker_call(task_host.runtimes["jobs"], "task.begin", {})
    )
    await asyncio.sleep(0)
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission
    assert not task_host.roots
    assert not task_host.manager.counts


@pytest.mark.parametrize("cancel", [False, True])
async def test_sdk_deadline_or_cancellation_settles_real_core_roots(task_host, cancel):
    class Peer:
        async def request(self, method, params, **kwargs):
            return await task_host.worker_call(
                task_host.runtimes["jobs"], method, params
            )

    context = RuntimeContext(Peer(), plugin_id="jobs", generation="g1")
    context.ready.set()
    running = asyncio.Event()

    async def attempt():
        assert context.call_info["root_id"] in task_host.roots
        running.set()
        await asyncio.Event().wait()

    task = context.task(attempt, timeout=0.02 if not cancel else 1)
    await running.wait()
    assert task_host.manager.counts == {"jobs": 1, "backend": 1}
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}
    assert not context.call_info
    assert not context.tasks
    assert (
        await context.task(lambda: asyncio.sleep(0, result="next"), timeout=0.1)
        == "next"
    )
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}


@pytest.mark.parametrize("value", [True, 29.9, 86400.1, float("nan"), float("inf")])
def test_operator_timeout_policy_validation(value):
    with pytest.raises(ValidationError):
        RuntimeSettings(max_task_timeout=value)


def test_operator_timeout_policy_defaults_and_boundaries():
    assert RuntimeSettings().max_task_timeout == 3600
    assert RuntimeSettings(max_task_timeout=30).max_task_timeout == 30
    assert RuntimeSettings(max_task_timeout=86400).max_task_timeout == 86400


async def test_withdrawn_generation_can_settle_only_its_admitted_root(task_host):
    old = task_host.runtimes["jobs"]
    admitted = await task_host.worker_call(old, "task.begin", {"timeout": 1})
    replacement = Runtime(
        "jobs",
        "g2",
        old.candidate,
        worker=old.worker,
        admitted=True,
        observed=time.time(),
        health={"runtime": "ready"},
    )
    task_host.runtimes["jobs"] = replacement
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(replacement, "task.end", admitted)
    assert error.value.code == "stale_generation"
    assert admitted["root_id"] in task_host.roots
    await task_host.worker_call(old, "task.end", admitted)
    # Cleanup acknowledgement is idempotent after withdrawal.
    await task_host.worker_call(old, "task.end", admitted)
    await task_host._drain({"jobs", "backend"}, 0.1)
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}
    new = await task_host.worker_call(replacement, "task.begin", {"timeout": 1})
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(old, "task.end", new)
    assert error.value.code == "stale_generation"
    with pytest.raises(PluginError) as error:
        await task_host.worker_call(old, "task.begin", {})
    assert error.value.code == "stale_generation"
    await task_host.worker_call(replacement, "task.end", new)
    assert not task_host.roots
    assert task_host.manager.counts == {"jobs": 0, "backend": 0}
