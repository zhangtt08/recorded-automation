"""Filesystem locations for bundled resources and user data."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

APP_TITLE = "录放台"
DATA_DIRNAME = "RecordedAutomation"


def resource_root() -> Path:
    """Directory holding packaged resources (UI assets, recorder script, schema)."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "ra"
    return Path(__file__).resolve().parent


def data_root() -> Path:
    """Per-user writable directory: workflows, journal, secrets, browser profile."""
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / DATA_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def browsers_path_env() -> None:
    """Point Playwright at a bundled engine folder, or the machine's browser cache.

    打包后 Playwright 的默认解析会指向驱动目录内的 .local-browsers（里面并没有浏览器），
    因此这里显式回退到本机缓存目录。
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    candidates: list[Path] = []
    override = os.environ.get("RA_BROWSERS_PATH")
    if override:
        candidates.append(Path(override))
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(str(sys._MEIPASS)) / "ms-playwright")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "ms-playwright")
    cache = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    if cache:
        candidates.append(Path(cache) / ("ms-playwright" if os.name == "nt" else "ms-playwright"))
    for candidate in candidates:
        if candidate.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            return


def new_run_id() -> str:
    return uuid4().hex[:12]
