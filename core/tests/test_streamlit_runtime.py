import io
import json
import os
import runpy
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import toolframe_dashboard_runner as runner
import toolframe_runtime as runtime
import server as runtime_server


class DefaultDashboardIdentityTests(unittest.TestCase):
    def test_fresh_dashboard_is_visibly_unnamed(self):
        calls = []
        fake_streamlit = SimpleNamespace(
            set_page_config=lambda **kwargs: calls.append(("page_config", kwargs)),
            title=lambda value: calls.append(("title", value)),
            caption=lambda value: calls.append(("caption", value)),
            warning=lambda value: calls.append(("warning", value)),
            html=lambda value, **kwargs: calls.append(("html", (value, kwargs))),
        )
        fake_dashboard = SimpleNamespace(
            render_widgets=lambda: calls.append(("render_widgets", None))
        )

        with patch.dict(
            sys.modules,
            {
                "streamlit": fake_streamlit,
                "toolframe_dashboard": fake_dashboard,
            },
        ):
            runpy.run_path(str(REPOSITORY_ROOT / "dashboard" / "app.py"))

        page_config = next(value for kind, value in calls if kind == "page_config")
        self.assertEqual(page_config["page_title"], "Unnamed")
        self.assertIn(("title", "Unnamed"), calls)
        self.assertTrue(
            any(
                kind == "warning" and "app" in value.lower() and "name" in value.lower()
                for kind, value in calls
            ),
            "the fresh dashboard must make the missing app name visible",
        )
        warning_html, warning_options = next(
            value for kind, value in calls if kind == "html"
        )
        self.assertIn("This dashboard has no authentication", warning_html)
        self.assertIn("Don't show again", warning_html)
        self.assertIn("window.localStorage.getItem", warning_html)
        self.assertIn("window.localStorage.setItem", warning_html)
        self.assertEqual(warning_options, {"unsafe_allow_javascript": True})
        self.assertIn(("render_widgets", None), calls)


class ServerEntrypointTests(unittest.TestCase):
    def test_uses_one_inherited_listener_and_25_mib_websocket_cap(self):
        fake_app = object()
        with (
            patch.dict(os.environ, {"TOOLFRAME_LISTEN_FD": "17"}),
            patch.object(runtime_server, "build_application", return_value=fake_app),
            patch.object(runtime_server.uvicorn, "run") as run,
        ):
            runtime_server.main()

        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args, (fake_app,))
        self.assertEqual(kwargs["fd"], 17)
        self.assertEqual(kwargs["workers"], 1)
        self.assertEqual(kwargs["ws_max_size"], 25 * 1024 * 1024)
        self.assertIsNone(kwargs["log_config"])
        self.assertFalse(kwargs["access_log"])

    def test_diagnostics_replace_third_party_rich_handlers(self):
        fastmcp_logger = runtime_server.logging.getLogger("fastmcp")
        child_logger = runtime_server.logging.getLogger("fastmcp.server.server")
        fastmcp_logger.addHandler(runtime_server.logging.NullHandler())
        child_logger.addHandler(runtime_server.logging.NullHandler())
        fastmcp_logger.propagate = False

        runtime_server._configure_diagnostics()

        self.assertEqual(fastmcp_logger.handlers, [])
        self.assertTrue(fastmcp_logger.propagate)
        self.assertEqual(fastmcp_logger.level, runtime_server.logging.NOTSET)
        self.assertEqual(child_logger.handlers, [])
        self.assertTrue(child_logger.propagate)


class FakeAuditSink:
    def __init__(self, failure=None):
        self.events = []
        self.failure = failure

    def emit(self, channel, event, level="INFO", payload=None, **fields):
        if self.failure is not None:
            raise self.failure
        record = {
            "channel": channel,
            "event": event,
            "level": level,
            "payload": payload,
            **fields,
        }
        self.events.append(record)
        return record


class FakeFatalReporter:
    def __init__(self):
        self.failures = []

    def logging_failure(self, error):
        self.failures.append(error)
        return True


