import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import anyio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import lite_mcp_dashboard_runner as runner
import lite_mcp_runtime as runtime
import server as runtime_server


class ServerEntrypointTests(unittest.TestCase):
    def test_uses_one_inherited_listener_and_25_mib_websocket_cap(self):
        fake_app = object()
        with (
            patch.dict(os.environ, {"LITE_MCP_LISTEN_FD": "17"}),
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
        dashboard = Path("/tmp/lite-mcp-dashboard")

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
    def test_emits_one_sentinel_then_signals_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory)
            widget = dashboard / "widgets" / "summary.py"
            widget.parent.mkdir()
            namespace = {"__name__": "lite_mcp_dashboard_test"}
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
                patch("lite_mcp_runtime.os.kill") as kill,
                patch("lite_mcp_runtime.os.getpid", return_value=4321),
                redirect_stderr(stderr),
            ):
                self.assertTrue(reporter(failure))
                self.assertTrue(reporter(ValueError("second")))

        sentinels = [
            line
            for line in stderr.getvalue().splitlines()
            if line.startswith("LITE_MCP_FATAL ")
        ]
        self.assertEqual(len(sentinels), 1)
        payload = json.loads(sentinels[0].removeprefix("LITE_MCP_FATAL "))
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

        with patch("lite_mcp_runtime.os.kill") as kill:
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
                    patch.dict(os.environ, {"LITE_MCP_DASHBOARD_DIR": str(dashboard)}),
                    patch("lite_mcp_runtime.os.kill") as kill,
                    redirect_stderr(stderr),
                    self.assertRaises(SyntaxError) as raised,
                ):
                    runner.main()
            finally:
                runtime._DASHBOARD_FATAL_REPORTER = previous_reporter

        self.assertEqual(Path(raised.exception.filename), app)
        self.assertIn("LITE_MCP_FATAL ", stderr.getvalue())
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
                patch.dict(os.environ, {"LITE_MCP_DASHBOARD_DIR": str(dashboard)}),
                self.assertRaises(StopException),
            ):
                runner.main()


if __name__ == "__main__":
    unittest.main()
