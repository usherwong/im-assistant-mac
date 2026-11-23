"""Telegram 读取层离线回归：布局换算、归属判定、抽消息、指纹短路。

不读屏、不连 Telegram：AX 布局用纯函数测，OCR 结果用合成 TextBlock。
真机自测：`PYTHONPATH=src python src/apps/telegram_reader.py`（Telegram 置前）。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from perception import TextBlock
from apps import telegram_reader as tr
from apps.telegram_reader import Layout


WIN = {"wid": 1, "title": "t", "x": 100.0, "y": 200.0, "w": 480.0, "h": 616.0}
LAYOUT = Layout(title="会话", top=0.14, input_top=0.91, pane_left=0.0, pane_right=1.0)


def block(text, x, top, w, h=0.026, conf=1.0):
    """Vision 口径：y 是底部原点。"""
    return TextBlock(text, conf, x, 1.0 - top - h, w, h)


class LayoutTests(unittest.TestCase):
    def test_compact_mode_pane_starts_at_zero(self):
        layout = tr.layout_from_parts(
            WIN, "会话", (160.0, 230.0, 270.0, 37.0), (170.0, 770.0, 300.0, 25.0), has_back=True)
        self.assertAlmostEqual(layout.top, (230 + 37 - 200) / 616 + 0.008)
        self.assertAlmostEqual(layout.input_top, (770 - 200) / 616 - 0.015)
        self.assertEqual(layout.pane_left, 0.0)
        self.assertEqual(layout.input_rect, (170.0, 770.0, 300.0, 25.0))

    def test_split_mode_pane_starts_left_of_title(self):
        layout = tr.layout_from_parts(
            WIN, "会话", (260.0, 230.0, 270.0, 37.0), (270.0, 770.0, 300.0, 25.0), has_back=False)
        self.assertAlmostEqual(layout.pane_left, (260 - 100) / 480 - 0.03)

    def test_implausible_geometry_is_rejected(self):
        self.assertIsNone(tr.layout_from_parts(
            WIN, "", (160.0, 230.0, 270.0, 37.0), (170.0, 770.0, 300.0, 25.0), True))
        self.assertIsNone(tr.layout_from_parts(
            WIN, "会话", (160.0, 590.0, 270.0, 37.0), (170.0, 770.0, 300.0, 25.0), True))
        self.assertIsNone(tr.layout_from_parts(
            WIN, "会话", (160.0, 230.0, 270.0, 37.0), (170.0, 240.0, 300.0, 25.0), True))

    def test_ocr_region_is_bottom_origin(self):
        layout = Layout("t", 0.14, 0.91, 0.0, 1.0)
        x, y, w, h = tr.ocr_region(layout)
        self.assertAlmostEqual(y, 0.09)
        self.assertAlmostEqual(h, 0.77)
        self.assertEqual((x, w), (0.0, 1.0))


class SideTests(unittest.TestCase):
    def test_left_is_them_right_is_me_center_is_unknown(self):
        self.assertEqual(tr.side_for(0.06, 0.32, LAYOUT), "them")
        self.assertEqual(tr.side_for(0.72, 0.22, LAYOUT), "me")
        self.assertEqual(tr.side_for(0.30, 0.40, LAYOUT), "unknown")

    def test_wide_incoming_still_them_and_wide_outgoing_still_me(self):
        self.assertEqual(tr.side_for(0.02, 0.85, LAYOUT), "them")
        self.assertEqual(tr.side_for(0.15, 0.82, LAYOUT), "me")


class ExtractTests(unittest.TestCase):
    def test_same_line_fragments_merge(self):
        msgs = tr.extract_messages([block("你好", 0.06, 0.50, 0.10),
                                    block("世界", 0.18, 0.50, 0.10)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["你好 世界"])

    def test_wrapped_lines_fold_inside_one_bubble(self):
        msgs = tr.extract_messages([block("第一行", 0.06, 0.50, 0.30),
                                    block("第二行", 0.06, 0.527, 0.30)], LAYOUT)
        self.assertEqual([(m.text, len(m.lines)) for m in msgs], [("第一行\n第二行", 2)])

    def test_separate_bubbles_stay_separate(self):
        msgs = tr.extract_messages([block("第一条", 0.06, 0.50, 0.20),
                                    block("第二条", 0.06, 0.56, 0.20)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["第一条", "第二条"])

    def test_date_separator_and_timestamp_are_dropped(self):
        msgs = tr.extract_messages([block("今天", 0.46, 0.40, 0.08),
                                    block("20:37", 0.90, 0.60, 0.05),
                                    block("在吗", 0.06, 0.70, 0.10)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["在吗"])

    def test_inline_time_is_stripped(self):
        msgs = tr.extract_messages([block("可以在这里测试 Jev 20:37", 0.06, 0.50, 0.40)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["可以在这里测试 Jev"])

    def test_sender_name_is_not_a_reply_target(self):
        msgs = tr.extract_messages([block("张三", 0.06, 0.50, 0.08, h=0.018),
                                    block("在吗", 0.06, 0.53, 0.10)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["在吗"])

    def test_short_body_with_body_height_survives(self):
        msgs = tr.extract_messages([block("在吗", 0.06, 0.50, 0.10)], LAYOUT)
        self.assertEqual([m.text for m in msgs], ["在吗"])


class FingerprintSkipTests(unittest.TestCase):
    def setup_mocks(self):
        win = SimpleNamespace(wid=1, title="t", x=0.0, y=0.0, w=480.0, h=616.0)
        layout = Layout("t", 0.14, 0.91, 0.0, 1.0)
        self.enterContext(patch.object(tr.perception, "find_window", return_value=win))
        self.enterContext(patch.object(tr, "probe_layout", return_value=(layout, None)))
        self.enterContext(patch.object(tr.perception, "capture_image", return_value=object()))
        self.enterContext(patch.object(tr, "fingerprint", return_value=b"fp"))
        return layout

    def test_unchanged_frame_skips_ocr(self):
        layout = self.setup_mocks()
        key = layout.key({"wid": 1, "w": 480.0, "h": 616.0})
        with patch.object(tr.perception, "ocr_image") as ocr:
            first = tr.read_telegram()
            second = tr.read_telegram(prev_fingerprint=b"fp", prev_layout=key)
        self.assertFalse(first["unchanged"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["messages"], [])
        ocr.assert_called_once()

    def test_changed_frame_runs_ocr_once(self):
        self.setup_mocks()
        with patch.object(tr.perception, "ocr_image", return_value=[]) as ocr:
            res = tr.read_telegram(prev_fingerprint=b"old", prev_layout=("x",))
        self.assertFalse(res["unchanged"])
        ocr.assert_called_once()


class DispatchTests(unittest.TestCase):
    def test_read_conversation_routes_telegram_profile(self):
        import perception
        from apps.profiles import TELEGRAM
        with patch("apps.telegram_reader.read_telegram", return_value={"ok": True}) as rt:
            self.assertEqual(perception.read_conversation(profile=TELEGRAM), {"ok": True})
        rt.assert_called_once()


if __name__ == '__main__':
    unittest.main()
