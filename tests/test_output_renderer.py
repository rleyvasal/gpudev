import contextlib
import io
import socket
import time
import unittest
from unittest import mock

with mock.patch.object(socket, "socket") as socket_factory:
    socket_factory.return_value.__enter__.return_value.bind.return_value = None
    from gpudev_craft import core


class FakeDisplayPublisher:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)


class FakeIPython:
    def __init__(self):
        self.display_pub = FakeDisplayPublisher()


class FakeRemoteKernelClient:
    def __init__(self):
        self.last_result = None

    def execute_interactive(self, code, output_hook):
        renderer = output_hook.__self__
        with renderer._lock:
            renderer._publish_running_locked()
        output_hook(
            {
                "msg_type": "stream",
                "content": {"name": "stdout", "text": "gpudev\r\n"},
            }
        )
        return {"content": {"status": "ok"}}


class HybridOutputRendererTests(unittest.TestCase):
    def setUp(self):
        self.ip = FakeIPython()
        self.ipython_patch = mock.patch.object(core, "get_ipython", return_value=self.ip)
        self.ipython_patch.start()

    def tearDown(self):
        self.ipython_patch.stop()

    def test_normal_stream_output_is_preserved(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {"msg_type": "stream", "content": {"name": "stdout", "text": "one\ntwo\n"}}
            )
            renderer.finish()
        self.assertEqual(stdout.getvalue(), "one\ntwo\n")
        self.assertEqual(self.ip.display_pub.calls, [])

    def test_cli_output_replaces_generic_status_not_the_command_result(self):
        renderer = core._HybridOutputRenderer(status_delay=60, code="!whoami")
        with renderer._lock:
            renderer._publish_running_locked()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stdout", "text": "gpudev\r\n"},
                }
            )
            renderer.finish("completed")

        self.assertEqual(stdout.getvalue(), "gpudev\n")
        published = [call["data"]["text/plain"] for call in self.ip.display_pub.calls]
        self.assertNotIn("GPU job completed", "\n".join(published))
        self.assertEqual(published[-1], "")

    def test_cli_output_survives_crlf_split_across_messages(self):
        renderer = core._HybridOutputRenderer(status_delay=60, code="!whoami")
        with renderer._lock:
            renderer._publish_running_locked()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stdout", "text": "gpudev\r"},
                }
            )
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stdout", "text": "\n"},
                }
            )
            renderer.finish("completed")

        self.assertEqual(stdout.getvalue(), "gpudev\n")
        published = [call["data"]["text/plain"] for call in self.ip.display_pub.calls]
        self.assertNotIn("GPU job completed", "\n".join(published))

    def test_execute_result_suppresses_generic_completion_card(self):
        renderer = core._HybridOutputRenderer(status_delay=60, code="2 + 2")
        with renderer._lock:
            renderer._publish_running_locked()

        renderer.handle(
            {
                "msg_type": "execute_result",
                "content": {
                    "data": {"text/plain": "4"},
                    "metadata": {},
                    "transient": {},
                },
            }
        )
        renderer.finish("completed")

        published = [call["data"].get("text/plain", "") for call in self.ip.display_pub.calls]
        self.assertIn("4", published)
        self.assertNotIn("GPU job completed", "\n".join(published))

    def test_remote_manager_preserves_shell_output_after_status(self):
        manager = core.RemoteExecutionManager()
        manager.remote_kc = FakeRemoteKernelClient()
        stdout = io.StringIO()

        with (
            mock.patch.object(manager, "_ensure_live", return_value=True),
            contextlib.redirect_stdout(stdout),
        ):
            manager.execute_remote("!whoami")

        self.assertEqual(stdout.getvalue(), "gpudev\n")
        published = [call["data"]["text/plain"] for call in self.ip.display_pub.calls]
        self.assertNotIn("GPU job completed", "\n".join(published))

    def test_pip_raw_bytes_become_one_live_progress_display(self):
        renderer = core._HybridOutputRenderer(
            status_delay=60,
            code="!pip install torch",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stdout", "text": "Downloading model.whl\n"},
                }
            )
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stdout", "text": "Progress 524288 of 1048576\n"},
                }
            )

        self.assertEqual(stdout.getvalue(), "Downloading model.whl\n")
        self.assertEqual(len(self.ip.display_pub.calls), 1)
        data = self.ip.display_pub.calls[0]["data"]
        self.assertIn('value="524288" max="1048576"', data["text/html"])
        self.assertIn("Installing Python packages", data["text/html"])
        self.assertIn("Downloaded: 512.0 KB / 1.0 MB (50.0%)", data["text/html"])
        renderer.finish()

    def test_curl_meter_becomes_a_labeled_download_summary(self):
        renderer = core._HybridOutputRenderer(
            status_delay=60,
            code="!curl -LO https://www.nuscenes.org/data/v1.0-mini.tgz",
        )
        stdout = io.StringIO()
        raw_meter = "  8 3974M 8 351M 0 0 10.0M 0 0:06:36 0:00:35 0:06:01 9759k"
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {"name": "stderr", "text": f"\r{raw_meter}\r"},
                }
            )

        self.assertEqual(stdout.getvalue(), "")
        html = self.ip.display_pub.calls[-1]["data"]["text/html"]
        self.assertIn("Downloading v1.0-mini.tgz", html)
        self.assertIn("Elapsed:", html)
        self.assertIn("Downloaded: 351.0 MB / 3.9 GB (8%)", html)
        self.assertIn("Speed: 9.5 MB/s", html)
        self.assertIn("Remaining: 6m 1s", html)
        self.assertNotIn("8 3974M", html)
        renderer.finish()

    def test_split_pip_raw_record_is_buffered_until_complete(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {"msg_type": "stream", "content": {"name": "stdout", "text": "Progress 25 of "}}
            )
            self.assertEqual(self.ip.display_pub.calls, [])
            renderer.handle(
                {"msg_type": "stream", "content": {"name": "stdout", "text": "100\n"}}
            )

        self.assertEqual(stdout.getvalue(), "")
        html = self.ip.display_pub.calls[-1]["data"]["text/html"]
        self.assertIn('value="25" max="100"', html)
        renderer.finish()

    def test_carriage_return_updates_are_coalesced(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            renderer.handle(
                {
                    "msg_type": "stream",
                    "content": {
                        "name": "stderr",
                        "text": "\r 25%|██▌       | 1/4\r 50%|█████     | 2/4",
                    },
                }
            )
            renderer.handle(
                {"msg_type": "stream", "content": {"name": "stderr", "text": "\n"}}
            )

        self.assertEqual(stdout.getvalue(), "")
        html = self.ip.display_pub.calls[-1]["data"]["text/html"]
        self.assertIn('value="50.0" max="100.0"', html)
        self.assertIn("Progress: 50%", html)
        self.assertIn("Items: 2 / 4", html)
        self.assertNotIn("█", html)
        renderer.finish()

    def test_silent_job_gets_elapsed_status_then_completion(self):
        renderer = core._HybridOutputRenderer(status_delay=0.01, status_interval=0.01)
        renderer.start()
        deadline = time.monotonic() + 0.5
        while not self.ip.display_pub.calls and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertTrue(self.ip.display_pub.calls)
        self.assertIn("GPU job in progress", self.ip.display_pub.calls[0]["data"]["text/plain"])
        renderer.finish("completed")
        self.assertIn("GPU job completed", self.ip.display_pub.calls[-1]["data"]["text/plain"])

    def test_native_html_progress_suppresses_duplicate_status(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
        with renderer._lock:
            renderer._publish_running_locked()

        renderer.handle(
            {
                "msg_type": "display_data",
                "content": {
                    "data": {"text/html": '<progress value="2" max="4"></progress>'},
                    "metadata": {},
                    "transient": {"display_id": "fastprogress-1"},
                },
            }
        )
        calls_before_finish = len(self.ip.display_pub.calls)
        renderer.finish("completed")

        self.assertEqual(len(self.ip.display_pub.calls), calls_before_finish)
        self.assertEqual(
            self.ip.display_pub.calls[-1]["data"]["text/html"],
            '<progress value="2" max="4"></progress>',
        )

    def test_remote_error_marks_final_status_failed(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
        with renderer._lock:
            renderer._publish_running_locked()
        with mock.patch.object(core, "display"):
            renderer.handle(
                {
                    "msg_type": "error",
                    "content": {"traceback": ["RuntimeError: download failed"]},
                }
            )
        renderer.finish("failed")
        self.assertTrue(renderer.saw_error)
        self.assertIn("GPU job failed", self.ip.display_pub.calls[-1]["data"]["text/plain"])


if __name__ == "__main__":
    unittest.main()
