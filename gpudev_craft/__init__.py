"""gpudev CRAFT — remote GPU for SolveIt / Jupyter.

Prefer the short dialog loader ``CRAFT.py`` (or ``from gpudev_craft.magics import install_core``).
Implementation lives in ``core``; optional addons via ``install_pcviz`` / ``install_sslive`` / ``install_tidy3``.
"""

from .magics import (
    install_core,
    install_mojo,
    install_pcviz,
    install_sslive,
    install_tidy3,
    install,
)

__all__ = [
    "install_core",
    "install",
    "install_pcviz",
    "install_sslive",
    "install_mojo",
    "install_tidy3",
]
