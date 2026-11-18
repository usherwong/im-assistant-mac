"""微信自动填入/发送写入层回归：总闸、草稿保护、坐标换算、发送按钮定位、读回复核。

真机验证不在单测里做（会真的往微信粘贴/发送），合成事件全部用 Mock 拦掉。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import wechat_auto
from perception import TextBlock

WIN = {'wid': 1, 'x': 0.0, 'y': 0.0, 'w': 480.0, 'h': 616.0}


class GateTests(unittest.TestCase):
    def test_gate_reads_env(self):
        for raw, expected in (('1', True), ('true', True), ('0', False), (None, False)):
            with self.subTest(raw=raw), patch('userconfig.get', return_value=raw):
                self.assertIs(wechat_auto.enabled(), expected)

    def test_fill_and_send_refuse_without_the_gate(self):
        with patch.object(wechat_auto, 'enabled', return_value=False):
            self.assertEqual(wechat_auto.fill('hi', WIN, (1, 2, 3, 4))[1], wechat_auto.REASON_GATE)
            self.assertEqual(wechat_auto.send(WIN, (1, 2, 3, 4))[1], wechat_auto.REASON_GATE)


class GeometryTests(unittest.TestCase):
    def test_region_conversion(self):
        region = wechat_auto._region(WIN, (120, 260, 200, 50))
        self.assertAlmostEqual(region[0], 0.25)
        self.assertAlmostEqual(region[1], 1 - (260 + 50) / 616)
        self.assertAlmostEqual(region[2], 200 / 480)
        self.assertAlmostEqual(region[3], 50 / 616)

    def test_region_rejects_empty(self):
        self.assertIsNone(wechat_auto._region(WIN, (1, 2, 0, 4)))


class MatchTests(unittest.TestCase):
    def test_prefix_survives_ocr_noise(self):
        self.assertTrue(wechat_auto._matches('好的，我下午三点到', '好的 我下午三点到。'))

    def test_short_and_empty_never_match(self):
        self.assertFalse(wechat_auto._matches('好的', ''))
        self.assertFalse(wechat_auto._matches('', '好的'))


class InputTextTests(unittest.TestCase):
    def block(self, text, top):
        return TextBlock(text, 1.0, .3, 1 - top - .03, .4, .03)

    def test_send_button_and_placeholder_are_not_a_draft(self):
        with patch.object(wechat_auto, '_ocr',
                          return_value=[self.block('发送', .93), self.block('发消息', .90)]):
            self.assertEqual(wechat_auto.input_text(WIN, (100, 560, 300, 26)), '')

    def test_trackpad_voice_hint_is_not_a_draft(self):
        with patch.object(wechat_auto, '_ocr',
                          return_value=[self.block('长按触控板语音输入', .88),
                                        self.block('发送', .93)]):
            self.assertEqual(wechat_auto.input_text(WIN, (100, 560, 300, 26)), '')

    def test_real_draft_survives_the_filter(self):
        with patch.object(wechat_auto, '_ocr',
                          return_value=[self.block('发送', .93), self.block('好的我下午到', .90)]):
            self.assertEqual(wechat_auto.input_text(WIN, (100, 560, 300, 26)), '好的我下午到')


class SendButtonTests(unittest.TestCase):
    def block(self, text, sx, sy, sw=40, sh=20):
        # screen points → Vision 归一化（y 底部原点）
        return TextBlock(text, 1.0, sx / 480, 1 - (sy + sh) / 616, sw / 480, sh / 616)

    def test_button_is_right_of_composer_in_the_same_row(self):
        blocks = [self.block('发送(S)', 410, 565)]
        with patch.object(wechat_auto, '_ocr', return_value=blocks):
            self.assertEqual(wechat_auto._send_button(WIN, (100, 560, 300, 26)), (430, 575))

    def test_message_bubble_is_not_the_button(self):
        blocks = [self.block('发送一下资料给我', 380, 560, 90, 24)]
        with patch.object(wechat_auto, '_ocr', return_value=blocks):
            self.assertIsNone(wechat_auto._send_button(WIN, (100, 560, 300, 26)))

    def test_button_inside_an_auto_detected_composer_is_found(self):
        # 自动识别出的输入区把发送按钮也包进去了（微信 4.2.0 实测）：仍要能找到。
        blocks = [self.block('发送', 820, 565)]
        with patch.object(wechat_auto, '_ocr', return_value=blocks):
            self.assertEqual(
                wechat_auto._send_button(WIN, (310, 560, 555, 120)), (840, 575))

    def test_left_side_and_far_away_are_ignored(self):
        blocks = [self.block('发送', 40, 565), self.block('发送', 410, 200)]
        with patch.object(wechat_auto, '_ocr', return_value=blocks):
            self.assertIsNone(wechat_auto._send_button(WIN, (100, 560, 300, 26)))


class FillTests(unittest.TestCase):
    def test_known_message_text_in_the_region_is_not_a_draft(self):
        board = Mock()
        board.stringForType_.return_value = None
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, 'input_text', side_effect=['人呢', '候选内容']), \
             patch.object(wechat_auto, 'click', return_value=True), \
             patch.object(wechat_auto, 'cmd_v', return_value=True), \
             patch.object(wechat_auto.AppKit, 'NSPasteboard') as pb:
            pb.generalPasteboard.return_value = board
            ok, reason = wechat_auto.fill('候选内容', WIN, (100, 560, 300, 26),
                                          known=('在吗', '人呢'))
        self.assertTrue(ok, reason)

    def test_existing_draft_stops_fill(self):
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, 'input_text', return_value='我自己写的草稿'):
            ok, reason = wechat_auto.fill('候选', WIN, (100, 560, 300, 26))
        self.assertFalse(ok)
        self.assertIn(wechat_auto.REASON_DRAFT, reason)

    def test_success_verifies_by_readback_and_restores_clipboard(self):
        board = Mock()
        board.stringForType_.return_value = '原来的剪贴板'
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, 'input_text', side_effect=['', '候选内容']), \
             patch.object(wechat_auto, 'click', return_value=True), \
             patch.object(wechat_auto, 'cmd_v', return_value=True), \
             patch.object(wechat_auto.AppKit, 'NSPasteboard') as pb:
            pb.generalPasteboard.return_value = board
            ok, reason = wechat_auto.fill('候选内容', WIN, (100, 560, 300, 26))
        self.assertTrue(ok, reason)
        board.clearContents.assert_called()
        self.assertEqual(board.setString_forType_.call_args_list[-1].args[0], '原来的剪贴板')

    def test_readback_mismatch_is_a_failure(self):
        board = Mock()
        board.stringForType_.return_value = None
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, 'input_text', side_effect=['', '']), \
             patch.object(wechat_auto, 'click', return_value=True), \
             patch.object(wechat_auto, 'cmd_v', return_value=True), \
             patch.object(wechat_auto.AppKit, 'NSPasteboard') as pb:
            pb.generalPasteboard.return_value = board
            ok, reason = wechat_auto.fill('候选', WIN, (100, 560, 300, 26))
        self.assertFalse(ok)
        self.assertEqual(reason, wechat_auto.REASON_PASTE_FAILED)


class SendTests(unittest.TestCase):
    def test_click_then_verify_empty_composer(self):
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, '_send_button', return_value=(430, 575)), \
             patch.object(wechat_auto, 'click', return_value=True) as click, \
             patch.object(wechat_auto, 'input_text', return_value=''):
            ok, reason = wechat_auto.send(WIN, (100, 560, 300, 26), '候选')
        self.assertTrue(ok, reason)
        click.assert_called_once_with((430, 575))

    def test_short_ui_leftover_does_not_fake_a_failure(self):
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, '_send_button', return_value=(430, 575)), \
             patch.object(wechat_auto, 'click', return_value=True), \
             patch.object(wechat_auto, 'input_text', return_value='发'):
            ok, reason = wechat_auto.send(WIN, (100, 560, 300, 26), '候选内容')
        self.assertTrue(ok, reason)

    def test_text_still_in_composer_means_not_sent(self):
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, '_send_button', return_value=(430, 575)), \
             patch.object(wechat_auto, 'click', return_value=True), \
             patch.object(wechat_auto, 'input_text', return_value='候选'):
            ok, reason = wechat_auto.send(WIN, (100, 560, 300, 26), '候选')
        self.assertFalse(ok)
        self.assertEqual(reason, wechat_auto.REASON_SEND_FAILED)

    def test_missing_button_falls_back_to_return(self):
        # 微信有些版本/主题把发送按钮画成图标，OCR 取不到文字；回车是同一条人机路径。
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, '_send_button', return_value=None), \
             patch.object(wechat_auto, 'press_return', return_value=True) as enter, \
             patch.object(wechat_auto, 'input_text', return_value=''):
            ok, reason = wechat_auto.send(WIN, (100, 560, 300, 26), '候选')
        self.assertTrue(ok, reason)
        enter.assert_called_once_with()

    def test_no_button_and_no_keyboard_is_a_hard_stop(self):
        with patch.object(wechat_auto, 'enabled', return_value=True), \
             patch.object(wechat_auto, '_send_button', return_value=None), \
             patch.object(wechat_auto, 'press_return', return_value=False):
            ok, reason = wechat_auto.send(WIN, (100, 560, 300, 26), '候选')
        self.assertFalse(ok)
        self.assertEqual(reason, wechat_auto.REASON_NO_SEND)


if __name__ == '__main__':
    unittest.main()