class McpAuditTests(unittest.TestCase):
    def test_component_results_use_public_mcp_models_for_audit_serialization(self):
        def echo(value: str) -> str:
            """Return the supplied value."""

            return value

        component = runtime.Tool.from_function(echo)
        sink = FakeAuditSink()
        middleware = runtime.McpAuditMiddleware(sink, FakeFatalReporter())
        context = SimpleNamespace(
            method="tools/list",
            type="request",
            source="client",
            message={"method": "tools/list"},
            fastmcp_context=None,
        )

        async def call_next(_):
            return [component]

        result = anyio.run(middleware.on_message, context, call_next)

        self.assertEqual(result, [component])
        self.assertEqual(
            [event["event"] for event in sink.events],
            ["mcp_message_received", "mcp_message_completed"],
        )
        normalized = sink.events[-1]["payload"]["result"]
        self.assertEqual(normalized[0]["name"], "echo")
        self.assertNotIn("fn", normalized[0])
        self.assertEqual(sink.events[0]["direction"], "client_to_server")
        self.assertEqual(sink.events[-1]["direction"], "server_to_client")
        json.dumps(normalized)

    def test_transport_records_socket_peer_and_selected_headers(self):
        sink = FakeAuditSink()
        fatal = FakeFatalReporter()

        async def downstream(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = runtime.McpTransportMiddleware(downstream, sink, fatal)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            sent.append(message)

        async def invoke():
            await middleware(
                {
                    "type": "http",
                    "path": "/mcp",
                    "method": "POST",
                    "client": ("192.168.1.55", 54321),
                    "headers": [
                        (b"user-agent", b"test-client/1"),
                        (b"x-forwarded-for", b"203.0.113.5"),
                        (b"mcp-session-id", b"wire-session"),
                    ],
                },
                receive,
                send,
            )

        anyio.run(invoke)
        event = sink.events[0]
        self.assertEqual(event["channel"], "mcp")
        self.assertEqual(event["sourceIp"], "192.168.1.55")
        self.assertEqual(event["sourcePort"], 54321)
        self.assertEqual(event["sessionId"], "wire-session")
        self.assertEqual(event["userAgent"], "test-client/1")
        self.assertEqual(event["httpMethod"], "POST")
        self.assertEqual(event["path"], "/mcp")
        self.assertEqual(event["statusCode"], 200)
        self.assertNotIn("203.0.113.5", json.dumps(event))

    def test_transport_correlates_response_session_client_and_wire_message(self):
        sink = FakeAuditSink()
        fatal = FakeFatalReporter()

        async def downstream(scope, receive, send):
            await receive()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"mcp-session-id", b"new-session"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'event: message\ndata: {"jsonrpc":"2.0","id":"init-1","result":{}}\n\n',
                }
            )

        middleware = runtime.McpTransportMiddleware(downstream, sink, fatal)
        request_body = (
            b'{"jsonrpc":"2.0","id":"init-1","method":"initialize",'
            b'"params":{"clientInfo":{"name":"agent","version":"1.2"}}}'
        )
        observed_before_send = []

        async def invoke():
            async def receive():
                return {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }

            async def send(message):
                if message["type"] == "http.response.body":
                    observed_before_send.append(sink.events[-1]["event"])

            await middleware(
                {
                    "type": "http",
                    "path": "/mcp",
                    "method": "POST",
                    "client": ("192.168.1.77", 50000),
                    "headers": [],
                },
                receive,
                send,
            )

        anyio.run(invoke)
        wire, transport = sink.events
        self.assertEqual(observed_before_send, ["mcp_wire_message"])
        self.assertEqual(wire["event"], "mcp_wire_message")
        self.assertEqual(wire["direction"], "server_to_client")
        self.assertEqual(wire["protocolRequestId"], "init-1")
        self.assertEqual(wire["sessionId"], "new-session")
        self.assertEqual(wire["client"]["name"], "agent")
        self.assertEqual(wire["client"]["version"], "1.2")
        self.assertEqual(wire["sourceIp"], "192.168.1.77")
        self.assertEqual(transport["event"], "mcp_transport_completed")
        self.assertEqual(transport["sessionId"], "new-session")
        self.assertEqual(transport["client"]["name"], "agent")

    def test_transport_routes_malformed_request_to_audit_only(self):
        sink = FakeAuditSink()
        fatal = FakeFatalReporter()

        async def downstream(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"bad"})

        middleware = runtime.McpTransportMiddleware(downstream, sink, fatal)

        async def invoke():
            async def receive():
                return {
                    "type": "http.request",
                    "body": b"not-json",
                    "more_body": False,
                }

            async def send(_):
                pass

            await middleware(
                {
                    "type": "http",
                    "path": "/mcp",
                    "method": "POST",
                    "headers": [],
                },
                receive,
                send,
            )

        anyio.run(invoke)
        self.assertEqual(len(sink.events), 1)
        self.assertEqual(sink.events[0]["channel"], "audit")
        self.assertEqual(sink.events[0]["outcome"], "rejected")
        body = sink.events[0]["payload"]["requestBody"]
        self.assertEqual(body["preview"], "not-json")
        self.assertEqual(body["originalBytes"], 8)
        self.assertFalse(body["truncated"])

    def test_wire_log_failure_prevents_response_body_send_and_reports_fatal(self):
        error = runtime.StructuredLogError("disk full")
        sink = FakeAuditSink(error)
        fatal = FakeFatalReporter()
        sent = []

        async def downstream(scope, receive, send):
            await receive()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"jsonrpc":"2.0","id":1,"result":{}}',
                }
            )

        middleware = runtime.McpTransportMiddleware(downstream, sink, fatal)

        async def invoke():
            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                sent.append(message["type"])

            await middleware(
                {
                    "type": "http",
                    "path": "/mcp",
                    "method": "POST",
                    "headers": [],
                },
                receive,
                send,
            )

        with self.assertRaises(runtime.StructuredLogError):
            anyio.run(invoke)
        self.assertEqual(sent, ["http.response.start"])
        self.assertEqual(fatal.failures, [error])

    def test_transport_capture_is_bounded_for_large_malformed_body(self):
        capture = runtime._BoundedBodyCapture()
        capture.add(b"a" * (1024 * 1024))

        payload = capture.payload()

        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["originalBytes"], 1024 * 1024)
        self.assertLess(len(payload["preview"]), 17 * 1024)

    def test_fastmcp_message_taxonomy_and_final_tool_error(self):
        sink = FakeAuditSink()
        fatal = FakeFatalReporter()
        middleware = runtime.McpAuditMiddleware(sink, fatal)
        context = SimpleNamespace(
            method="tools/call",
            type="request",
            source="client",
            message=SimpleNamespace(name="echo", arguments={"value": "x"}),
            fastmcp_context=None,
        )

        async def call_next(_):
            return {
                "structuredContent": {
                    "error": {"code": "tool_timeout", "retryable": True}
                }
            }

        result = anyio.run(middleware.on_message, context, call_next)
        self.assertIn("structuredContent", result)
        self.assertEqual(
            [event["event"] for event in sink.events],
            ["mcp_message_started", "mcp_message_completed"],
        )
        self.assertEqual(sink.events[-1]["outcome"], "timeout")
        self.assertTrue(sink.events[-1]["retryable"])
        self.assertEqual(sink.events[-1]["tool"], "echo")
        self.assertEqual(sink.events[0]["direction"], "client_to_server")
        self.assertEqual(sink.events[-1]["direction"], "server_to_client")

    def test_is_error_result_is_failed_without_structured_error(self):
        outcome, retryable = runtime._mcp_result_outcome({"isError": True})

        self.assertEqual(outcome, "failed")
        self.assertIsNone(retryable)

    def test_initialize_uses_message_client_info_before_session_commit(self):
        client_info = SimpleNamespace(name="agent-client", version="2.0")
        context = SimpleNamespace(
            method="initialize",
            type="request",
            source="client",
            message=SimpleNamespace(params=SimpleNamespace(client_info=client_info)),
            fastmcp_context=None,
        )

        fields = runtime._mcp_context_fields(context, "operation")

        self.assertEqual(fields["client"]["name"], "agent-client")
        self.assertEqual(fields["client"]["version"], "2.0")
        self.assertFalse(fields["client"]["verified"])

    def test_notification_emits_one_event(self):
        sink = FakeAuditSink()
        middleware = runtime.McpAuditMiddleware(sink, FakeFatalReporter())
        context = SimpleNamespace(
            method="notifications/initialized",
            type="notification",
            source="client",
            message={"method": "notifications/initialized"},
            fastmcp_context=None,
        )

        async def call_next(_):
            return None

        anyio.run(middleware.on_message, context, call_next)
        self.assertEqual(
            [event["event"] for event in sink.events], ["mcp_notification"]
        )

    def test_structured_writer_failure_reports_fatal_without_recursing(self):
        error = runtime.StructuredLogError("disk full")
        fatal = FakeFatalReporter()
        middleware = runtime.McpAuditMiddleware(FakeAuditSink(error), fatal)
        context = SimpleNamespace(
            method="tools/list",
            type="request",
            source="client",
            message={},
            fastmcp_context=None,
        )

        async def call_next(_):
            return []

        with self.assertRaises(runtime.StructuredLogError):
            anyio.run(middleware.on_message, context, call_next)
        self.assertEqual(fatal.failures, [error])


