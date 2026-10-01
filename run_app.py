"""录放台启动入口（封装后的 exe 从这里开始）。"""

from __future__ import annotations

import os
import sys

if sys.stdout is None:  # windowed 打包后没有控制台
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _devnull
    sys.stderr = _devnull

from ra.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
