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

    def test_pip_raw_bytes_become_one_live_progress_display(self):
        renderer = core._HybridOutputRenderer(status_delay=60)
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
        self.assertIn("512.0 KB / 1.0 MB", data["text/html"])
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
        self.assertIn("2/4", html)
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