class HostOriginMiddlewareTests(unittest.TestCase):
    def test_preserves_http_rejection_statuses(self):
        async def downstream(scope, receive, send):
            self.fail("rejected HTTP request reached the downstream app")

        middleware = runtime.HostOriginMiddleware(
            downstream,
            allowed_hosts=["localhost"],
            allowed_origins=["http://localhost:8000"],
        )
        cases = [
            ([(b"host", b"attacker.invalid")], 421),
            (
                [
                    (b"host", b"localhost:8000"),
                    (b"origin", b"http://attacker.invalid"),
                ],
                403,
            ),
        ]

        for headers, expected_status in cases:
            sent = []

            async def capture(message, sink=sent):
                sink.append(message)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def invoke(request_headers=headers):
                await middleware(
                    {"type": "http", "method": "GET", "headers": request_headers},
                    receive,
                    capture,
                )

            anyio.run(invoke)
            self.assertEqual(sent[0]["type"], "http.response.start")
            self.assertEqual(sent[0]["status"], expected_status)

    def test_rejects_websocket_host_with_policy_close(self):
        downstream_calls = []
        sent = []

        async def downstream(scope, receive, send):
            downstream_calls.append(scope)

        middleware = runtime.HostOriginMiddleware(
            downstream,
            allowed_hosts=["localhost"],
            allowed_origins=["http://localhost:8000"],
        )
        scope = {
            "type": "websocket",
            "headers": [
                (b"host", b"attacker.invalid"),
                (b"origin", b"http://localhost:8000"),
            ],
        }

        async def capture(message):
            sent.append(message)

        async def invoke():
            await middleware(scope, None, capture)

        anyio.run(invoke)

        self.assertEqual(downstream_calls, [])
        self.assertEqual(
            sent,
            [
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "Unrecognized Host header",
                }
            ],
        )

    def test_rejects_websocket_origin_with_policy_close(self):
        sent = []

        async def downstream(scope, receive, send):
            self.fail("rejected WebSocket reached the downstream app")

        middleware = runtime.HostOriginMiddleware(
            downstream,
            allowed_hosts=["localhost"],
            allowed_origins=["http://localhost:8000"],
        )
        scope = {
            "type": "websocket",
            "headers": [
                (b"host", b"localhost:8000"),
                (b"origin", b"http://attacker.invalid"),
            ],
        }

        async def capture(message):
            sent.append(message)

        async def invoke():
            await middleware(scope, None, capture)

        anyio.run(invoke)

        self.assertEqual(sent[0]["type"], "websocket.close")
        self.assertEqual(sent[0]["code"], 1008)

    def test_allows_valid_websocket(self):
        downstream_calls = []

        async def downstream(scope, receive, send):
            downstream_calls.append(scope)

        middleware = runtime.HostOriginMiddleware(
            downstream,
            allowed_hosts=["localhost"],
            allowed_origins=["http://localhost:8000"],
        )
        scope = {
            "type": "websocket",
            "headers": [
                (b"host", b"localhost:8000"),
                (b"origin", b"http://localhost:8000"),
            ],
        }

        async def invoke():
            await middleware(scope, None, lambda _: None)

        anyio.run(invoke)

        self.assertEqual(downstream_calls, [scope])


