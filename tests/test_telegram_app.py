"""Telegram 适配器与 AX 写入层离线回归：分发、输入框定位、写入读回、草稿保护。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from apps.profiles import TELEGRAM


class TelegramAppTests(unittest.TestCase):
    def test_identity(self):
        from apps.telegram_app import TelegramApp
        app = TelegramApp()
        self.assertEqual(app.key, 'telegram')
        self.assertEqual(app.display_name, 'Telegram')
        self.assertIn('ru.keepcoder.Telegram', app.bundle_ids)
        self.assertTrue(app.needs_screen_capture)

    def test_delegates_to_reader_and_ax_input(self):
        from apps.telegram_app import TelegramApp
        app = TelegramApp()
        with patch('perception.find_window', return_value='win') as fw, \
             patch('perception.read_conversation', return_value={'ok': True}) as rc, \
             patch('ax_input.locate_input', return_value={'box': None}) as li, \
             patch('ax_input.fill_text', return_value=(True, '已填入输入框')) as ft, \
             patch('perception.warm_ocr', return_value=9.0) as wo:
            self.assertEqual(app.find_window(7), 'win')
            fw.assert_called_once_with(TELEGRAM, 7)
            self.assertEqual(app.read_conversation(previous_wid=1), {'ok': True})
            rc.assert_called_once_with(profile=TELEGRAM, previous_wid=1)
            self.assertEqual(app.locate_input({'wid': 1}), {'box': None})
            li.assert_called_once_with({'wid': 1}, app.bundle_ids)
            self.assertEqual(app.fill_text('hi', target={'box': 'b'}), (True, '已填入输入框'))
            ft.assert_called_once_with('hi', target={'box': 'b'})
            self.assertEqual(app.warm(), 9.0)


class FakeAxCase(unittest.TestCase):
    class El:
        def __init__(self, name):
            self.name = name

    def setUp(self):
        import ax_input
        self.ax = ax_input
        self.attrs = {}
        self.rects = {}
        self.enterContext(patch.object(ax_input, '_attr',
                                       side_effect=lambda el, n: self.attrs[id(el)].get(n)))
        self.enterContext(patch.object(ax_input, '_rect',
                                       side_effect=lambda el: self.rects.get(id(el))))
        self.enterContext(patch.object(ax_input, 'running_pid', return_value=1))
        self.enterContext(patch.object(ax_input.AS, 'AXUIElementCreateApplication',
                                       return_value=self.el('app')))
        self.enterContext(patch.object(ax_input.fill, 'has_accessibility', return_value=True))

    def el(self, name, role=None, rect=None, children=(), value=None):
        import ApplicationServices as AS
        obj = self.El(name)
        self.attrs[id(obj)] = {AS.kAXRoleAttribute: role,
                               AS.kAXChildrenAttribute: list(children),
                               AS.kAXValueAttribute: value}
        if rect is not None:
            self.rects[id(obj)] = rect
        return obj


class LocateInputTests(FakeAxCase):
    def tree(self):
        import ApplicationServices as AS
        composer = self.el('composer', 'AXTextArea', (150, 900, 300, 26), value='')
        search = self.el('search', 'AXTextArea', (20, 60, 120, 20), value='')
        win = self.el('win', 'AXWindow', (0, 0, 480, 616), children=[search, composer])
        app = self.attrs  # app element is returned by AXUIElementCreateApplication
        root = self.ax.AS.AXUIElementCreateApplication(1)
        self.attrs[id(root)][AS.kAXWindowsAttribute] = [win]
        self.attrs[id(win)][AS.kAXChildrenAttribute] = [search, composer]
        self.enterContext(patch.object(
            self.ax.AS, 'AXUIElementIsAttributeSettable',
            side_effect=lambda el, name, _: (0, el is composer)))
        return root, win, composer

    def test_largest_settable_text_area_is_the_composer(self):
        self.tree()
        target = self.ax.locate_input({'wid': 1, 'x': 0, 'y': 0, 'w': 480, 'h': 616},
                                      ('ru.keepcoder.Telegram',))
        self.assertEqual(target['box'].name, 'composer')
        self.assertEqual(target['rect'], (150, 900, 300, 26))
        self.assertIsNone(target['reason'])

    def test_missing_accessibility_is_reported(self):
        self.tree()
        with patch.object(self.ax.fill, 'has_accessibility', return_value=False):
            target = self.ax.locate_input({'wid': 1}, ('ru.keepcoder.Telegram',))
        self.assertIsNone(target['box'])
        self.assertEqual(target['reason'], self.ax.fill.REASON_NO_ACCESS)


class FillTests(FakeAxCase):
    def box(self, value=''):
        return self.el('box', 'AXTextArea', (150, 900, 300, 26), value=value)

    def test_writes_draft_free_composer(self):
        box = self.box('')
        with patch.object(self.ax, 'set_value', return_value=True) as sv:
            ok, reason = self.ax.fill_text('候选', target={'box': box})
        self.assertTrue(ok, reason)
        sv.assert_called_once_with(box, '候选')

    def test_existing_draft_is_never_overwritten(self):
        box = self.box('我自己写的')
        with patch.object(self.ax, 'set_value') as sv:
            ok, reason = self.ax.fill_text('候选', target={'box': box})
        self.assertFalse(ok)
        self.assertEqual(reason, self.ax.REASON_DRAFT)
        sv.assert_not_called()

    def test_readback_mismatch_is_a_failure(self):
        box = self.box('')
        with patch.object(self.ax, 'set_value', return_value=False):
            ok, reason = self.ax.fill_text('候选', target={'box': box})
        self.assertFalse(ok)
        self.assertEqual(reason, self.ax.REASON_NOT_VERIFIED)

    def test_no_box_is_a_clear_reason(self):
        ok, reason = self.ax.fill_text('候选', target=None)
        self.assertFalse(ok)
        self.assertEqual(reason, self.ax.REASON_NO_INPUT)


class SetValueTests(FakeAxCase):
    def test_telegram_error_code_but_value_written_counts_as_success(self):
        import ApplicationServices as AS
        box = self.el('box', 'AXTextArea', value='')

        def fake_set(el, name, value):
            self.attrs[id(el)][AS.kAXValueAttribute] = value
            return -25200                      # Telegram 的「假失败」

        with patch.object(self.ax.AS, 'AXUIElementSetAttributeValue', side_effect=fake_set):
            self.assertTrue(self.ax.set_value(box, '写入成功'))

    def test_unchanged_value_is_a_failure(self):
        box = self.el('box', 'AXTextArea', value='')
        with patch.object(self.ax.AS, 'AXUIElementSetAttributeValue', return_value=-25200):
            self.assertFalse(self.ax.set_value(box, '没写进去'))


if __name__ == '__main__':
    unittest.main()
