"""T13c季度覆盖：已上市分母、未知上市日审计和独立成分快照读取。"""
import csv
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import re

from earnings_history import visibility
from earnings_universe import (BJ, SINA_COMPANY, SW_COMPONENT, THS_COMPANY,
                               candidate_industries, current_legacy_members)
from flow_source import cell

LISTING_VERSION = '同花顺-新浪公司页上市日-v1'
SIDE_FIELDS = ['date', 'code', '行业', '季报期', '规则可见日', '当前成分总量', '已上市分母',
               '未上市家数', '上市日期未知家数', '可评估家数', '加速家数', '季报覆盖率',
               '行业利润加速占比', '说明', '生成时间', '修订原因']
SIDE_LIMIT = 50_000_000


def aware(value):
    moment = dt.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('来源采集时间必须包含时区')
    return moment.astimezone(BJ)


def membership_key(rows):
    return tuple(sorted(str(row['证券代码']).zfill(6) for row in rows))


def read_members(root, legacy=None):
    """完整27候选快照覆盖自身；旧v3范围及其文件不改，来源时间单独保留。"""
    root = Path(root)
    result = {code: dict(value) for code, value in (legacy if legacy is not None else current_legacy_members(root)).items()}
    first = {code: {membership_key(value['rows']): value['date']} for code, value in result.items()}
    names = None
    snapshots = []
    for path in (root / 'data/财报/成分快照').glob('*.json'):
        raw = json.loads(path.read_text())
        stamp = aware(raw['生成时间'])
        if raw.get('状态') != '探测通过' or raw.get('完整候选数') != 27 or raw.get('错误') or raw.get('跨行业重复'):
            raise ValueError('不完整或有错误的候选成分快照：' + str(path))
        snapshots.append((stamp, path, raw))
    latest_at = None
    latest_digest = None
    for stamp, path, raw in sorted(snapshots, key=lambda x: (x[0], str(x[1]))):
        names = names or candidate_industries(root)
        if set(raw['成分']) != set(names):
            raise ValueError('成分快照不是完整27候选集合')
        seen = set()
        business = {}
        for code, value in raw['成分'].items():
            rows = value['rows']
            codes = membership_key(rows)
            if not codes or len(set(codes)) != len(codes) or any(not re.fullmatch(r'\d{6}', c) for c in codes):
                raise ValueError('候选成分代码缺失、重复或不合法')
            if seen & set(codes):
                raise ValueError('候选成分跨行业重复')
            seen.update(codes)
            if value['完整总数'] != len(codes) or value['来源'] != SW_COMPONENT or value['行业'] != names[code]:
                raise ValueError('候选成分数量或来源身份不一致')
            observed = dt.date.fromisoformat(value['快照日期']).isoformat()
            if observed != stamp.date().isoformat():
                raise ValueError('候选成分日期与采集时间不一致')
            business[code] = codes
            signal_date = first.setdefault(code, {}).setdefault(codes, observed)
            result[code] = {'date': observed, 'rows': rows, '来源': value['来源'],
                            '来源快照': str(path.relative_to(root)), '_signal_date': signal_date}
        digest = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
        if stamp == latest_at and digest != latest_digest:
            raise ValueError('同采集时点候选成分快照冲突')
        latest_at, latest_digest = stamp, digest
    return result


def members(root):
    snapshot = read_members(root)
    business = {code: membership_key(value['rows']) for code, value in snapshot.items()}
    universe = sorted({code for codes in business.values() for code in codes})
    digest = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
    return snapshot, universe, digest


def read_listings(root):
    """临时抓取失败不抹去已知上市事实；有效日期的真实修订取最新成功观测。"""
    path = Path(root) / 'data/财报/上市日期.jsonl'
    latest = {}
    if not path.exists():
        return latest
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        code = row['代码']
        if not re.fullmatch(r'\d{6}', code) or row['实现版本'] != LISTING_VERSION:
            raise ValueError('上市日期代码或来源实现版本不符')
        moment = aware(row['采集时间'])
        if row['状态'] not in ('成功', '未知，不进入分母'):
            raise ValueError('上市日期状态不明确')
        if row['来源'] not in (THS_COMPANY.format(code=code), SINA_COMPANY.format(code=code)):
            raise ValueError('上市日期来源不符')
        if row['状态'] != '成功':
            if row.get('上市日期'):
                raise ValueError('未知上市日期记录携带日期值')
            continue
        date = dt.date.fromisoformat(row['上市日期'])
        if date > moment.date() or not row.get('原始日期') or not row.get('来源字段') or not re.fullmatch(r'[0-9a-f]{64}', row.get('响应SHA256', '')):
            raise ValueError('上市日期原始证据或日期边界无效')
        previous = latest.get(code)
        if previous and moment == aware(previous['采集时间']) and previous['上市日期'] != row['上市日期']:
            raise ValueError('同采集时点上市日期发生冲突')
        if previous is None or moment >= aware(previous['采集时间']):
            latest[code] = row
    return latest


