"""自动回复接线回归：什么时候 arm、什么时候被取消（不跑真实线程/不碰微信）。"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support_hud import HUD
import autoreply


def conf(**kw):
    base = dict(auto_fill=True, auto_send=False, delay=5, allowlist=('家人群',),
                max_risk=6, min_gap=30, synthetic=True, acknowledged=True)
    base.update(kw)
    return autoreply.Config(**base)


class AutoArmTests(unittest.TestCase):
    def setUp(self):
        import test_hud_reply
        self.fixture = test_hud_reply.HudReplyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.h._app = SimpleNamespace(key='wechat', display_name='聊天应用')
        self.h._reply_key = ('wechat', '家人群', '在吗', None, ())
        self.h._auto_lock = threading.Lock()
        self.h._auto_last_key = None
        self.h._auto_paused = False
        self.h._auto = None
        self.h._auto_cancel_evt = threading.Event()
        self.h._auto_run = Mock()

    def arm(self, config, risk=3, ranked=None):
        with patch('autoreply.load', return_value=config):
            self.h._auto_prepare(SimpleNamespace(text='在吗'), {'risk': risk},
                                 ranked if ranked is not None else
                                 [(0, '高情商', [{'text': '在的', 'prob': .9}]),
                                  (0, '贴吧', [{'text': '嗯', 'prob': .4}])])

    def test_arms_with_top_candidate_and_fill_only(self):
        self.arm(conf())
        self.assertEqual(self.h._auto['text'], '在的')
        self.assertFalse(self.h._auto['send'])
        args = self.h._auto_run.call_args.args
        self.assertEqual(args[0], '在的')
        self.assertEqual(args[1], '家人群')
        self.assertFalse(args[2])

    def test_picks_the_first_candidate_on_the_panel(self):
        self.arm(conf(), ranked=[(0, 'A', [{'text': '第一条', 'prob': .2}]),
                                 (1, 'B', [{'text': '第二条', 'prob': .8}])])
        self.assertEqual(self.h._auto['text'], '第一条')

    def test_works_without_a_verdict(self):
        # 判断层挂掉（TypeSafe 超时/本地模型未下载）时仍然按首条候选自动回复
        self.arm(conf(), risk=None)
        self.assertEqual(self.h._auto['text'], '在的')

    def test_auto_send_flag_is_forwarded(self):
        self.arm(conf(auto_send=True))
        self.assertTrue(self.h._auto_run.call_args.args[2])

    def test_disabled_never_arms(self):
        self.arm(conf(auto_fill=False))
        self.assertIsNone(self.h._auto)
        self.h._auto_run.assert_not_called()

    def test_unlisted_chat_never_arms(self):
        self.h._reply_key = ('wechat', '陌生群', '在吗', None, ())
        self.arm(conf())
        self.assertIsNone(self.h._auto)
        self.h._auto_run.assert_not_called()

    def test_high_risk_never_arms(self):
        self.arm(conf(max_risk=4), risk=8)
        self.assertIsNone(self.h._auto)

    def test_paused_never_arms(self):
        self.h._auto_paused = True
        self.arm(conf())
        self.assertIsNone(self.h._auto)

    def test_first_message_seen_after_switching_is_never_answered(self):
        # 启动/切回聊天时屏幕上已有的消息：只记录，不自动回复
        self.h._auto_suppress_text = '在吗'
        self.arm(conf())
        self.assertIsNone(self.h._auto)
        self.h._auto_run.assert_not_called()

    def test_same_message_is_armed_once(self):
        self.arm(conf())
        self.arm(conf())
        self.assertEqual(self.h._auto_run.call_count, 1)

    def test_foreground_switch_cancels_pending(self):
        self.arm(conf())
        evt = self.h._auto_cancel_evt
        self.h._set_foreground_state(SimpleNamespace(key='telegram', display_name='Telegram'))
        self.assertIsNone(self.h._auto)
        self.assertTrue(evt.is_set())


if __name__ == '__main__':
    unittest.main()
