"""Real worker propagation, OTLP HTTP export and immutable profile transitions."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import tomlkit
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from test_host import fixture_source, operate

from toolframe_core.api import create_app
from toolframe_core.config import ConfigFile, atomic_write
from toolframe_core.host import Host


@pytest.fixture
def collector(monkeypatch):
    for key in os.environ:
        if key.startswith("OTEL_") or key == "TOOLFRAME_TRACING_ENABLED":
            monkeypatch.delenv(key)
    exports = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["authorization"] == "Bearer exporter-secret"
            body = self.rfile.read(int(self.headers["content-length"]))
            exports.append(ExportTraceServiceRequest.FromString(body))
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://127.0.0.1:{server.server_port}/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=Bearer exporter-secret"
    )
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "50")
    try:
        yield exports
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def tracing_host(home):
    if not (home / "toolframe.toml").exists():
        config = ConfigFile(home)
        composition = config.initialize()
        composition.packages.find_links = [
            str(Path(__file__).resolve().parents[2] / "toolframe-sdk/dist")
        ]
        composition.runtime.health_interval = 60
        atomic_write(config.path, tomlkit.dumps(composition.model_dump()))
    host = await Host.create(home)
    await host.start()
    return host


async def installed_tracing(host):
    python = host.home / host.runtimes["alpha"].candidate["environment"]["python"]
    process = await asyncio.create_subprocess_exec(
        str(python),
        "-I",
        "-c",
        "import importlib.metadata as m; print('opentelemetry-sdk' in {d.metadata['Name'] for d in m.distributions()})",
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0
    return output.strip() == b"True"


async def test_http_nested_workers_export_and_switch_profiles(
    tmp_path, monkeypatch, collector
):
    home = tmp_path / "app"
    host = await tracing_host(home)
    try:
        for name, dependency in (("alpha", None), ("consumer", "alpha")):
            source = fixture_source(tmp_path, name, dependency=dependency)
            module = source / "fixture/__init__.py"
            module.write_text(
                module.read_text()
                .replace("read_only=True", "read_only=True, public_api=True")
                .replace(
                    "return {'version': 'old'}",
                    "\n        if params.get('fail'): raise RuntimeError('payload-secret')\n        return {'version': 'old'}",
                )
            )
            result = await operate(host, "add", source=str(source), enable=True)
            assert result["status"] == "succeeded", result
        base = host.runtimes["alpha"].candidate["environment"]["id"]
        assert not await installed_tracing(host)
        assert await host.call_service("consumer", "consumer.service", "echo", {})
    finally:
        await host.close()
    assert collector == []

    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    host = await tracing_host(home)
    trace_id, parent_id = "a" * 32, "b" * 16
    try:
        traced = host.runtimes["alpha"].candidate["environment"]["id"]
        assert traced != base and await installed_tracing(host)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(host, manage_lifespan=False)),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.post(
                "/api/consumer/consumer.service/echo",
                json={"secret": "payload-secret"},
                headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
            )
            assert response.status_code == 200, response.text
            failure = await client.post(
                "/api/consumer/consumer.service/echo", json={"fail": True}
            )
            assert failure.status_code == 400
            assert failure.json()["error"]["code"] == "handler_failed"
    finally:
        await host.close()

    spans = []
    for export in collector:
        for resource in export.resource_spans:
            attributes = {
                item.key: item.value.string_value
                for item in resource.resource.attributes
            }
            for scope in resource.scope_spans:
                for span in scope.spans:
                    spans.append((attributes, span))
    chain = [
        (resource, span) for resource, span in spans if span.trace_id.hex() == trace_id
    ]
    assert (
        len(chain) == 7
    )  # HTTP + client/server for Core -> consumer -> Core -> alpha.
    assert {r["service.name"] for r, _ in chain} == {
        "toolframe.core",
        "toolframe.plugin.consumer",
        "toolframe.plugin.alpha",
    }
    ids = {span.span_id.hex() for _, span in chain}
    roots = [span for _, span in chain if span.parent_span_id.hex() == parent_id]
    assert len(roots) == 1
    assert roots[0].name == "POST /api/{owner}/{service}/{method}"
    assert all(span.parent_span_id.hex() in ids | {parent_id} for _, span in chain)
    assert sum(span.status.code == 2 for _, span in spans) >= 6
    serialized = json.dumps([MessageToDict(export) for export in collector])
    assert "payload-secret" not in serialized
    assert "exporter-secret" not in serialized
    for resource, _ in chain:
        assert resource["toolframe.application.id"]
        if resource["service.name"] != "toolframe.core":
            assert resource["toolframe.plugin.generation"]

    count = len(collector)
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "false")
    host = await tracing_host(home)
    try:
        assert host.runtimes["alpha"].candidate["environment"]["id"] == base
        assert not await installed_tracing(host)
        assert await host.call_service("consumer", "consumer.service", "echo", {})
    finally:
        await host.close()
    assert len(collector) == count