def listed_members(codes, period, listings):
    """冻结于所用季度的规则可见日前；不随季度之后的新IPO扩大分母。"""
    codes = sorted(set(codes))
    boundary = dt.date.fromisoformat(visibility(period)[:10])
    eligible, not_yet, unknown = [], [], []
    for code in codes:
        record = listings.get(code)
        if record is None:
            unknown.append(code)
        elif dt.date.fromisoformat(record['上市日期']) < boundary:
            eligible.append(code)
        else:
            not_yet.append(code)
    assert len(codes) == len(eligible) + len(not_yet) + len(unknown)
    return {'当前成分总量': len(codes), '已上市分母': len(eligible), '未上市家数': len(not_yet),
            '上市日期未知家数': len(unknown), '规则可见日': boundary.isoformat(),
            '已上市代码': eligible, '未上市代码': not_yet, '未知代码': unknown}


def plan_coverage_table(root, details, stamp):
    """派生侧表只在值真变化时追加；不在此函数提交，和主信号同事务晋升。"""
    root = Path(root)
    path = root / 'data/财报/覆盖率明细.csv'
    old = path.read_bytes() if path.exists() else b''
    latest = {}
    if old:
        reader = csv.DictReader(io.StringIO(old.decode()))
        if reader.fieldnames != SIDE_FIELDS:
            raise ValueError('覆盖率侧表表头不符，不覆盖')
        latest = {(r['date'], r['code']): r for r in reader}
    compare = [f for f in SIDE_FIELDS if f not in ('生成时间', '修订原因')]
    pending = []
    seen = set()
    for detail in sorted(details, key=lambda r: (r['date'], r['code'])):
        row = {f: cell(detail.get(f)) for f in SIDE_FIELDS}
        key = (row['date'], row['code'])
        if key in seen:
            raise ValueError('覆盖率侧表新结果同键重复')
        seen.add(key)
        dt.date.fromisoformat(row['date'])
        if row['当前成分总量']:
            pieces = [int(row[k]) for k in ('已上市分母', '未上市家数', '上市日期未知家数')]
            if any(n < 0 for n in pieces):
                raise ValueError('覆盖率分母分解不能为负')
            count = sum(pieces)
            if count != int(row['当前成分总量']):
                raise ValueError('覆盖率分母分解恒等式不成立')
            denominator, valid = pieces[0], int(row['可评估家数'])
            passed = int(row['加速家数']) if row['加速家数'] else 0
            if not 0 <= passed <= valid <= denominator:
                raise ValueError('覆盖率侧表可评估/加速家数越界')
            expected = valid / denominator if denominator else None
            ratio = float(row['季报覆盖率']) if row['季报覆盖率'] else None
            if (expected is None) != (ratio is None) or (ratio is not None and not math.isclose(ratio, expected, rel_tol=1e-10, abs_tol=1e-12)):
                raise ValueError('覆盖率侧表比例与已上市分母不一致')
        before = latest.get(key)
        if before and all(row[f] == before[f] for f in compare):
            continue
        row['生成时间'] = stamp
        row['修订原因'] = 'T13c已上市覆盖分母或原始指标变化，保留旧版本'
        latest[key] = row
        pending.append(row)
    if not pending:
        return {}, {'追加行': 0, '整表重算覆盖': False, '总字节': len(old)}
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, SIDE_FIELDS, lineterminator='\n')
    if not old:
        writer.writeheader()
    writer.writerows(pending)
    payload = old + (b'\n' if old and not old.endswith(b'\n') else b'') + out.getvalue().encode()
    overwrite = len(payload) > SIDE_LIMIT
    if overwrite:
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, SIDE_FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(latest[key] for key in sorted(latest))
        payload = out.getvalue().encode()
    return {path: payload}, {'追加行': len(pending), '整表重算覆盖': overwrite, '总字节': len(payload)}