class StreamlitConfigurationTests(unittest.TestCase):
    def test_configuration_is_bounded_and_uses_absolute_watch_directory(self):
        dashboard = Path("/tmp/toolframe-dashboard")

        values = runtime._streamlit_config_values(
            dashboard_dir=dashboard,
            allowed_hosts=["localhost", "192.168.1.10"],
            allowed_origins=["http://localhost:8123"],
            port=8123,
        )

        self.assertEqual(values["server.folderWatchList"], [str(dashboard)])
        self.assertFalse(values["server.runOnSave"])
        self.assertTrue(values["server.headless"])
        self.assertFalse(values["browser.gatherUsageStats"])
        self.assertEqual(values["client.toolbarMode"], "minimal")
        self.assertEqual(values["client.showErrorDetails"], "none")
        self.assertTrue(values["server.enableCORS"])
        self.assertTrue(values["server.enableXsrfProtection"])
        self.assertEqual(values["server.maxUploadSize"], 25)
        self.assertEqual(values["server.maxMessageSize"], 25)
        self.assertEqual(values["server.port"], 8123)
        self.assertEqual(values["server.baseUrlPath"], "")


class DashboardFatalReporterTests(unittest.TestCase):
    def test_generic_fatal_reporter_is_injectable_and_first_wins(self):
        killed = []
        reporter = runtime.FatalShutdownReporter(
            killer=lambda process, sig: killed.append((process, sig))
        )
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            self.assertTrue(reporter.report({"phase": "test", "retryable": False}))
            self.assertTrue(reporter.report({"phase": "second"}))

        self.assertTrue(reporter.reported)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertEqual(killed, [(os.getpid(), signal.SIGTERM)])

    def test_emits_one_sentinel_then_signals_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory)
            widget = dashboard / "widgets" / "summary.py"
            widget.parent.mkdir()
            namespace = {"__name__": "toolframe_dashboard_test"}
            try:
                exec(  # noqa: S102 - create a traceback with the widget source path.
                    compile("raise ValueError('broken\\nwidget')", str(widget), "exec"),
                    namespace,
                )
            except ValueError as error:
                failure = error

            reporter = runtime.DashboardFatalReporter(dashboard)
            stderr = io.StringIO()
            with (
                patch("toolframe_runtime.os.kill") as kill,
                patch("toolframe_runtime.os.getpid", return_value=4321),
                redirect_stderr(stderr),
            ):
                self.assertTrue(reporter(failure))
                self.assertTrue(reporter(ValueError("second")))

        sentinels = [
            line
            for line in stderr.getvalue().splitlines()
            if line.startswith("TOOLFRAME_FATAL ")
        ]
        self.assertEqual(len(sentinels), 1)
        payload = json.loads(sentinels[0].removeprefix("TOOLFRAME_FATAL "))
        self.assertEqual(payload["phase"], "widget-execution")
        self.assertEqual(payload["widget"], "summary")
        self.assertEqual(payload["message"], "broken widget")
        self.assertFalse(payload["retryable"])
        kill.assert_called_once_with(4321, signal.SIGTERM)

    def test_streamlit_control_exception_is_never_fatal(self):
        control_base = type(
            "ScriptControlException",
            (BaseException,),
            {"__module__": "streamlit.runtime.test"},
        )
        stop = type(
            "StopException", (control_base,), {"__module__": "streamlit.runtime.test"}
        )
        reporter = runtime.DashboardFatalReporter(Path("/tmp/dashboard"))

        with patch("toolframe_runtime.os.kill") as kill:
            self.assertFalse(reporter(stop()))

        kill.assert_not_called()


