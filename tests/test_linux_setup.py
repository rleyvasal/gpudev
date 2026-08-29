import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "linux-setup.sh").read_text(encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
