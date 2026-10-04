"""排版与按钮反馈的回归闸门。

这些规则每一条都对应一个真实出现过的缺陷：卡片被纵向 flex 压扁导致内容溢出到下一张卡片上面
（按钮看着在、实际点不到）、class="s" 没有规则导致 Agent 屏像未加样式的裸文本、
后端拒绝时处理器没有 catch 导致「点了没反应」、剪贴板被拒时直接甩出一个文本框。
改坏任何一条，这里要红。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ra" / "ui"
CSS = (UI / "app.css").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")


class LayoutGate(unittest.TestCase):
    def test_scrollable_views_do_not_shrink_their_children(self):
        self.assertRegex(CSS, r"\.view\.scroll\s*>\s*\*\s*\{\s*flex:\s*none")

    def test_card_body_text_has_a_real_rule(self):
        self.assertRegex(CSS, r"\n\.s\s*\{[^}]*font-size")

    def test_resize_grips_cannot_swallow_clicks(self):
        self.assertRegex(CSS, r"\.grips\s*\{\s*pointer-events:\s*none")
        self.assertRegex(CSS, r"\.grips\s+i\s*\{[^}]*pointer-events:\s*auto")
        match = re.search(r"\.wbtns\s*\{[^}]*z-index:\s*(\d+)", CSS)
        self.assertIsNotNone(match, "标题条按钮必须压在缩放手势之上，否则右上角的关闭点不到")
        grip_z = int(re.search(r"\.grips\s+i\s*\{[^}]*z-index:\s*(\d+)", CSS).group(1))
        self.assertGreater(int(match.group(1)), grip_z)

    def test_settings_grid_leaves_room_above_the_bottom_grip(self):
        match = re.search(r"\.set-grid\s*\{\s*padding:\s*([^;]+);", CSS)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(len(match.group(1).split()), 3, "底部内边距不能省：最后一行会被缩放手势盖住")


class FeedbackGate(unittest.TestCase):
    def test_every_rejection_is_reported_to_the_user(self):
        self.assertIn("error.notified = true", JS)
        self.assertRegex(JS, r'unhandledrejection[\s\S]{0,400}?toast\("这一步没有完成')

    def test_one_screen_failing_to_load_names_an_exit(self):
        self.assertIn("function loadFailed", JS)
        self.assertIn('id="load-retry"', JS)
        self.assertRegex(JS, r'refreshAgent\(\)\.catch\(\(error\) => loadFailed')

    def test_clipboard_has_a_fallback_before_the_textarea(self):
        self.assertIn("async function copyText", JS)
        self.assertIn('document.execCommand("copy")', JS)

    def test_self_check_button_cannot_stick_disabled(self):
        block = JS[JS.index("async function runSelfCheck"):]
        self.assertIn("finally", block[:600])

    def test_minimize_reports_failure_instead_of_silently_rejecting(self):
        block = JS[JS.index('$("win-min").onclick'):][:200]
        self.assertIn("catch", block)


class AgentScreenGate(unittest.TestCase):
    def test_agent_cards_carry_padding_and_a_tool_table_header(self):
        self.assertIn('class="card agent-card"', HTML)
        self.assertIn('class="tool-head"', HTML)
        self.assertEqual(HTML.count('id="agent-tools"'), 1)

    def test_agent_stat_cards_use_the_card_style(self):
        self.assertIn("""'<div class="card stat"><p class="k">'""", JS)


class AuditHarnessGate(unittest.TestCase):
    """体检工具本身不能被悄悄削弱：它必须继续拦下会改数据的调用，并继续真的点每个按钮。"""

    def setUp(self):
        path = UI.parent / "uiaudit.py"
        self.assertTrue(path.exists(), "ra/uiaudit.py 不能删：它是「按钮点不动」唯一的产物级证据")
        self.source = path.read_text(encoding="utf-8")

    def test_mutating_calls_stay_blocked(self):
        for method in ("delete_workflow", "run_workflow", "record_start", "reset_profile", "win_close"):
            self.assertIn(f'"{method}"', self.source)

    def test_it_clicks_and_hit_tests_instead_of_trusting_intent(self):
        self.assertIn("elementFromPoint", self.source)
        self.assertIn("el.click()", self.source)
        self.assertIn("scrollIntoView", self.source)

    def test_a_covered_or_dead_button_fails_the_run(self):
        self.assertIn('"covered"', self.source)
        self.assertIn('"dead"', self.source)
        self.assertIn("return 0 if not bad else 1", self.source)


if __name__ == "__main__":
    unittest.main()
