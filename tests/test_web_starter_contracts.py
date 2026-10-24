import importlib
import sys

import pytest
from jsonschema import ValidationError
from toolframe_sdk.worker import Worker

from toolframe_core.scaffold import new_plugin


@pytest.fixture
def starter(tmp_path, monkeypatch):
    result = new_plugin(tmp_path, "contract-notes", presentation="web")
    monkeypatch.syspath_prepend(result["path"] + "/src")
    module = "toolframe_contract_notes"
    try:
        yield importlib.import_module(module + ".plugin").plugin
    finally:
        for name in tuple(sys.modules):
            if name == module or name.startswith(module + "."):
                del sys.modules[name]


def note():
    return {
        "id": "a" * 32,
        "body": "Remember",
        "revision": 1,
        "created_at": "2026-10-24T12:00:00+00:00",
    }


class Service:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def invoke(self, service, method, params):
        self.calls.append((service, method, params))
        return self.result


def worker_for(starter, result):
    worker = Worker("toolframe_contract_notes.plugin:plugin")
    worker.definition = starter
    worker.plugin = Service(result)
    return worker


@pytest.mark.parametrize(
    "method,payload,result",
    [
        ("list", {}, {"notes": [note()]}),
        ("create", {"body": "Remember", "request_id": "r" * 16}, note()),
        ("update", {"id": "a" * 32, "body": "Remember", "revision": 1}, note()),
    ],
)
async def test_generated_service_is_described_and_validated_by_worker(
    starter, method, payload, result
):
    worker = worker_for(starter, result)
    service = worker.describe()["definition"]["services"]["contract-notes.notes"]
    assert service["methods"][method]["description"]
    assert (
        await worker.handle(
            "runtime.invoke",
            {"service": "contract-notes.notes", "method": method, "params": payload},
        )
        == result
    )
    assert worker.plugin.calls


@pytest.mark.parametrize(
    "method,payload",
    [
        ("list", {"unexpected": True}),
        ("create", {"body": "Remember"}),
        ("create", {"body": 12, "request_id": "r" * 16}),
        ("create", {"body": "x" * 1001, "request_id": "r" * 16}),
        ("create", {"body": "   ", "request_id": "r" * 16}),
        ("create", {"body": "Remember", "request_id": "short"}),
        ("create", {"body": "Remember", "request_id": "r" * 65}),
        ("update", {"id": "a" * 32, "body": "Remember", "revision": True}),
        ("update", {"id": "a" * 32, "body": "Remember", "revision": 0}),
        ("update", {"id": "a" * 32, "body": "Remember", "revision": "1"}),
        ("update", {"id": "not-an-id", "body": "Remember", "revision": 1}),
        (
            "update",
            {"id": "a" * 32, "body": "Remember", "revision": 1, "owner": "other"},
        ),
    ],
)
async def test_invalid_input_is_rejected_before_business_code(starter, method, payload):
    worker = worker_for(starter, note())
    with pytest.raises(ValidationError):
        await worker.handle(
            "runtime.invoke",
            {"service": "contract-notes.notes", "method": method, "params": payload},
        )
    assert not worker.plugin.calls


@pytest.mark.parametrize(
    "result",
    [
        {},
        {**note(), "revision": "1"},
        {**note(), "private": "secret"},
        {**note(), "body": "x" * 1001},
        {**note(), "id": "invalid"},
    ],
)
async def test_invalid_service_output_is_rejected_by_worker(starter, result):
    worker = worker_for(starter, result)
    with pytest.raises(ValidationError):
        await worker.handle(
            "runtime.invoke",
            {
                "service": "contract-notes.notes",
                "method": "create",
                "params": {"body": "Remember", "request_id": "r" * 16},
            },
        )
    assert len(worker.plugin.calls) == 1
