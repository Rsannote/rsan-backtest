"""T14离线边界：最后修订、40实际日、5日桥接、缺失、时点与人工保护。"""
import csv
import datetime as dt
import io
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mainline_cases as cases
import mainline_cases_data as storage
sys.path.insert(0, str(ROOT / 'tests'))
import verify_t14_public_sources as public_sources


def concurrent_append_worker(path, barrier, index):
    """真实子进程同时追加；放慢replace使无锁实现稳定暴露同读旧版本竞争。"""
    original_write = storage.atomic_text

    def slow_write(target, text):
        time.sleep(.08)
        original_write(target, text)

    storage.atomic_text = slow_write
    row = {k: '' for k in storage.FIELDS}
    row.update({'事件id': '并发事件', '幅度': str(index), '状态': '待中断确认',
                '触发事件': '旧人工判读', '支撑': f'进程{index}支撑'})
    barrier.wait(timeout=15)
    storage.append_records(path, [row], dt.datetime(2026, 10, 30, 20, tzinfo=cases.BJ))


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.names, self.codes, self.candidates = cases.registry(ROOT)
        self.config = cases.parameters(ROOT / 'system/主线案例参数.json')
        self.now = dt.datetime(2026, 10, 30, 20, tzinfo=cases.BJ)

    def write(self, path, fields, rows):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        return target

    def flow(self, days):
        rows = []
        for day in days:
            for i, code in enumerate(self.codes):
                position = self.candidates.index(code) if code in self.candidates else 100
                rows.append({'date': day, 'code': code, '相对沪深300_60': -.01 - position * .01,
                             '占比_28': (28 - i) / 406, '收盘': 100 + i})
        return pd.DataFrame(rows)

    def dates(self, n):
        return pd.bdate_range('2026-01-01', periods=n).strftime('%Y-%m-%d').tolist()

    def test_forty_actual_hits_not_bridge_span_and_formed_only_at_fortieth(self):
        self.assertEqual(cases.episodes([True] * 39 + [False] * 5), [])
        event = cases.episodes([True] * 20 + [False] * 5 + [True] * 20)[0]
        self.assertEqual((event['hits'], event['span'], event['formed']), (40, 45, 44))
        self.assertEqual(event['bridge_false'], 5)
        self.assertEqual(cases.episodes([True] * 20 + [False] * 5 + [True] * 19), [])

    def test_five_bridges_six_confirms_and_does_not_backfill_failure(self):
        one = cases.episodes([True] * 40 + [False] * 5 + [True] * 40)
        self.assertEqual(len(one), 1)
        self.assertEqual((one[0]['hits'], one[0]['span']), (80, 85))
        two = cases.episodes([True] * 40 + [False] * 6 + [True] * 40)
        self.assertEqual(len(two), 2)
        self.assertEqual((two[0]['end'], two[0]['confirmed']), (39, 45))
        self.assertEqual(two[1]['start'], 46)
        self.assertEqual(two[0]['tail_false'], 6)

    def test_tail_five_unconfirmed_six_confirmed_and_no_future_formation(self):
        for length in range(40):
            self.assertEqual(cases.episodes([True] * length), [])
        for gap in range(6):
            event = cases.episodes([True] * 40 + [False] * gap)[0]
            self.assertEqual(event['status'], '待中断确认')
            self.assertIsNone(event['confirmed'])
            self.assertEqual(event['end'], 39)
            self.assertEqual(event['formed'], 39)
        self.assertEqual(cases.episodes([True] * 40 + [False] * 6)[0]['confirmed'], 45)

    def test_unknown_bridge_does_not_count_as_hit_or_prove_exit(self):
        event = cases.episodes([True] * 20 + [None] * 5 + [True] * 20)[0]
        self.assertEqual((event['hits'], event['bridge_unknown'], event['span']), (40, 5, 45))
        self.assertEqual(cases.episodes([True] * 39 + [None] * 5), [])
        event = cases.episodes([True] * 40 + [None] * 6)[0]
        self.assertEqual(event['status'], '数据中断待核实')
        self.assertIsNone(event['confirmed'])
        self.assertEqual(event['tail_unknown'], 6)
        event = cases.episodes([True] * 40 + [False] * 5 + [None])[0]
        self.assertIsNone(event['confirmed'])
        self.assertEqual(cases.episodes([True] * 40 + [None])[0]['status'], '数据不足待确认')

    def test_27_candidate_rank_excludes_comprehensive_new_and_no_positive_filter(self):
        day = '2026-01-01'
        frame = self.flow([day])
        frame.loc[frame.code == '801230', '相对沪深300_60'] = 999
        # 日表已存的含综合旧排名故意设错，程序须忽略它。
        frame['相对强弱排名_60_28'] = 999
        states, _, audit = cases.daily_conditions(frame, [day], self.codes, self.candidates, self.config)
        self.assertTrue(all(states[c][0] is True for c in self.candidates[:3]))
        self.assertEqual(len(states), 27)
        self.assertNotIn('801230', states)
        self.assertTrue(audit[0]['相对完整'])
        self.assertLess(frame.loc[frame.code == self.candidates[0], '相对沪深300_60'].iloc[0], 0)

    def test_top_twenty_percent_means_five_of_27_not_six(self):
        day = '2026-01-01'
        frame = self.flow([day])
        target = self.candidates[5]
        frame.loc[frame.code == target, '相对沪深300_60'] = 10
        # 综合排除以后target占比排名第6，不能ceil(27*.2)取前6。
        states, _, _ = cases.daily_conditions(frame, [day], self.codes, self.candidates, self.config)
        self.assertIs(states[target][0], False)
        other = self.candidates[4]
        frame.loc[frame.code == other, '相对沪深300_60'] = 11
        states, _, _ = cases.daily_conditions(frame, [day], self.codes, self.candidates, self.config)
        self.assertIs(states[other][0], True)

    def test_last_revision_blank_before_filter_and_aggregation_never_falls_back(self):
        day = '2026-01-01'
        frame = self.flow([day])
        original = frame.iloc[0].to_dict()
        revision = dict(original, 相对沪深300_60='')
        rows = frame.to_dict('records') + [dict(original, date='2026-02-01')] + [revision]
        path = self.write('flow.csv', list(frame.columns), rows)
        latest = storage.latest_frame(path)
        self.assertEqual(len(latest), 29)
        self.assertEqual(latest[(latest.date == day) & (latest.code == original['code'])]['相对沪深300_60'].iloc[0], '')
        states, _, audit = cases.daily_conditions(latest, [day], self.codes, self.candidates, self.config)
        self.assertIsNone(states[original['code']][0])
        self.assertFalse(audit[0]['相对完整'])
        # 另一完整排名已证明不成立的行业仍是False，不能全部归为未知。
        self.assertIs(states[self.candidates[-1]][0], False)

    def test_missing_comprehensive_invalidates_share_total_but_not_known_relative_failure(self):
        day = '2026-01-01'
        frame = self.flow([day])
        frame.loc[frame.code == '801230', '占比_28'] = float('nan')
        states, _, audit = cases.daily_conditions(frame, [day], self.codes, self.candidates, self.config)
        self.assertIsNone(states[self.candidates[0]][0])
        self.assertIs(states[self.candidates[3]][0], False)
        self.assertFalse(audit[0]['占比完整'])
        frame.loc[frame.code == '801230', '占比_28'] = .9
        self.assertFalse(cases.daily_conditions(frame, [day], self.codes, self.candidates, self.config)[2][0]['占比完整'])

    def test_missing_whole_date_is_unknown_and_ties_use_code(self):
        days = self.dates(3)
        frame = self.flow([days[0], days[2]])
        frame['相对沪深300_60'] = -1
        states, _, _ = cases.daily_conditions(frame, days, self.codes, self.candidates, self.config)
        self.assertTrue(all(states[c][0] is True for c in self.candidates[:3]))
        self.assertTrue(all(states[c][1] is None for c in self.candidates))

    def test_signal_window_last_revision_twenty_prior_and_future_visibility(self):
        days = self.dates(25)
        code = self.candidates[0]
        rows = [{'date': d, 'code': code, '口径': '28', '资金_成交占比': str(i),
                 '业绩_EPS本年上修占比': '', '环境_中国10年20日变化bp': '4',
                 '环境_中国可见时刻': '2027-01-01T00:00:00+08:00',
                 '环境_大盘攻守': '攻', '市场可见时刻': d + 'T20:00:00+08:00'} for i, d in enumerate(days)]
        rows.append(dict(rows[23], 资金_成交占比=''))
        self.write('data/主线/历史信号表_28.csv', list(rows[0]), rows)
        signals = storage.SignalWindow(self.root, days)
        window = signals.window(24, code)
        self.assertEqual((window['开始'], window['结束'], window['已覆盖交易日']), (days[4], days[23], 20))
        self.assertEqual(window['各字段有效日数']['资金']['资金_成交占比'], 19)
        self.assertIsNone(window['读数'][-1]['原值']['资金_成交占比'])
        self.assertIsNone(window['读数'][0]['原值']['环境_中国10年20日变化bp'])
        self.assertEqual(window['读数'][0]['原值']['环境_大盘攻守'], '攻')
        self.assertEqual(window['各字段有效日数']['业绩预期']['业绩_EPS本年上修占比'], 0)
        self.assertEqual(signals.window(2, code)['已覆盖交易日'], 2)

    def test_signal_reader_overlays_sorted_year_shards_and_latest_blank_28_31(self):
        for scope, base, directory in [(28, '历史信号表_28.csv', '历史信号修订'), (31, '信号表.csv', '信号修订')]:
            with self.subTest(scope=scope):
                fields = ['date', 'code', '口径', '资金_成交占比', '业绩_EPS本年上修占比']
                old = {'date': '2026-01-01', 'code': self.candidates[0], '口径': str(scope),
                       '资金_成交占比': '.1', '业绩_EPS本年上修占比': '.2'}
                path = self.write(f'data/主线/{base}', fields, [old])
                original = path.read_bytes()
                # 故意先创建2027分片；文件读取顺序必须由年名排序确定。
                self.write(f'data/主线/{directory}/2027.csv', fields, [dict(old, date='2027-01-01', 资金_成交占比='.7')])
                self.write(f'data/主线/{directory}/2026.csv', fields,
                           [dict(old, 资金_成交占比='.3'), dict(old, 资金_成交占比='', 业绩_EPS本年上修占比='.4')])
                frame = storage.read_signal_table(self.root, scope)
                self.assertEqual(len(frame), 2)
                latest = frame[frame.date == '2026-01-01'].iloc[0]
                self.assertEqual(latest['资金_成交占比'], '')
                self.assertEqual(latest['业绩_EPS本年上修占比'], '.4')
                paths = storage.signal_paths(self.root, scope)
                self.assertEqual([p.name for p in paths], [base, '2026.csv', '2027.csv'])
                self.assertEqual(len(storage.read_signal_table(self.root, scope, '2026-12-31')), 1)
                self.assertEqual(path.read_bytes(), original)

    def test_signal_window_uses_year_revision_without_changing_condition_source(self):
        days = self.dates(25)
        code = self.candidates[0]
        fields = ['date', 'code', '口径', '资金_成交占比', '业绩_EPS本年上修占比']
        base = [{'date': d, 'code': code, '口径': '28', '资金_成交占比': '.1',
                 '业绩_EPS本年上修占比': ''} for d in days]
        path = self.write('data/主线/历史信号表_28.csv', fields, base)
        before = path.read_bytes()
        self.write('data/主线/历史信号修订/2026.csv', fields,
                   [dict(base[23], 资金_成交占比='', 业绩_EPS本年上修占比='.8')])
        signals = storage.SignalWindow(self.root, days)
        window = signals.window(24, code)
        self.assertIsNone(window['读数'][-1]['原值']['资金_成交占比'])
        self.assertEqual(window['读数'][-1]['原值']['业绩_EPS本年上修占比'], '.8')
        self.assertEqual(window['各字段有效日数']['资金']['资金_成交占比'], 19)
        self.assertIn('历史信号修订/2026.csv', window['来源'])
        self.assertEqual(path.read_bytes(), before)
        # T13只改变附属窗口；核心日条件仍直接取T8，与分片资金值没有关联。
        states, _, _ = cases.daily_conditions(self.flow([days[-1]]), [days[-1]], self.codes, self.candidates, self.config)
        self.assertIs(states[code][0], True)

    def test_signal_reader_no_revision_directory_preserves_base_behavior(self):
        code = self.candidates[0]
        path = self.write('data/主线/历史信号表_28.csv', ['date', 'code', '口径', '资金_成交占比'],
                          [{'date': '2026-01-01', 'code': code, '口径': '28', '资金_成交占比': '.1'},
                           {'date': '2026-01-01', 'code': code, '口径': '28', '资金_成交占比': ''}])
        pd.testing.assert_frame_equal(storage.read_signal_table(self.root, 28).reset_index(drop=True),
                                      storage.latest_frame(path).reset_index(drop=True))
        self.assertEqual(storage.read_signal_table(self.root, 31).shape[0], 0)
        with self.assertRaises(ValueError):
            storage.read_signal_table(self.root, 99)

    def test_price_source_last_empty_does_not_fall_back_to_older_close(self):
        days = self.dates(2)
        code = self.candidates[0]
        self.write(f'data/资金轮动/原始/申万/{code}.csv', ['date', 'close'],
                   [{'date': days[0], 'close': 100}, {'date': days[1], 'close': 110}, {'date': days[0], 'close': ''}])
        prices, _ = storage.price_table(self.root, [code], days)
        self.assertTrue(pd.isna(prices.at[days[0], code]))
        self.assertEqual(prices.at[days[1], code], 110)

    def test_second_is_overall_second_including_self_and_needs_complete_endpoints(self):
        days = self.dates(3)
        codes = self.candidates[:3]
        prices = pd.DataFrame({codes[0]: [100, 120, 150], codes[1]: [100, 110, 130], codes[2]: [100, 100, 105]}, index=days)
        ranked, note = cases.interval_ranking(prices, days, 0, 2, codes, {days[0]: 100, days[2]: 110})
        self.assertEqual(ranked[1][0], codes[1])
        self.assertAlmostEqual(ranked[1][1], .2)
        self.assertIn('含本行业', note)
        prices.at[days[1], codes[2]] = float('nan')
        self.assertEqual(len(cases.interval_ranking(prices, days, 0, 2, codes, {days[0]: 100, days[2]: 110})[0]), 3)
        prices.at[days[2], codes[2]] = float('nan')
        self.assertEqual(cases.interval_ranking(prices, days, 0, 2, codes, {days[0]: 100, days[2]: 110})[0], [])

    def test_append_original_bytes_latest_nonempty_human_and_idempotent(self):
        row = {k: '' for k in storage.FIELDS}
        row.update({'事件id': 'test', '状态': '待中断确认', '计算截止': '2026-10-29', '幅度': .2})
        first = dict(row, 触发事件='人工触发', 支撑='人工支撑', 上位类型='人工上位')
        second = dict(row, 幅度=.3)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, storage.FIELDS, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows([first, second])
        path = self.root / 'cases.csv'
        path.write_bytes(stream.getvalue().encode())
        before = path.read_bytes()
        unchanged = dict(row, 幅度=.3)
        self.assertEqual(storage.append_records(path, [unchanged], self.now), 1)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual(storage.latest_records(path)['test']['支撑'], '人工支撑')
        before = path.read_bytes()
        row['幅度'] = .4
        self.assertEqual(storage.append_records(path, [row], self.now), 1)
        self.assertTrue(path.read_bytes().startswith(before))
        last = storage.latest_records(path)['test']
        self.assertEqual((last['触发事件'], last['支撑'], last['上位类型']), ('人工触发', '人工支撑', '人工上位'))
        before = path.read_bytes()
        self.assertEqual(storage.append_records(path, [row], self.now), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(storage.append_records(path, [], self.now), 1)
        self.assertEqual(storage.latest_records(path)['test']['状态'], '输入修订后不再满足')
        self.assertEqual(storage.append_records(path, [], self.now), 0)
        self.assertEqual(storage.append_records(path, [row], self.now), 1)
        self.assertEqual(storage.latest_records(path)['test']['支撑'], '人工支撑')

    def test_real_concurrent_append_keeps_every_successful_version_and_human(self):
        path = self.root / 'concurrent.csv'
        row = {k: '' for k in storage.FIELDS}
        row.update({'事件id': '并发事件', '幅度': '0', '状态': '待中断确认', '触发事件': '旧人工判读'})
        storage.append_records(path, [row], self.now)
        original = path.read_bytes()
        context = multiprocessing.get_context('fork')
        barrier = context.Barrier(4)
        workers = [context.Process(target=concurrent_append_worker, args=(path, barrier, i)) for i in range(1, 5)]
        for worker in workers:
            worker.start()
        try:
            for worker in workers:
                worker.join(timeout=20)
                self.assertFalse(worker.is_alive(), '并发写锁未释放')
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
        with path.open(newline='') as stream:
            records = list(csv.DictReader(stream))
        self.assertEqual(len(records), 5)
        self.assertEqual({r['幅度'] for r in records}, {'0', '1', '2', '3', '4'})
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertTrue(all(r['触发事件'] == '旧人工判读' for r in records))
        self.assertTrue(records[1]['支撑'])
        self.assertTrue(all(r['支撑'] == records[1]['支撑'] for r in records[1:]))
        self.assertFalse(list(self.root.rglob('*.lock')), '锁不得写到数据/工作树')

    def fixture(self):
        days = self.dates(130)
        for rel in ('system/行业清单.csv', 'system/主线案例参数.json'):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / rel).read_bytes())
        self.write('data/行情库/指数/sh000300.csv', ['date', 'close', 'factor'],
                   [{'date': d, 'close': 100 + i * .1, 'factor': 1} for i, d in enumerate(days)])
        for rel, field in [('data/市场状态/日指标.csv', '成交额亿'), ('data/市场状态/融资余额.csv', '融资余额亿'),
                           ('data/社区情绪/温度计.csv', '温度')]:
            self.write(rel, ['date', field], [{'date': d, field: 100 + i} for i, d in enumerate(days)])
        for name in ('中国10年国债', '美国10年国债'):
            self.write(f'data/宏观/{name}.csv', ['date', 'value', '可见时刻'],
                       [{'date': days[0], 'value': 2, '可见时刻': days[0] + 'T18:00:00+08:00'},
                        {'date': days[-1], 'value': 99, '可见时刻': '2027-01-01T00:00:00+08:00'}])
        frame = self.flow(days[60:])
        self.write('data/资金轮动/日表.csv', list(frame.columns), frame.to_dict('records'))
        for i, code in enumerate(self.codes):
            self.write(f'data/资金轮动/原始/申万/{code}.csv', ['date', 'close'],
                       [{'date': d, 'close': 100 + j * (1 + i / 100)} for j, d in enumerate(days)])
        return days

    def test_end_to_end_actual_prices_public_features_missing_t13_and_repeat_bytes(self):
        days = self.fixture()
        first = cases.run(self.root, now=self.now)
        self.assertEqual(first['案例数'], 3)
        path = self.root / 'data/事件库/主线.csv'
        rows = list(storage.latest_records(path).values())
        row = rows[0]
        self.assertEqual(row['类型'], '主线')
        self.assertEqual((row['开始'], row['成立日期'], row['结束']), (days[60], days[99], days[-1]))
        self.assertEqual(int(row['持续交易日']), 69)
        self.assertEqual(int(row['实际命中交易日']), 70)
        self.assertEqual(row['状态'], '待中断确认')
        self.assertAlmostEqual(float(row['幅度']), 229 / 160 - 1)
        self.assertAlmostEqual(float(row['区间内相对沪深300超额']), 229 / 160 - 1 - ((112.9 / 106) - 1))
        self.assertAlmostEqual(float(row['启动前60日涨幅']), .6)
        self.assertEqual(float(row['启动时中国10年国债']), 2)
        window = json.loads(row['启动前20日五个维度的读数'])
        self.assertEqual(window['有T13行交易日'], 0)
        self.assertEqual(window['已覆盖交易日'], 20)
        self.assertTrue(all(not r[k] for r in rows for k in storage.HUMAN_FIELDS))
        before = {p: (self.root / p).read_bytes() for p in ('data/事件库/主线.csv', 'data/事件库/主线_总表.md',
                                                         'data/资金轮动/日表.csv', 'system/行业清单.csv')}
        second = cases.run(self.root, now=self.now + dt.timedelta(days=1))
        self.assertEqual(second['追加行数'], 0)
        for p, content in before.items():
            self.assertEqual((self.root / p).read_bytes(), content, p)
        with self.assertRaisesRegex(ValueError, '拒绝截短历史'):
            cases.run(self.root, asof=days[110], now=self.now)

    def test_benchmark_latest_revision_applied_before_sort_no_blank_fallback(self):
        days = self.fixture()
        path = self.root / 'data/行情库/指数/sh000300.csv'
        with path.open('a') as stream:
            stream.write(f'{days[0]},101,1\n')
        data = cases.Data(self.root, self.now)
        self.assertEqual(data.values[0], 101)
        with path.open('a') as stream:
            stream.write(f'{days[0]},,1\n')
        with self.assertRaisesRegex(ValueError, '不回退旧值'):
            cases.Data(self.root, self.now)

    def test_global_published_cutoff_cannot_regress_after_all_events_closed(self):
        days = self.fixture()
        path = self.root / 'data/资金轮动/日表.csv'
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        for day in days[100:]:
            for j, code in enumerate(self.candidates):
                frame.loc[(frame.date == day) & (frame.code == code), '相对沪深300_60'] = '100' if j >= 24 else '-999'
        frame.to_csv(path, index=False)
        initial = cases.run(self.root, now=self.now)
        self.assertEqual(initial['状态计数'], {'已确认结束': 3})
        rows = storage.latest_records(self.root / 'data/事件库/主线.csv')
        self.assertLess(max(r['计算截止'] for r in rows.values()), days[115])
        csv_path = self.root / 'data/事件库/主线.csv'
        table_path = self.root / 'data/事件库/主线_总表.md'
        old_csv, old_table = csv_path.read_bytes(), table_path.read_bytes()
        with self.assertRaisesRegex(ValueError, '全局计算截止.*拒绝截短历史'):
            cases.run(self.root, asof=days[115], now=self.now)
        self.assertEqual(csv_path.read_bytes(), old_csv)
        self.assertEqual(table_path.read_bytes(), old_table)
        # 报告被清理时，data总表所有快照中的最高截止仍是保护依据。
        (self.root / 'logs/test/T14/建库报告.json').unlink()
        with self.assertRaisesRegex(ValueError, '全局计算截止.*拒绝截短历史'):
            cases.run(self.root, asof=days[115], now=self.now)
        self.assertEqual(csv_path.read_bytes(), old_csv)
        self.assertEqual(table_path.read_bytes(), old_table)

    def test_registry_and_parameter_guards(self):
        for config in [dict(self.config, 最少实际命中日=0), dict(self.config, 最多桥接交易日=-1),
                       dict(self.config, 相对强弱前几名=True), dict(self.config, 成交占比前比例=0),
                       dict(self.config, 成交占比前比例=True)]:
            path = self.root / 'config.json'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                cases.parameters(path)

    def test_public_document_network_guard_prevents_local_request(self):
        with mock.patch.dict('os.environ', {}, clear=True), mock.patch.object(public_sources.requests, 'get') as request:
            with self.assertRaisesRegex(RuntimeError, '禁止本机联网'):
                public_sources.verify(self.root)
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
