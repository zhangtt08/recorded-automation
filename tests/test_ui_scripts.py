"""界面脚本必须能被 JS 引擎解析：一次转义错误就能让整屏界面静默不初始化。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ra" / "ui"


class UiScriptSyntaxTest(unittest.TestCase):
    """app.js 里一个提前闭合的引号不会让 Python 报任何错，只会让界面停在「正在连接后端…」。"""

    @unittest.skipIf(shutil.which("node") is None, "本机没有 node，跳过 JS 语法门")
    def test_ui_scripts_parse(self):
        for name in ("app.js", "record_script.js"):
            source = (UI / name).read_text(encoding="utf-8")
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
                handle.write("new Function(%s);" % repr(source))
                tmp = handle.name
            code = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=60)
            Path(tmp).unlink(missing_ok=True)
            self.assertEqual(code.returncode, 0, f"{name}: {code.stderr[:400]}")

    def test_index_html_has_the_surfaces_the_checks_rely_on(self):
        html = (UI / "index.html").read_text(encoding="utf-8")
        for element in ('id="view-agent"', 'id="set-winmode"', 'id="win-full"', 'id="gsw"',
                        'id="onboarding"', 'id="agent-probe"', 'href="icon.png"',
                        "<title>录放台</title>", 'id="win-close"', 'id="tb-view"',
                        'id="set-agent"', 'id="win-line"'):
            self.assertIn(element, html, element)

    def test_every_view_has_a_section(self):
        html = (UI / "index.html").read_text(encoding="utf-8")
        for view in ("deck", "recording", "review", "run", "history", "secrets", "agent", "settings"):
            self.assertIn('id="view-' + view + '"', html, view)

    def test_agent_surface_is_wired_in_the_controller(self):
        js = (UI / "app.js").read_text(encoding="utf-8")
        for hook in ("refreshAgent", "agent_probe", "renderOnboarding", "win_remeasure",
                     "win_mode", "startGesture", "win_gesture_end"):
            self.assertIn(hook, js, hook)


if __name__ == "__main__":
    unittest.main()
