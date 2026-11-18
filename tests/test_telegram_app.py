"""Telegram 渠道离线回归：profile 键、适配器分发、发送按钮识别、标题选取。

不读屏、不碰真实 Telegram：AX 元素用字典桩替换（所有读取都经 ax_input._attr/_rect），
OCR 用合成 TextBlock。真机验证见 docs/probe/2026-11-18-telegram.md。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from apps.profiles import TELEGRAM, WECHAT, by_key
from perception import TextBlock


class ProfileTests(unittest.TestCase):
    def test_calibration_keys_are_per_app(self):
        self.assertEqual(WECHAT.message_key, 'JEV_MESSAGE_REGION')
        self.assertEqual(WECHAT.input_key, 'JEV_INPUT_REGION')
        self.assertEqual(TELEGRAM.message_key, 'JEV_MESSAGE_REGION_TELEGRAM')
        self.assertEqual(TELEGRAM.input_key, 'JEV_INPUT_REGION_TELEGRAM')

    def test_wechat_values_match_perception_constants(self):
        import perception
        self.assertEqual(WECHAT.layout.chat_pane_x_min, perception.CHAT_PANE_X_MIN)
        self.assertEqual(WECHAT.layout.title_bar_y_max, perception.TITLE_BAR_Y_MAX)
        self.assertEqual(WECHAT.app_names, perception.WECHAT_APP_NAMES)

    def test_telegram_requires_calibration_and_nearest_title(self):
        self.assertTrue(TELEGRAM.requires_calibration)
        self.assertEqual(TELEGRAM.title_pick, 'nearest')
        self.assertEqual(by_key('telegram'), TELEGRAM)
        self.assertIsNone(by_key('feishu'))


class TelegramAppTests(unittest.TestCase):
    def test_identity(self):
        from apps.telegram_app import TelegramApp
        app = TelegramApp()
        self.assertEqual(app.key, 'telegram')
        self.assertEqual(app.display_name, 'Telegram')
        self.assertIn('ru.keepcoder.Telegram', app.bundle_ids)
        self.assertTrue(app.needs_screen_capture)

    def test_delegates_to_perception_and_ax_input(self):
        from apps.telegram_app import TelegramApp
        app = TelegramApp()
        with patch('perception.find_window', return_value='win') as fw, \
             patch('perception.read_conversation', return_value={'ok': True}) as rc, \
             patch('ax_input.locate_input', return_value={'box': None}) as li, \
             patch('ax_input.fill_text', return_value=(True, '已填入输入框')) as ft, \
             patch('perception.warm_ocr', return_value=9.0) as wo:
            self.assertEqual(app.find_window(7), 'win')
            fw.assert_called_once_with(TELEGRAM, 7)
            self.assertEqual(app.read_conversation(previous_wid=1, calibration='cal'),
                             {'ok': True})
            rc.assert_called_once_with(profile=TELEGRAM, previous_wid=1, calibration='cal')
            self.assertEqual(app.locate_input({'wid': 1}), {'box': None})
            li.assert_called_once_with({'wid': 1}, app.bundle_ids)
            self.assertEqual(app.fill_text('hi', target={'box': 'b'}), (True, '已填入输入框'))
            ft.assert_called_once_with('hi', target={'box': 'b'})
            self.assertEqual(app.warm(), 9.0)

    def test_requires_calibration_error_comes_from_perception(self):
        import perception
        res = perception.read_conversation(profile=TELEGRAM)
        self.assertFalse(res['ok'])
        self.assertIn('校准', res['error'])


class SendButtonTests(unittest.TestCase):
    """AX 元素用字典桩：ax_input 的所有读取都走 _attr/_rect，替换这两层即可。"""

    class _El:
        def __init__(self, name):
            self.name = name

    def setUp(self):
        import ax_input
        self.ax = ax_input
        self._attrs = {}
        self._rects = {}
        self.enterContext(patch.object(ax_input, '_attr',
                                       side_effect=lambda el, name: self._attrs[id(el)].get(name)))
        self.enterContext(patch.object(ax_input, '_rect',
                                       side_effect=lambda el: self._rects.get(id(el))))

    def el(self, name, role, rect=None, label='', enabled=True, children=()):
        import ApplicationServices as AS
        obj = self._El(name)
        self._attrs[id(obj)] = {AS.kAXRoleAttribute: role,
                                AS.kAXTitleAttribute: label,
                                AS.kAXEnabledAttribute: enabled,
                                AS.kAXChildrenAttribute: list(children)}
        if rect is not None:
            self._rects[id(obj)] = rect
        return obj

    def test_rightmost_button_wins_when_label_is_localized(self):
        box = self.el('box', 'AXTextArea', (100, 900, 200, 25))
        emoji = self.el('emoji', 'AXButton', (300, 895, 40, 40), 'Emoji')
        send = self.el('send', 'AXButton', (350, 895, 40, 40), 'Absenden')
        window = self.el('win', 'AXWindow', (0, 0, 480, 616), children=[emoji, send])
        self.assertIs(self.ax.find_send_button(window, box), send)

    def test_label_hint_wins_even_when_not_rightmost(self):
        box = self.el('box', 'AXTextArea', (100, 900, 200, 25))
        send = self.el('send', 'AXButton', (300, 895, 40, 40), 'Send')
        extra = self.el('extra', 'AXButton', (360, 895, 40, 40), 'More')
        window = self.el('win', 'AXWindow', (0, 0, 480, 616), children=[send, extra])
        self.assertIs(self.ax.find_send_button(window, box), send)

    def test_disabled_and_out_of_row_buttons_are_ignored(self):
        box = self.el('box', 'AXTextArea', (100, 900, 200, 25))
        disabled = self.el('disabled', 'AXButton', (350, 895, 40, 40), 'Send', enabled=False)
        toolbar = self.el('toolbar', 'AXButton', (350, 40, 40, 40), 'Send')
        window = self.el('win', 'AXWindow', (0, 0, 480, 616), children=[disabled, toolbar])
        self.assertIsNone(self.ax.find_send_button(window, box))

    def test_no_box_means_no_send_button(self):
        window = self.el('win', 'AXWindow', (0, 0, 480, 616))
        self.assertIsNone(self.ax.find_send_button(window, None))


class CalibratedTitleTests(unittest.TestCase):
    def header(self):
        # 顶部是 macOS 窗口标题（更靠上），下面是会话标题
        return [TextBlock('Telegram @ usher', 1.0, .10, .959, .20, .024),
                TextBlock('Saved Messages', 1.0, .20, .881, .20, .024)]

    def run_title(self, pick, header=None, top=.12, noise=None):
        from calibration import Calibration
        import perception
        header = self.header() if header is None else header
        with patch.object(perception, 'ocr_image', side_effect=[[], header]), \
             patch('calibrated_messages.extract', return_value=[]), \
             patch('calibrated_messages.recover_numeric_bubbles', side_effect=lambda image, blocks, rect: blocks):
            cal = Calibration(480.0, 616.0, 0.0, 616.0 * top, 480.0, 616.0 * (0.90 - top))
            win = {'wid': 1, 'title': '', 'w': 480.0, 'h': 616.0, 'x': 0.0, 'y': 0.0}
            return perception.read_calibrated(object(), cal, win, title_pick=pick,
                                              noise=noise)['chat_title']

    def test_nearest_skips_the_window_title(self):
        self.assertEqual(self.run_title('nearest'), 'Saved Messages')

    def test_top_keeps_legacy_behaviour(self):
        self.assertEqual(self.run_title('top'), 'Telegram @ usher')

    def group_header(self):
        # 群聊头部三行：窗口标题 / 群名 / 成员数副标题（离消息区最近）
        return [TextBlock('Telegram @ usher', 1.0, .10, .959, .20, .024),
                TextBlock('Project group', 1.0, .20, .916, .20, .022),
                TextBlock('5 members, 2 online', 1.0, .20, .880, .20, .020)]

    def test_group_subtitle_is_filtered_by_profile_noise(self):
        header = self.group_header()
        self.assertEqual(self.run_title('nearest', header=header, top=.16,
                                        noise=TELEGRAM.noise), 'Project group')
        # 没有 profile noise 时最近一行就是副标题——这正是要避免的串会话
        self.assertEqual(self.run_title('nearest', header=header, top=.16),
                         '5 members, 2 online')


class InlineTimeTests(unittest.TestCase):
    """Telegram 的时间/已读勾在气泡右下角，Vision 会把它并进正文。"""

    def strip(self, text, strip=True):
        from perception import Message
        import perception
        msg = Message(text, 'me', .8, 1.0, lines=[text], x=.6, w=.2)
        header = []
        with patch.object(perception, 'ocr_image', side_effect=[[], header]), \
             patch('calibrated_messages.extract', return_value=[msg]), \
             patch('calibrated_messages.recover_numeric_bubbles',
                   side_effect=lambda image, blocks, rect: blocks):
            from calibration import Calibration
            cal = Calibration(480.0, 616.0, 0.0, 616.0 * .12, 480.0, 616.0 * .78)
            win = {'wid': 1, 'title': '', 'w': 480.0, 'h': 616.0, 'x': 0.0, 'y': 0.0}
            res = perception.read_calibrated(object(), cal, win, strip_time=strip)
        return [m.text for m in res['messages']]

    def test_time_and_read_marks_are_stripped(self):
        self.assertEqual(self.strip('hello 19:18w/'), ['hello'])

    def test_body_time_survives_when_glued(self):
        self.assertEqual(self.strip('发送 19:18:42 19:18 W/'), ['发送 19:18:42'])

    def test_time_only_message_disappears(self):
        self.assertEqual(self.strip('19:18'), [])

    def test_off_by_default(self):
        self.assertEqual(self.strip('hello 19:18w/', strip=False), ['hello 19:18w/'])


class ComposerCalibrationTests(unittest.TestCase):
    def test_short_composer_region_is_valid(self):
        # Telegram 输入框实测 26pt 高；旧的下限 60pt 会让它无法框选（#155）
        from calibration import Calibration
        self.assertEqual(Calibration(480, 616, 10, 500, 400, 26).height, 26)
        with self.assertRaises(ValueError):
            Calibration(480, 616, 10, 500, 400, 19)


class CalibrationStoreTests(unittest.TestCase):
    """HUD 的校准按 app 分开存：Telegram 用 _TELEGRAM 键，微信继续用旧键。"""

    def load(self, key, values):
        from support_hud import HUD
        fake = HUD['Harness']()
        with patch.object(HUD['userconfig'], 'get',
                          side_effect=lambda k, *a: values.get(k)):
            fake._load_calibration(key)
        return fake

    def test_telegram_region_restored_from_its_own_keys(self):
        from calibration import Calibration
        region = Calibration(480, 616, 0, 80, 480, 480)
        editor = Calibration(480, 616, 10, 560, 400, 26)
        fake = self.load('telegram', {
            'JEV_MESSAGE_REGION_TELEGRAM': region.serialize(),
            'JEV_INPUT_REGION_TELEGRAM': editor.serialize(),
        })
        self.assertEqual(fake._cal_msg_key, 'JEV_MESSAGE_REGION_TELEGRAM')
        self.assertEqual(fake._cal_input_key, 'JEV_INPUT_REGION_TELEGRAM')
        self.assertTrue(fake._calibration_required)
        self.assertEqual(fake._calibration, region)
        self.assertEqual(fake._input_calibration, editor)

    def test_wechat_keeps_the_legacy_keys(self):
        from calibration import Calibration
        region = Calibration(600, 600, 120, 60, 450, 350)
        fake = self.load('wechat', {'JEV_MESSAGE_REGION': region.serialize()})
        self.assertEqual(fake._cal_msg_key, 'JEV_MESSAGE_REGION')
        self.assertEqual(fake._calibration, region)

    def test_apps_that_need_calibration_ask_for_it_when_none_saved(self):
        fake = self.load('telegram', {})
        self.assertTrue(fake._calibration_required)
        self.assertIsNone(fake._calibration)
        fake = self.load('wechat', {})
        self.assertFalse(fake._calibration_required)


if __name__ == '__main__':
    unittest.main()
