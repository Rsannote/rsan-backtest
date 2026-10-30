"""T13c 成分与上市日：保留当前快照、原字段和请求证据，不推测缺值。"""
import csv
import datetime as dt
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re

BJ = dt.timezone(dt.timedelta(hours=8))
NEW = {'801960', '801970', '801980'}
SW_COMPONENT = 'https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/'
THS_COMPANY = 'https://basic.10jqka.com.cn/new/{code}/company.html'
SINA_COMPANY = 'https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code}.phtml'
COMPONENT_REFERENCE = 'https://github.com/akfamily/akshare/blob/5def33b2d0ba51529e386cb86d7da0c85e5a5626/akshare/index/index_research_sw.py'


def candidate_industries(root):
    """历史28口径只取27个候选，不变更Claude维护的31行业清单。"""
    with (Path(root) / 'system/行业清单.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    selected = {row['申万代码']: row['行业'] for row in rows
                if row['主线判定'] == '参与' and row['申万代码'] not in NEW}
    if len(rows) != 31 or len(selected) != 27 or len({r['申万代码'] for r in rows}) != 31:
        raise ValueError('Claude行业清单不是31行业/27个历史候选')
    return selected


def component_page(obj):
    """沿AkShare index_component_sw的官方字段；总数不明确时不冒称完整。"""
    data = obj.get('data') if isinstance(obj, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('申万成分缺data.results')
    totals = [data[key] for key in ('count', 'total') if key in data]
    if not totals or any(isinstance(v, bool) or not re.fullmatch(r'\d+', str(v)) for v in totals):
        raise ValueError('申万成分总数缺失或无效')
    if len({int(v) for v in totals}) != 1:
        raise ValueError('申万成分总数字段冲突')
    total = int(totals[0])
    if total <= 0:
        raise ValueError('申万成分为空，不能覆盖现有快照')
    rows = []
    for raw in data['results']:
        if not isinstance(raw, dict):
            raise ValueError('申万成分行不是对象')
        code = str(raw['stockcode'])
        if not re.fullmatch(r'\d{6}', code) or not isinstance(raw.get('stockname'), str) or not raw['stockname'].strip():
            raise ValueError('申万成分代码/名称无效')
        beginning = str(raw.get('beginningdate') or '')[:10]
        if beginning:
            dt.date.fromisoformat(beginning)
        # beginningdate是计入指数日期，绝不能当成上市日期。
        weight = raw.get('newweight')
        if weight not in (None, '', '--'):
            try:
                if isinstance(weight, bool) or not math.isfinite(float(weight)):
                    raise ValueError('非有限权重')
            except (ValueError, TypeError) as exc:
                raise ValueError('申万成分权重不是有限数值或明确缺值') from exc
        rows.append({'证券代码': code, '证券名称': raw['stockname'],
                     '最新权重': '' if weight is None else str(weight), '计入日期': beginning})
    if len({r['证券代码'] for r in rows}) != len(rows) or len(rows) > total:
        raise ValueError('申万成分页重复或超出总数')
    return rows, total


class CompanyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.parts = []
        self.title = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
        if tag == 'title':
            self.in_title = False

    def handle_data(self, value):
        if self.hidden:
            return
        self.parts.append(value)
        if self.in_title:
            self.title.append(value)


def listing_date(text, code, source, *, today=None):
    """只读股票公司页明示上市字段，拒绝代码错配、冲突和成立日期替代。"""
    if not re.fullmatch(r'\d{6}', code):
        raise ValueError('上市日期股票代码无效')
    if source not in (THS_COMPANY.format(code=code), SINA_COMPANY.format(code=code)):
        raise ValueError('上市日期来源不在已指定的公司页')
    parser = CompanyText()
    parser.feed(text)
    title = ' '.join(parser.title)
    if not re.search(r'(?<!\d)' + code + r'(?!\d)', title):
        raise ValueError('公司页标题未验证目标股票身份')
    visible = ' '.join(' '.join(parser.parts).split())
    # 必须捕获整个市场标签；不能从“B股上市日期”的“上市”处重新开始。
    pattern = re.compile(r'(?<!\w)(?P<label>(?:(?P<market>[A-Za-z])\s*股\s*)?上市(?:日期|时间))'
                         r'\s*[：:]?\s*(?P<date>\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)'
                         r'(?![\d年月日/.\-])', re.I)
    market = 'B' if code.startswith(('200', '900')) else 'A'
    found = []
    for match in pattern.finditer(visible):
        if match['market'] and match['market'].upper() != market:
            continue
        raw = match['date']
        parts = re.findall(r'\d+', raw)
        value = dt.date(*map(int, parts)).isoformat()
        if dt.date.fromisoformat(value) > (today or dt.datetime.now(BJ).date()):
            raise ValueError('上市日期在未来，不能当已上市')
        found.append((value, raw, match['label'], visible[max(0, match.start() - 20):match.end() + 30]))
    if not found:
        raise ValueError('公司页缺少明确的上市日期/上市时间字段')
    if len({x[0] for x in found}) != 1:
        raise ValueError('公司页上市日期有冲突，不静默选择A/B股或其他日期')
    value, raw, field, excerpt = found[0]
    return {'代码': code, '上市日期': value, '原始日期': raw, '来源字段': field,
            '来源': source, '来源片段': excerpt}


def current_legacy_members(root):
    """原v3快照只读；T13c另存版本，不能扩大业绩加速或T12b的范围。"""
    path = Path(root) / 'data/v3/申万成分.json'
    return json.loads(path.read_text()) if path.exists() else {}
