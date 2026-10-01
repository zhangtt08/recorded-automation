"""浏览器引擎选择：优先自带/已安装的 Chromium，缺失时自动回退到本机 Edge。

封装版不依赖某一台机器的浏览器缓存；两个引擎都不可用时给出可执行的修复指令。
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

ATTEMPTS = (("chromium", {}), ("msedge", {"channel": "msedge"}))
HEADLESS_ATTEMPTS = (("chromium", {}), ("chromium-new-headless", {"channel": "chromium"}),
                     ("msedge", {"channel": "msedge"}))


def launch_persistent(pw, profile_dir: Path, *, headless: bool = False, proxy: str = "",
                      viewport: dict | None = None, args: list[str] | None = None):
    """返回 (context, engine_name)。无界面时允许用完整 Chromium 的新无界面模式。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    options: dict = {"headless": headless}
    if viewport:
        options["viewport"] = viewport
    if args:
        options["args"] = list(args)
    if proxy:
        options["proxy"] = {"server": proxy}
    failures = []
    for name, extra in (HEADLESS_ATTEMPTS if headless else ATTEMPTS):
        try:
            context = pw.chromium.launch_persistent_context(str(profile_dir), **{**options, **extra})
            return context, name
        except PlaywrightError as exc:
            failures.append(f"{name}: {str(exc).splitlines()[0] if str(exc) else '启动失败'}")
    raise RuntimeError(
        "没有可用的浏览器引擎（" + "；".join(failures) + "）。"
        "修复方式：执行 python -m playwright install chromium，或安装 Microsoft Edge。")
