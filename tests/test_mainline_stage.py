"""T15 阶段判定的离线单元测试：用人工构造的小样本检验状态机每一步。"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mainline_stage as ms  # noqa: E402


def make_inputs(n=80, market='攻'):
    days = pd.Index([f'2020-01-{i:02d}' if i < 32 else f'2020-02-{i - 31:02d}' if i < 61 else f'2020-03-{i - 60:02d}'
                     for i in range(1, n + 1)], name='date')
    codes = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L']
    frame = lambda v: pd.DataFrame(v, index=days, columns=codes, dtype=float)
    rel = frame(0.0)
    for k, c in enumerate(codes):
        rel[c] = -0.01 * k                      # A 最强，排名 1
    data = {
        '资金_相对沪深300_20': rel,
        '资金_占比5均较20均': frame(0.0),
        '资金_占比60日分位': frame(50.0),
        '资金_占比5日变化': frame(0.001),
        '拥挤_占比一年分位': frame(50.0),
        '拥挤_偏离60日线': frame(0.0),
        '业绩_利润加速占比': frame(np.nan),
        '环境_中国10年20日变化bp': frame(0.0),
    }
    closes = frame(100.0)
    hs = pd.Series(100.0, index=days)
    meta = pd.DataFrame({'行业': codes, '久期': ['中'] * len(codes)}, index=codes)
    return {'scope': 28, 'codes': codes, 'days': days, 'data': data, 'closes': closes, 'hs300': hs,
            'market': pd.Series(market, index=days, dtype=object), 'meta': meta, 'news_major': None,
            'catalysts': pd.DataFrame(columns=['date', '相关行业'])}


class StageMachineTest(unittest.TestCase):
    def setUp(self):
        self.params = ms.load_params(ROOT)

    def milestones(self, events, code):
        return [(e['date'], e['里程碑']) for e in events if e['code'] == code and e.get('里程碑')]

    def test_start_needs_consecutive_days(self):
        inp = make_inputs()
        inp['data']['资金_占比5均较20均'].iloc[10:14, 0] = 0.25   # 只有 4 天
        stages, events = ms.run_machine(inp, self.params)
        self.assertNotIn('启动', set(stages['A']))
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25   # 连续 5 天
        stages, events = ms.run_machine(inp, self.params)
        self.assertEqual(stages['A'].iloc[14], '启动')
        self.assertEqual(stages['A'].iloc[13], '无')

    def test_confirm_after_15_days_with_share_percentile(self):
        inp = make_inputs()
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25
        inp['data']['资金_占比60日分位'].iloc[:, 0] = 80.0
        stages, events = ms.run_machine(inp, self.params)
        # 启动开始于第 10 个位置，满 15 天在第 24 个位置
        self.assertEqual(stages['A'].iloc[23], '启动')
        self.assertEqual(stages['A'].iloc[24], '确认')
        self.assertIn(('确认',), [(m,) for _, m in self.milestones(events, 'A')])

    def test_defensive_market_raises_bar(self):
        inp = make_inputs(market='守')
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25
        inp['data']['资金_占比60日分位'].iloc[:, 0] = 75.0     # 低于 70+10
        stages, _ = ms.run_machine(inp, self.params)
        self.assertNotIn('确认', set(stages['A']))
        stages, _ = ms.run_machine(inp, self.params, gate=False)
        self.assertIn('确认', set(stages['A']))

    def test_crowding_overlay_and_retreat(self):
        inp = make_inputs()
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25
        inp['data']['资金_占比60日分位'].iloc[:, 0] = 80.0
        inp['data']['拥挤_占比一年分位'].iloc[30:35, 0] = 95.0
        # 第 50 天起跌破 20 日线、占比下降，连续 5 天退潮
        inp['closes'].iloc[50:, 0] = 90.0
        inp['data']['资金_占比5日变化'].iloc[50:, 0] = -0.001
        stages, events = ms.run_machine(inp, self.params)
        self.assertEqual(stages['A'].iloc[31], '拥挤')
        self.assertEqual(stages['A'].iloc[40], '确认')
        self.assertEqual(stages['A'].iloc[54], '退潮')
        marks = [m for _, m in self.milestones(events, 'A')]
        self.assertEqual(marks, ['启动', '确认', '拥挤', '退潮'])

    def test_retreat_on_rank_drop(self):
        inp = make_inputs()
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25
        inp['data']['资金_占比60日分位'].iloc[:, 0] = 80.0
        inp['data']['资金_相对沪深300_20'].iloc[40:, 0] = -1.0   # 跌到最后一名
        stages, _ = ms.run_machine(inp, self.params)
        self.assertEqual(stages['A'].iloc[40], '退潮')

    def test_failed_start_returns_to_none(self):
        inp = make_inputs()
        inp['data']['资金_占比5均较20均'].iloc[10:15, 0] = 0.25
        inp['data']['资金_相对沪深300_20'].iloc[16:, 0] = -1.0
        stages, _ = ms.run_machine(inp, self.params)
        self.assertEqual(stages['A'].iloc[20], '无')

    def test_latent_requires_earnings_improvement(self):
        inp = make_inputs()
        inp['data']['拥挤_占比一年分位'].iloc[:, 11] = 20.0      # L 排名最后、不拥挤
        earn = inp['data']['业绩_利润加速占比']
        earn.iloc[:, :] = 0.10
        earn.iloc[30:, 11] = 0.30                                 # L 业绩加速占比提升
        stages, events = ms.run_machine(inp, self.params)
        self.assertEqual(stages['L'].iloc[35], '潜伏')
        self.assertEqual(stages['K'].iloc[35], '无')

    def test_missing_rank_keeps_state(self):
        inp = make_inputs()
        inp['data']['资金_相对沪深300_20'].iloc[20:25, :] = np.nan
        stages, _ = ms.run_machine(inp, self.params)
        self.assertTrue((stages.iloc[20:25] == '无').all().all())

    def test_param_override_validation(self):
        with self.assertRaises(KeyError):
            ms.load_params(ROOT, {'不存在的参数': 1})


if __name__ == '__main__':
    unittest.main()
