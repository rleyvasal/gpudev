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
        torch_install = SCRIPT.index("-r /tmp/gpudev-req/pylock.gpudev-torch.toml")
        base_install = SCRIPT.index("-r /tmp/gpudev-req/requirements-base.txt")
        self.assertLess(torch_install, base_install)
        between = SCRIPT[torch_install:base_install]
        self.assertIn("RUN --mount=type=cache,target=/root/.cache/uv", between)

    def test_torch_stack_is_detected_resolved_and_locked(self):
        requirements = function_body("write_base_requirements")
        self.assertIn("requirements-torch.in", SCRIPT)
        self.assertIn("pylock.gpudev-torch.toml", SCRIPT)
        self.assertIn("--query-gpu=index,name,compute_cap,driver_version", SCRIPT)
        self.assertIn('--torch-backend "$resolver_backend"', SCRIPT)
        self.assertIn('requested="${GPUDEV_TORCH_BACKEND:-auto}"', SCRIPT)
        self.assertIn("GPUDEV_ML_REFRESH", SCRIPT)
        self.assertIn('"gpu_fingerprint"', SCRIPT)
        self.assertIn("torch\ntorchvision\ntorchaudio", requirements)
        self.assertNotIn("torch==", requirements)

    def test_setup_reuses_lock_only_for_same_hardware_profile(self):
        reuse = function_body("ml_lock_is_current")
        self.assertIn('profile.get("gpu_fingerprint") == sys.argv[2]', reuse)
        self.assertIn('profile.get("requested_backend") == sys.argv[3]', reuse)
        self.assertIn('profile.get("schema") == int(sys.argv[4])', reuse)
        self.assertIn('profile.get("lock_sha256") == lock_hash', reuse)
        self.assertIn('"${GPUDEV_ML_REFRESH:-0}" != "1"', reuse)

    def test_torch_build_runs_a_cuda_kernel_on_every_gpu(self):
        check = function_body("check_all_torch_gpus")
        self.assertIn('--gpus "device=${index}"', check)
        self.assertIn("torch.arange(4, device=device", check)
        self.assertIn("torch.cuda.synchronize(device)", check)
        self.assertIn("torch.cuda.device_count() == 1", check)

    def test_ml_resolution_happens_before_image_build(self):
        main = function_body("main")
        self.assertLess(main.index("resolve_ml_stack"), main.index("build_base_image"))
        self.assertLess(main.index("build_base_image"), main.index("verify_torch_cuda"))

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
