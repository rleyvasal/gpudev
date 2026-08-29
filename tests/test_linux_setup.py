import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "linux-setup.sh").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    marker = f"{name}() {{"
    start = SCRIPT.find(marker)
    if start < 0:
        raise AssertionError(f"Shell function not found: {name}")
    next_function = SCRIPT.find("\n}", start)
    if next_function < 0:
        raise AssertionError(f"Shell function end not found: {name}")
    return SCRIPT[start : next_function + 2]


class LinuxSetupTests(unittest.TestCase):
    def test_cuda_downloads_have_resilient_uv_settings(self):
        self.assertIn("UV_HTTP_TIMEOUT=300", SCRIPT)
        self.assertIn("UV_HTTP_RETRIES=5", SCRIPT)
        self.assertGreaterEqual(
            SCRIPT.count("--mount=type=cache,target=/root/.cache/uv"),
            2,
        )

    def test_torch_and_base_requirements_use_separate_cached_layers(self):
        torch_install = SCRIPT.index("-r /tmp/gpudev-req/requirements-torch.txt")
        base_install = SCRIPT.index("-r /tmp/gpudev-req/requirements-base.txt")
        self.assertLess(torch_install, base_install)
        between = SCRIPT[torch_install:base_install]
        self.assertIn("RUN --mount=type=cache,target=/root/.cache/uv", between)

    def test_active_unchanged_tunnel_is_not_restarted(self):
        body = function_body("setup_host_cf_tunnel")
        self.assertIn("Tunnel configuration unchanged", body)
        self.assertIn("NEED_HOST_TUNNEL_RESTART=1", body)
        self.assertNotIn("systemctl restart gpudev-tunnel", body)
        self.assertNotIn('pkill -f "cloudflared tunnel run', body)

    def test_changed_tunnel_restart_is_deferred(self):
        body = function_body("schedule_host_tunnel_restart")
        self.assertIn("systemd-run", body)
        self.assertIn("--on-active=5s", body)
        main = function_body("main")
        self.assertLess(
            main.index("run_health_check"),
            main.index("schedule_host_tunnel_restart"),
        )


if __name__ == "__main__":
    unittest.main()
