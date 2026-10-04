"""窗口一体化：几何换算、默认停靠无边框、切换会记住、不支持时如实报告。"""

import tempfile
import unittest
from pathlib import Path

from ra import winframe
from ra.main import build

INSETS = {"left": 8, "top": 0, "right": 8, "bottom": 8}


class FakeWindow:
    """只替 Shell 会被 Api 调到的那几个方法，返回值刻意带 mode/ok。"""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.mode = "docked"
        self.calls = []

    def win_mode(self, mode: str) -> dict:
        self.calls.append(mode)
        if not self.supported:
            return {"ok": False, "error": "CDP 不可用：当前引擎无法切换窗口模式"}
        self.mode = mode
        return {"ok": True, "mode": mode, "error": "", "frameless": mode in ("docked", "fullscreen"),
                "titlebar_hidden": mode == "docked", "strip": 31 if mode == "docked" else 0,
                "content": [120, 0, 1400, 860], "fullscreen": mode == "fullscreen"}

    def win_state(self) -> dict:
        return {"mode": self.mode, "docked": self.mode == "docked", "strip": 31, "frameless": True,
                "titlebar_hidden": self.mode == "docked", "content": [120, 0, 1400, 860], "fullscreen":
                self.mode == "fullscreen", "supported": True, "hwnd": True}

    def win_gesture_end(self) -> dict:
        return {"ok": True, "content": [120, 0, 1400, 860], "mode": self.mode}


class GeometryTest(unittest.TestCase):
    """内容矩形 <-> 窗口矩形的换算是纯函数：错了窗口就会露出标题条，这里先拦住。"""

    def test_roundtrip_content_to_window_and_back(self):
        content = (300, 0, 1084, 721)
        window = winframe.content_to_window(content, INSETS, 31)
        self.assertEqual(window, (292, -31, 1100, 760))
        self.assertEqual(winframe.window_to_content(window, INSETS, 31), content)

    def test_strip_offset_puts_the_browser_titlebar_above_the_screen(self):
        window = winframe.content_to_window((120, 0, 1400, 860), INSETS, 31)
        # 标题条占据 [窗口顶边+上留白, 内容顶边)，必须整段在屏幕外
        self.assertLessEqual(window[1] + INSETS["top"] + 31, 0)

    def test_no_strip_means_no_offset(self):
        self.assertEqual(winframe.content_to_window((0, 0, 800, 600), INSETS, 0), (-8, 0, 816, 608))

    def test_clamp_keeps_the_window_inside_the_work_area(self):
        clamped = winframe.clamp_content((-5000, -5000, 99999, 99999), (0, 0, 1920, 1040))
        self.assertEqual(clamped, (0, 0, 1920, 1040))
        small = winframe.clamp_content((100, 0, 10, 10), (0, 0, 1920, 1040), minimum=(960, 540))
        self.assertGreaterEqual(small[2], 960)
        self.assertGreaterEqual(small[3], 540)


class WindowModeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-window-"))
        self.api, self.session, _ = build(self.root)

    def tearDown(self):
        self.session.shutdown()

    def test_default_is_docked_frameless(self):
        self.assertEqual(self.api.settings.all()["window_mode"], "docked")
        self.assertFalse(self.api.settings.all()["window_fullscreen"])

    def test_switching_mode_persists_the_choice(self):
        window = FakeWindow()
        self.api.attach(window)
        result = self.api.win_mode("fullscreen")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.api.settings.all()["window_mode"], "fullscreen")
        self.api.win_mode("docked")
        self.assertEqual(self.api.settings.all()["window_mode"], "docked")
        self.assertEqual(window.calls, ["fullscreen", "docked"])

    def test_win_fullscreen_maps_onto_the_mode_switch(self):
        window = FakeWindow()
        self.api.attach(window)
        self.assertTrue(self.api.win_fullscreen(True)["ok"])
        self.assertEqual(window.mode, "fullscreen")
        self.api.win_fullscreen(False)
        self.assertEqual(window.mode, "docked")

    def test_unsupported_engine_reports_the_failure_and_keeps_the_preference(self):
        self.api.attach(FakeWindow(supported=False))
        result = self.api.win_mode("fullscreen")
        self.assertFalse(result["ok"])
        self.assertIn("CDP", result["error"])
        self.assertEqual(self.api.settings.all()["window_mode"], "docked")

    def test_unknown_mode_is_refused_with_the_allowed_list(self):
        self.api.attach(FakeWindow())
        result = self.api.win_mode("tilted")
        self.assertFalse(result["ok"])
        self.assertIn("docked", result["modes"])

    def test_window_state_reports_defaults_without_a_window(self):
        state = self.api.win_state()
        self.assertFalse(state["frameless"])
        self.assertFalse(state["supported"])


class SettingsGuardTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ra-window-settings-"))
        self.api, self.session, _ = build(self.root)

    def tearDown(self):
        self.session.shutdown()

    def test_bad_window_mode_is_rejected_not_silently_stored(self):
        result = self.api.settings_set({"window_mode": "sideways"})
        self.assertIn("error", result)
        self.assertEqual(self.api.settings.all()["window_mode"], "docked")

    def test_agent_switches_are_stored_as_booleans(self):
        self.api.settings_set({"agent_api": False, "agent_port": 9001})
        settings = self.api.settings.all()
        self.assertFalse(settings["agent_api"])
        self.assertEqual(settings["agent_port"], 9001)


class ShellProbeWithoutWindowTest(unittest.TestCase):
    """没浏览器、没 CDP 时也必须能回答窗口状态：_probe 里每个字段都要在构造时就存在。"""

    def test_probe_and_place_answer_without_a_browser(self):
        from ra.shell import Shell

        root = Path(tempfile.mkdtemp(prefix="ra-shell-"))
        shell = Shell(root, root / "profile", lambda method, args: None, mode="docked")
        state = shell._probe()
        for key in ("mode", "strip", "content", "rect", "place_error", "asked", "titlebar_hidden",
                    "frameless", "supported", "work", "monitor"):
            self.assertIn(key, state)
        self.assertFalse(state["hwnd"])
        placed = shell._place((100, 0, 1200, 700))
        self.assertFalse(placed["ok"])
        self.assertIn("句柄", placed["note"])
        self.assertEqual(shell.win_state()["mode"], "docked")

    def test_place_body_actually_runs(self):
        """摆放主体必须真跑一遍：一个漏传的参数（verify）曾让它 NameError，
        而早退出的测试完全看不见 —— 这条就是给那种静默失败兜底的。"""
        from ra.shell import Shell

        root = Path(tempfile.mkdtemp(prefix="ra-shell-"))
        shell = Shell(root, root / "profile", lambda method, args: None, mode="docked")
        shell._hwnd = 1
        shell._content = (100, 0, 1200, 700)
        shell._send_bounds = lambda strip_override=None: (92, -76, 1216, 784)
        shell._read_content = lambda: shell._content
        shell._sync_page = lambda: None
        shell._marker = lambda on: None
        shell._verify_marker = lambda x, y: 0
        result = shell._place((120, 0, 1100, 680), verify=True)
        self.assertTrue(result["ok"], result)
        for key in ("content", "window", "asked", "actual", "via", "strip", "pixel_ok", "drift"):
            self.assertIn(key, result)
        self.assertTrue(result["pixel_ok"])
        self.assertEqual(result["content"][0], 120)


if __name__ == "__main__":
    unittest.main()