class ManagedRunnerTests(unittest.TestCase):
    def test_compile_error_references_user_app(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory)
            app = dashboard / "app.py"
            app.write_text("def broken(:\n", encoding="utf-8")

            previous_reporter = runtime._DASHBOARD_FATAL_REPORTER
            runtime.configure_dashboard_fatal_reporter(dashboard)
            stderr = io.StringIO()
            try:
                with (
                    patch.dict(os.environ, {"TOOLFRAME_DASHBOARD_DIR": str(dashboard)}),
                    patch("toolframe_runtime.os.kill") as kill,
                    redirect_stderr(stderr),
                    self.assertRaises(SyntaxError) as raised,
                ):
                    runner.main()
            finally:
                runtime._DASHBOARD_FATAL_REPORTER = previous_reporter

        self.assertEqual(Path(raised.exception.filename), app)
        self.assertIn("TOOLFRAME_FATAL ", stderr.getvalue())
        kill.assert_called_once()

    def test_streamlit_stop_is_not_wrapped(self):
        try:
            from streamlit.runtime.scriptrunner_utils.exceptions import StopException
        except ImportError:
            self.skipTest("Streamlit is not installed in this test environment")

        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory)
            (dashboard / "app.py").write_text(
                "from streamlit.runtime.scriptrunner_utils.exceptions import StopException\n"
                "raise StopException()\n",
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"TOOLFRAME_DASHBOARD_DIR": str(dashboard)}),
                self.assertRaises(StopException),
            ):
                runner.main()


if __name__ == "__main__":
    unittest.main()
