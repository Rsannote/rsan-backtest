#!/usr/bin/env python3
"""T6 宏观来源只读探测；联网仅限 Actions，产出仅 logs/宏观探测.txt。

本地可运行 --self-test。HTTP 200、网页可达与有效时间序列分开记录；
抓取日、统计期、真实发布日期不互相替代。所有来源仍待审核。
接口参数参考 AKShare 的 bond_china.py、bond_em.py、macro_china.py 源码，
直接请求来源，不安装或执行 AKShare。仅使用 Python 标准库。
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'logs/宏观探测.txt'
START = '2015-01-01'
UA = 'Mozilla/5.0 (compatible; RsanMacroProbe/1.0)'
MAX_BYTES = 8 * 1024 * 1024


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('数值缺失或类型不符')
    result = float(str(value).strip().replace(',', '').rstrip('%'))
    if not math.isfinite(result):
        raise ValueError('非有限数值')
    return result


def day(value):
    text = str(value).strip()
    if re.fullmatch(r'\d{6}', text):
        return datetime.strptime(text, '%Y%m').strftime('%Y-%m')
    if re.fullmatch(r'\d{4}-\d{2}', text):
        datetime.strptime(text, '%Y-%m')
        return text
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f'日期无法识别：{text[:40]}')


def normalized(rows):
    """不填零、不补日期；重复日期冲突直接失败。"""
    result = {}
    today = datetime.now(BJ).date().isoformat()
    for raw_day, raw_value in rows:
        d, v = day(raw_day), number(raw_value)
        if d > today:
            raise ValueError(f'来源含未来观测：{d}')
        if d in result and result[d] != v:
            raise ValueError(f'同日冲突：{d}')
        result[d] = v
    if not result:
        raise ValueError('没有可用日期和数值；空返回不算成功')
    return dict(sorted(result.items()))


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def china_bond(text):
    table = Tables()
    table.feed(text)
    index = None
    result = []
    for row in table.rows:
        if '10年' in row and '日期' in row:
            index = (row.index('日期'), row.index('10年'))
            continue
        if index and len(row) > max(index):
            # 明确只取国债到期收益率，禁止混入国开债/即期曲线。
            label = '|'.join(row[:index[0]])
            if '国债收益率曲线' in label and not any(x in label for x in ('即期', '远期')):
                result.append((row[index[0]], row[index[1]]))
    return normalized(result)


def csv_series(text, value_key, date_key):
    rows = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not rows.fieldnames or value_key not in rows.fieldnames or date_key not in rows.fieldnames:
        raise ValueError(f'CSV 缺少 {date_key}/{value_key}，字段={rows.fieldnames}')
    return normalized((r[date_key], r[value_key]) for r in rows
                      if str(r.get(value_key, '')).strip() not in ('', '.', 'N/A', 'ND'))


def em_series(text, value_key, date_key):
    obj = json.loads(text)
    records = (obj.get('result') or {}).get('data') or []
    return normalized((r[date_key], r[value_key]) for r in records
                      if r.get(value_key) is not None)


def yahoo_series(text):
    obj = json.loads(text)
    data = obj['chart']['result'][0]
    if data['meta'].get('exchangeTimezoneName') is None:
        raise ValueError('缺少交易所时区')
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(data['meta']['exchangeTimezoneName'])
    return normalized((datetime.fromtimestamp(t, tz).date().isoformat(), v)
                      for t, v in zip(data['timestamp'], data['indicators']['quote'][0]['close'])
                      if v is not None)


def request(url, trace, deadline, data=None):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('联网探测只能在 GitHub Actions 运行')
    if time.monotonic() >= deadline - 20:
        raise TimeoutError('本轮总预算已用尽，余下来源保留未完成状态')
    time.sleep(1)
    item = {'网址': url, '方法': 'POST' if data is not None else 'GET',
            '北京时间': datetime.now(BJ).isoformat()}
    trace.append(item)
    began = time.monotonic()
    try:
        headers = {'User-Agent': UA, 'Accept': '*/*'}
        if 'chinabond' in url:
            headers['Referer'] = 'https://yield.chinabond.com.cn/'
        if 'chinamoney' in url:
            headers['Referer'] = 'https://www.chinamoney.com.cn/'
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=18) as response:
            item.update(状态码=response.status, 最终网址=response.url,
                        内容类型=response.headers.get('Content-Type'),
                        Last_Modified=response.headers.get('Last-Modified'))
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('响应超过8MiB，停止读取，不将截断内容作为成功')
            encoding = response.headers.get_content_charset() or 'utf-8'
        text = raw.decode(encoding, errors='replace')
        item.update(字节数=len(raw), SHA256=hashlib.sha256(raw).hexdigest(),
                    响应开头=re.sub(r'\s+', ' ', text[:650]))
        return text
    except Exception as exc:
        item['错误'] = f'{type(exc).__name__}: {exc}'
        if isinstance(exc, urllib.error.HTTPError):
            item['状态码'] = exc.code
        raise
    finally:
        item['耗时秒'] = round(time.monotonic() - began, 3)


def url(base, **params):
    return base + '?' + urllib.parse.urlencode(params)


def series_summary(values):
    keys = list(values)
    years = sorted(set(k[:4] for k in keys))
    return {'有效条数': len(values), '最早观测': keys[0], '最新观测': keys[-1],
            '距抓取日天数': ((datetime.now(BJ).date() - date.fromisoformat(keys[-1])).days
                           if len(keys[-1]) == 10 else None),
            '实取年份': years, '2015年有数据': '2015' in years,
            '各年条数': {y: sum(k.startswith(y) for k in keys) for y in years},
            '样例': [{'date': k, 'value': values[k]} for k in
                     dict.fromkeys((keys[0], keys[len(keys) // 2], keys[-1]))],
            '至少21个日频观测': len(keys) >= 21 and all(len(k) == 10 for k in keys)}


def self_test():
    class Checks(unittest.TestCase):
        def test_missing_and_zero(self):
            for v in (None, '--', '.', 'NaN', 'inf', True):
                with self.assertRaises((ValueError, TypeError)):
                    number(v)
            self.assertEqual(number(0), 0)
            with self.assertRaises(ValueError):
                normalized([])

        def test_treasury_exact_ten_year(self):
            self.assertEqual(csv_series('Date,1 Mo,10 Yr\n01/05/2015,0.02,2.04\n',
                                        '10 Yr', 'Date'), {'2015-01-05': 2.04})

        def test_china_curve_identity(self):
            html = '<table><tr><th>曲线名称</th><th>日期</th><th>10年</th></tr>'
            for name, v in [('中债国开债收益率曲线', 4), ('中债国债收益率曲线', 3.6)]:
                html += f'<tr><td>{name}</td><td>2015-01-05</td><td>{v}</td></tr>'
            self.assertEqual(china_bond(html + '</table>'), {'2015-01-05': 3.6})

        def test_empty_error_and_conflicting_dates(self):
            with self.assertRaises(ValueError):
                em_series('{"success":false,"result":null}', 'X', 'date')
            with self.assertRaises(ValueError):
                normalized([('2015-01-05', 3), ('2015-01-05', 4)])
            with self.assertRaises(ValueError):
                day('2015-13-01')

        def test_month_is_not_publication_date(self):
            self.assertEqual(day('201501'), '2015-01')

        def test_network_guard(self):
            from unittest.mock import patch
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
                with self.assertRaises(RuntimeError):
                    request('https://example.invalid', [], time.monotonic() + 100)

    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks)).wasSuccessful()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', help='仅离线检查解析与联网保护')
    args = parser.parse_args()
    if args.self_test:
        return 0 if self_test() else 1
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('联网探测只能在 GitHub Actions 运行；本地请用 --self-test')
    today = datetime.now(BJ).date()
    deadline = time.monotonic() + 660
    run = (f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/"
           f"{os.environ.get('GITHUB_RUN_ID')}")
    report = {'任务': 'T6 宏观数据来源探测，待审核后另案接入',
              '北京时间': datetime.now(BJ).isoformat(), 'Actions': run,
              '源提交': os.environ.get('GITHUB_SHA'), '程序完成': False, '已批准接入': False,
              '范围': '只写本日志；未接入 data/宏观、生产工作流或主线雷达',
              '口径': ['数据日期为来源观测日；美债为美国当地日期；月度为统计期。',
                       '抓取时间及HTTP Last-Modified不能代替真实发布日期。',
                       '同一运行重复请求只能说明短时可重复，不能证明跨日稳定。',
                       '2015/2020/近期抽样或首尾跨度不能证明完整历史；各年条数另列。',
                       '本次历史序列可能含修订值；无历史版本不能宣称时点可见。',
                       '主线环境维度后续需按可见时刻对齐中美交易日，禁止前视填充。'],
              '探测': [], '中美国债交叉核对': []}
    cache = {}
    OUT.parent.mkdir(parents=True, exist_ok=True)

    def save():
        OUT.write_text('T6 宏观数据源探测（北京时间）\n' +
                       json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def probe(name, indicator, level, unit, frequency, fn, note='', repeat=1):
        entry = {'来源': name, '指标': indicator, '级别': level, '单位': unit,
                 '频率': frequency, '限制': note, '请求': [], '复测': [],
                 '真实发布日期': '未取得；不可用观测日期或抓取时间冒充',
                 '状态': '进行中'}
        report['探测'].append(entry)
        save()
        got = None
        for n in range(repeat):
            try:
                values, extra = fn(entry['请求'])
                if not values:
                    entry.update(extra)
                    entry['复测'].append({'轮次': n + 1, '状态': '仅页面或结构可达，未取得指标序列'})
                else:
                    summary = series_summary(values)
                    entry['复测'].append({'轮次': n + 1, '状态': '取得有效观测', **summary})
                    entry.update(extra)
                    got = values
                    cache[name] = values
            except Exception as exc:
                entry['复测'].append({'轮次': n + 1, '状态': '失败',
                                      '错误': f'{type(exc).__name__}: {exc}'})
            save()
        success = sum(x['状态'] == '取得有效观测' for x in entry['复测'])
        entry['有效序列成功次数'] = f'{success}/{repeat}'
        entry['状态'] = ('取得有效观测（来源待审核）' if success == repeat else
                       '部分成功' if success else '未取得指标序列')
        if got:
            entry.update(series_summary(got))
        save()
        print(f"{indicator} / {name}：{entry['状态']}，{success}/{repeat}", flush=True)
        return got

    def get(u, trace, data=None):
        return request(u, trace, deadline, data)

    def china(trace):
        values, windows = {}, []
        # 每个自然年分开请求，符合中债每段不超过一年的限制。
        for year in range(2015, today.year + 1):
            end = min(today, date(year, 12, 31)).isoformat()
            u = url('https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery',
                    startDate=f'{year}-01-01', endDate=end, gjqx='0', qxId='ycqx', locale='cn_ZH')
            chunk = china_bond(get(u, trace))
            values.update(chunk)
            windows.append([f'{year}-01-01', end])
        return normalized(values.items()), {'实际请求窗口': windows,
                '数据口径': '中债国债到期收益率曲线，10年，百分数；未使用债券价格代理'}

    probe('中债官方历史', '中国10年国债收益率', '一级', '%', '日', china,
          '按年请求2015至今，任一窗口失败则不能认定全历史已取得')

    def china_recent(trace):
        u = url('https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery',
                startDate=(today - timedelta(days=90)).isoformat(), endDate=today.isoformat(),
                gjqx='0', qxId='ycqx', locale='cn_ZH')
        return china_bond(get(u, trace)), {'窗口': '最近90自然日，三次独立请求同一URL'}

    probe('中债官方近期复测', '中国10年国债收益率', '一级', '%', '日', china_recent, repeat=3)

    def treasury(trace):
        values = {}
        for year in range(2015, today.year + 1):
            u = url(f'https://home.treasury.gov/resource-center-data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all',
                    type='daily_treasury_yield_curve', field_tdr_date_value=str(year), _format='csv')
            values.update(csv_series(get(u, trace), '10 Yr', 'Date'))
        return normalized(values.items()), {'数据口径': '美国财政部10年恒定期限国债收益率，非期货价格',
                                           '真实发布日期': '日表无逐行公布时刻，需另查美东发布安排；不能当日A股盘中已知'}

    probe('美国财政部官方历史', '美国10年国债收益率', '一级', '%', '日', treasury)

    def fred(code):
        def fetch(trace):
            u = url('https://fred.stlouisfed.org/graph/fredgraph.csv', id=code, cosd=START)
            text = get(u, trace)
            head = next(csv.reader(io.StringIO(text)))
            date_key = 'observation_date' if 'observation_date' in head else 'DATE'
            return csv_series(text, code, date_key), {'数据口径': code,
                    '真实发布日期': '观测日可取，逐行实际公开日期/时刻未取得；尚不具备时点数据保证'}
        return fetch

    probe('FRED_DGS10', '美国10年国债收益率', '一级汇集（原始来源美国财政部）', '%', '日',
          fred('DGS10'), 'FRED与财政部非独立原始数据源；重复三次只验证本轮可重复', repeat=3)

    def em(report_name, key, date_key='REPORT_DATE'):
        def fetch(trace):
            params = dict(reportName=report_name, columns='ALL', pageSize='500',
                          pageNumber='1', sortColumns=date_key, sortTypes='-1', source='WEB', client='WEB')
            base = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
            text = get(url(base, **params), trace)
            obj = json.loads(text)
            pages = int((obj.get('result') or {}).get('pages') or 0)
            values = em_series(text, key, date_key)
            # 硬上限16页；超过或未到2015年明确标注截断。
            for page in range(2, min(pages, 16) + 1):
                if min(values) <= START:
                    break
                params['pageNumber'] = str(page)
                values.update(em_series(get(url(base, **params), trace), key, date_key))
            return normalized(values.items()), {'数据字段': key, '总页数': pages,
                    '首条字段': list((obj.get('result') or {}).get('data', [{}])[0]),
                    '真实发布日期': 'REPORT_DATE/SOLAR_DATE为统计期/观测日，未核验真实公布日；不能用于时点回放'}
        return fetch

    for label, key in [('中国10年国债收益率', 'EMM00166466'), ('美国10年国债收益率', 'EMG00001310')]:
        probe('东财_' + label, label, '二级', '%', '日',
              em('RPTA_WEB_TREASURYYIELD', key, 'SOLAR_DATE'),
              '仅候选备用；服务器访问可能失败；未作为生产主源', repeat=2)

    def inspect(u, keywords=(), data=None):
        def fetch(trace):
            text = get(u, trace, data)
            try:
                obj = json.loads(text)
                detail = {'顶层结构': list(obj)[:25] if isinstance(obj, dict) else type(obj).__name__,
                          '结构样例': str(obj)[:1300]}
            except (ValueError, TypeError):
                links = re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</a>', text, re.S | re.I)
                detail = {'相关链接': [{'网址': urllib.parse.urljoin(u, href),
                                       '标题': re.sub('<[^>]+>', '', title).strip()[:150]}
                                      for href, title in links
                                      if not keywords or any(k in title for k in keywords)][:12]}
            return None, {**detail, '判定': '只证明页面/接口结构可达，尚未解析指标，不算数据源成功'}
        return fetch

    probe('外汇交易中心', '中国10年国债收益率', '一级', '%', '日', inspect(url(
        'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/ClsYldCurvHis',
        lang='CN', reference='1,2,3', bondType='CYCC000',
        startDate=(today-timedelta(days=7)).isoformat(), endDate=today.isoformat(),
        termId='1', pageNum='1', pageSize='500')), '字段/分页/10年期限待核实；不绕过登录或验证码')
    for label, u in [('DR007', 'https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/currency/bkrr-cny.json'),
                     ('Shibor', 'https://www.shibor.org/r/cms/www/shibor/data/shibor.json'),
                     ('美元兑人民币', 'https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json')]:
        probe('官方_' + label, label, '一级', '%' if label != '美元兑人民币' else '人民币/美元',
              '日', inspect(u), '本轮先核实返回结构，未核实字段前不把其他期限/币种算作目标')
    probe('FRED_DEXCHUS', '美元兑人民币', '一级汇集（美联储H.10）', '人民币/美元', '日',
          fred('DEXCHUS'), '美联储纽约午间汇率，不能与人民币中间价混用')

    probe('人民银行统计发布', '社融、M1/M2', '一级', '待核实', '月', inspect(
        'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html',
        ('社会融资', '金融统计', '货币')), '需沿官方公告提取真实发布日期及原始口径；本轮仅记录发现入口')
    probe('统计局官方发布', 'CPI、PPI、制造业PMI', '一级', '待核实', '月', inspect(
        'https://www.stats.gov.cn/sj/zxfb/', ('居民消费', '工业生产者', '采购经理')), '网页发布日期不自动赋给全部历史观测')
    probe('统计局新站目录', 'CPI、PPI、制造业PMI', '一级', '待核实', '月', inspect(url(
        'https://data.stats.gov.cn/dg/website/publicrelease/web/external/new/queryIndexTreeAsync',
        pid='', code='1')), '目录能访问也不代表取得数值或逐条发布日期')
    for label, report_name, key, unit in [
        ('M1', 'RPT_ECONOMY_CURRENCY_SUPPLY', 'CURRENCY', '亿元'),
        ('M2', 'RPT_ECONOMY_CURRENCY_SUPPLY', 'BASIC_CURRENCY', '亿元'),
        ('CPI同比', 'RPT_ECONOMY_CPI', 'NATIONAL_SAME', '%'),
        ('PPI同比', 'RPT_ECONOMY_PPI', 'BASE_SAME', '%'),
        ('制造业PMI', 'RPT_ECONOMY_PMI', 'MAKE_INDEX', '指数点')]:
        probe('东财_' + label, label, '二级', unit, '月', em(report_name, key),
              '缺真实发布日期则禁止历史回放；M1统计口径变更需审核后标注')
    probe('商务部统计接口', '社融增量', '官方转引', '待核实', '月', inspect(
        'https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery', data={}),
        '以人民银行为原始来源；字段单位和发布日期尚待核实')

    def yahoo(symbol):
        def fetch(trace):
            stamp = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
            u = url('https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol, safe=''),
                    period1=stamp, period2=int(time.time()), interval='1d')
            text = get(u, trace)
            meta = json.loads(text)['chart']['result'][0]['meta']
            if meta.get('symbol') != symbol:
                raise ValueError(f'返回的标的代码不符：{meta.get("symbol")}')
            return yahoo_series(text), {'标的元信息': {k: meta.get(k) for k in
                    ('symbol', 'shortName', 'longName', 'currency', 'exchangeTimezoneName', 'instrumentType')},
                    '真实发布日期': '当地观测日；收盘可见时刻未取得，未完成合约连续性与授权核实'}
        return fetch

    for label, symbol, unit in [('美元指数', 'DX-Y.NYB', '指数点'),
                                ('布伦特原油期货', 'BZ=F', '美元/桶'),
                                ('COMEX黄金期货', 'GC=F', '美元/盎司')]:
        probe('Yahoo_' + symbol, label, '二级', unit, '日', yahoo(symbol),
              '期货连续序列换月口径待审核，不能当成现货或固定到期合约')
    probe('伦敦金属交易所', 'LME铜', '一级', '美元/吨（待字段核实）', '日', inspect(
        'https://www.lme.com/en/Metals/Non-ferrous/LME-Copper', ('Copper', 'copper')),
        '免费历史范围和现货/3月合约定义待核实；付费或登录限制不绕过')

    def shfe(trace):
        text = get(url('https://stock2.finance.sina.com.cn/futures/api/json.php/IndexService.getInnerFuturesDailyKLine', symbol='CU0'), trace)
        records = json.loads(text)
        return normalized((r['date'], r['close']) for r in records), {
            '数据口径': '沪铜CU0连续序列，收盘价；换月拼接规则待核实'}
    probe('新浪沪铜', '沪铜', '二级', '人民币/吨', '日', shfe,
          '沪铜与LME铜分别记录；不能用COMEX铜替代LME铜')

    for label, a, b in [
        ('中国10年国债', '中债官方历史', '东财_中国10年国债收益率'),
        ('美国10年国债', '美国财政部官方历史', 'FRED_DGS10')]:
        common = sorted(set(cache.get(a, {})) & set(cache.get(b, {})))
        check = {'指标': label, '来源甲': a, '来源乙': b,
                 '说明': '同一观测日同单位原始值直接相减；不自动判定容差或采用来源'}
        if len(common) >= 3:
            chosen = [common[0], common[len(common)//2], common[-1]]
            check['核对'] = [{'日期': d, '甲': cache[a][d], '乙': cache[b][d],
                              '差值_百分点': round(cache[a][d] - cache[b][d], 8)} for d in chosen]
        else:
            check['缺口'] = '两个来源共同有效日期不足3个，未完成三时点官方核对'
        report['中美国债交叉核对'].append(check)
    report['结束北京时间'] = datetime.now(BJ).isoformat()
    report['程序完成'] = True
    report['有效来源数'] = sum(x['有效序列成功次数'].split('/')[0] != '0' for x in report['探测'])
    report['审核事项'] = ['中美10年收益率来源、完整历史、短时复测与逐时可见性是否足以接入环境维度。',
                          '其余来源的单位、合约定义、修订历史和真实发布日期仍须逐项核实。',
                          '本次仅探测；T6正式CSV、最新表、更新计划及运行清单留待探测PR审核通过。']
    save()
    # 数据源拒绝属于探测结果；程序完成与来源验收分别记录，全空则失败。
    print('探测已完成，结果写入 logs/宏观探测.txt；来源失败和验收缺口见日志。', flush=True)
    return 0 if report['有效来源数'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
