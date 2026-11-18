"""自动回复决策层回归：白名单、风险阈值、限频、开关顺序，全部离线。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import autoreply
from autoreply import Config


def cfg(**kw):
    base = dict(auto_fill=True, auto_send=False, delay=5, allowlist=('家人群',),
                max_risk=6, min_gap=30, synthetic=True, acknowledged=True)
    base.update(kw)
    return Config(**base)


class ParseTests(unittest.TestCase):
    def test_allowlist_split_and_clean(self):
        self.assertEqual(autoreply.parse_allowlist(' 家人群 | 客户A||家人群 '), ('家人群', '客户A'))
        self.assertEqual(autoreply.parse_allowlist(None), ())

    def test_allowlist_drops_oversize_titles(self):
        self.assertEqual(autoreply.parse_allowlist('x' * 200), ())

    def test_dump_round_trip(self):
        titles = ('家人群', '客户A')
        self.assertEqual(autoreply.parse_allowlist(autoreply.dump_allowlist(titles)), titles)

    def test_load_reads_env_keys(self):
        values = {
            'JEV_AUTO_FILL': '1', 'JEV_AUTO_SEND': '1', 'JEV_AUTO_DELAY': '99',
            'JEV_AUTO_ALLOWLIST': 'A|B', 'JEV_AUTO_MAX_RISK': '9',
            'JEV_AUTO_MIN_GAP': '0', 'JEV_SYNTHETIC_INPUT': '1', 'JEV_AUTO_ACK': 'yes',
        }
        conf = autoreply.load(lambda key: values.get(key))
        self.assertTrue(conf.auto_fill and conf.auto_send and conf.synthetic)
        self.assertEqual(conf.delay, 60.0)          # 上限
        self.assertEqual(conf.allowlist, ('A', 'B'))
        self.assertEqual(conf.max_risk, 9)
        self.assertEqual(conf.min_gap, 0.0)
        self.assertTrue(conf.acknowledged)

    def test_load_defaults_are_off(self):
        conf = autoreply.load(lambda key: None)
        self.assertFalse(conf.active)
        self.assertFalse(conf.synthetic)
        self.assertEqual(conf.allowlist, ())
        self.assertEqual(conf.delay, 5.0)


class PlanTests(unittest.TestCase):
    def plan(self, conf, **kw):
        args = dict(app_key='wechat', chat_title='家人群', risk=3.0,
                    last_sent_at=None, now=1000.0)
        args.update(kw)
        return autoreply.plan(conf, **args)

    def test_happy_path(self):
        p = self.plan(cfg())
        self.assertTrue(p.fill)
        self.assertFalse(p.send)

    def test_auto_send_requires_the_flag(self):
        self.assertTrue(self.plan(cfg(auto_send=True)).send)

    def test_off_by_default(self):
        p = self.plan(cfg(auto_fill=False))
        self.assertFalse(p.fill)
        self.assertIn('未开启', p.reason)

    def test_only_wechat_for_now(self):
        self.assertFalse(self.plan(cfg(), app_key='telegram').fill)

    def test_synthetic_gate(self):
        p = self.plan(cfg(synthetic=False))
        self.assertFalse(p.fill)
        self.assertIn('合成事件', p.reason)

    def test_empty_allowlist_never_fires(self):
        self.assertFalse(self.plan(cfg(allowlist=())).fill)

    def test_title_matches_after_trimming(self):
        self.assertTrue(self.plan(cfg(allowlist=('File Transfer',)),
                                  chat_title=' File Transfer ').fill)

    def test_unknown_chat_skipped(self):
        self.assertFalse(self.plan(cfg(), chat_title='陌生群').fill)

    def test_risk_threshold(self):
        self.assertFalse(self.plan(cfg(), risk=7).fill)
        self.assertTrue(self.plan(cfg(), risk=6).fill)

    def test_rate_limit(self):
        p = self.plan(cfg(), last_sent_at=990.0, now=1000.0)
        self.assertFalse(p.fill)
        self.assertIn('限频', p.reason)
        self.assertTrue(self.plan(cfg(), last_sent_at=960.0, now=1000.0).fill)


class AllowlistEditTests(unittest.TestCase):
    def test_add_and_remove(self):
        conf = cfg(allowlist=())
        conf = autoreply.with_allowlist(conf, ' 新会话 ')
        self.assertEqual(conf.allowlist, ('新会话',))
        self.assertEqual(autoreply.with_allowlist(conf, '新会话').allowlist, ('新会话',))
        self.assertEqual(autoreply.without_allowlist(conf, '新会话').allowlist, ())

    def test_pipe_in_title_is_refused(self):
        conf = autoreply.with_allowlist(cfg(allowlist=()), 'a|b')
        self.assertEqual(conf.allowlist, ())


if __name__ == '__main__':
    unittest.main()
