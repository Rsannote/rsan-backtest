"""T14专用的最后版本读取、五维窗口与只追加案例存储。全程离线。"""
import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
from contextlib import contextmanager

import pandas as pd

from auto_judgment_common import atomic_text, read_csv
from build_events import PUBLIC, HUMAN, EXTRA, cell, Data as T9Data, finite

NEW = {'801960', '801970', '801980'}
HUMAN_FIELDS = list(dict.fromkeys(HUMAN + ['上位类型', '支撑']))
CASE_FIELDS = ['行业', '申万代码', '区间内相对沪深300超额', '启动前20日五个维度的读数',
               '启动时大盘攻守', '同期第二强的行业', '同期第二强代码', '同期第二强相对超额',
               '同期第二强说明', '实际命中交易日', '桥接跨度交易日', '桥接已知失败日',
               '桥接未知日', '尾部已知失败日', '尾部未知日', '识别来源', '参数', '上位类型', '支撑']
FIELDS = PUBLIC + EXTRA + CASE_FIELDS
DIMENSIONS = {
    '环境适配': ('环境_',), '业绩预期': ('业绩_',), '热度催化': ('热度_',),
    '资金': ('资金_',), '拥挤度': ('拥挤_',),
}


@contextmanager
def write_lock(identity):
    """临时目录中的进程互斥锁；覆盖完整读-比-写，不在data生成锁文件。"""
    directory = Path(tempfile.gettempdir()) / 'rsan-t14-locks'
    directory.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(str(Path(identity).resolve()).encode('utf-8')).hexdigest()
    # 不删除锁文件，避免等待者持有旧inode、后来者新建另一把锁的竞争。
    with (directory / (key + '.lock')).open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def latest_frame(path, keys=('date', 'code')):
    """文件顺序的最后修订优先，必须先去重，再排序、筛选、转数值。"""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    if not set(keys).issubset(frame.columns):
        raise ValueError(f'{path}缺主键{keys}')
    return frame.drop_duplicates(list(keys), keep='last')


def signal_paths(root, scope=28):
    """T13b约定的只读输入顺序；不依赖尚未合并的存储helper。"""
    if scope not in (28, 31):
        raise ValueError('主线信号口径只能为28或31')
    base = Path(root) / 'data/主线'
    original = base / ('历史信号表_28.csv' if scope == 28 else '信号表.csv')
    shards = base / ('历史信号修订' if scope == 28 else '信号修订')
    return ([original] if original.exists() else []) + sorted(shards.glob('[0-9][0-9][0-9][0-9].csv'))


def read_signal_table(root, scope=28, asof=None):
    """基表后叠加排序年度分片，最后空值也生效；筛选只能在跨文件去重之后。"""
    frames = []
    for path in signal_paths(root, scope):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
        if not {'date', 'code'}.issubset(frame.columns):
            raise ValueError(f'{path}缺主键date/code')
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=['date', 'code'])
    # 不对各文件按日期重排，不以非空聚合；一行修订整体替代同键旧行。
    result = pd.concat(frames, ignore_index=True, sort=False).fillna('')
    result = result.drop_duplicates(['date', 'code'], keep='last')
    if '口径' in result and any(result['口径'] != str(scope)):
        raise ValueError(f'T13信号表混入非{scope}行业口径')
    if asof:
        result = result[result.date <= asof]
    return result


def numeric(series):
    values = pd.to_numeric(series, errors='coerce')
    return values.where(values.map(lambda x: pd.notna(x) and math.isfinite(x)))


def clean(value):
    if value is None or str(value).strip() in ('', 'nan', 'NaN', 'None'):
        return None
    return str(value)


def price_table(root, codes, days):
    """独立读取已存申万价格日线；不回填T8条件或取旧修订代替最后空值。"""
    series = {}
    paths = []
    for code in codes:
        path = Path(root) / 'data/资金轮动/原始/申万' / (code + '.csv')
        paths.append(path)
        if path.exists():
            frame = latest_frame(path, ('date',))
            series[code] = numeric(frame.set_index('date')['close'])
        else:
            series[code] = pd.Series(dtype=float)
    return pd.DataFrame(series).reindex(index=days, columns=codes), paths


def visible(stamp, cutoff):
    try:
        value = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    return value.utcoffset() is not None and value <= cutoff


class CaseData(T9Data):
    """复用T9只读特征方法；基准价格先取最后修订，不改T9已有拒重约定。"""
    def __init__(self, root, now):
        self.root = Path(root)
        self.now = now
        self.paths = {}
        path = self.root / 'data/行情库/指数/sh000300.csv'
        frame = latest_frame(path, ('date',))
        self.add_path(path)
        latest_day = now.date() if now.time() >= dt.time(18) else now.date() - dt.timedelta(days=1)
        selected = {}
        for row in frame.to_dict('records'):
            day = dt.date.fromisoformat(row['date']).isoformat()
            if day < '2005-01-01' or day > latest_day.isoformat():
                continue
            close, factor = finite(row.get('close')), finite(row.get('factor'))
            if close is None or factor is None or close <= 0 or factor <= 0:
                raise ValueError('沪深300最后修订close/factor无效，不回退旧值')
            selected[day] = close * factor
        self.days = sorted(selected)
        self.values = [selected[d] for d in self.days]
        self.index = {d: i for i, d in enumerate(self.days)}
        if not self.days:
            raise ValueError('无已收尾沪深300行情')
        # 以下字段与T9Data.features的已审核接口一致；宏观可见时刻由原方法逐条过滤。
        self.amount = self.load('data/市场状态/日指标.csv', '成交额亿')
        self.margin = self.load('data/市场状态/融资余额.csv', '融资余额亿')
        self.temperature = self.load('data/社区情绪/温度计.csv', '温度')
        self.macro = {}
        for name in ('中国10年国债', '美国10年国债'):
            path = self.root / 'data/宏观' / (name + '.csv')
            self.add_path(path)
            rows = read_csv(path)
            if not rows:
                raise ValueError('宏观序列为空：' + name)
            for row in rows:
                if finite(row['value']) is None:
                    raise ValueError('宏观序列数值缺失：' + name)
                dt.date.fromisoformat(row['date'])
                if dt.datetime.fromisoformat(row['可见时刻']).utcoffset() is None:
                    raise ValueError('宏观可见时刻缺时区')
            self.macro[name] = rows
        self.cache = {}


class SignalWindow:
    """只读已合并的T13历史28表；取T-20..T-1逐日原值，不填补或取平均。"""
    def __init__(self, root, days):
        self.days = days
        self.path = Path(root) / 'data/主线/历史信号表_28.csv'
        self.paths = signal_paths(root, 28)
        self.rows = {}
        self.columns = []
        frame = read_signal_table(root, 28)
        if not frame.empty:
            self.columns = [c for c in frame.columns if any(c.startswith(p) for v in DIMENSIONS.values() for p in v)
                            and not c.endswith(('_有效排名', '_全排名', '_有效行业数'))]
            # T13状态与可见性字段一并保留，原始数字保持来源字符串精度。
            self.columns += [c for c in ('环境适配_状态', '业绩预期_状态', '热度催化_状态',
                                        '资金_状态', '拥挤度_状态', '市场可见时刻', '社区可见时刻',
                                        '缺失说明', '使用时点说明') if c in frame]
            self.columns = list(dict.fromkeys(self.columns))
            self.rows = {(r['date'], r['code']): r for r in frame.to_dict('records')}

    def row(self, day, code):
        raw = self.rows.get((day, code))
        if raw is None:
            return None, ['T13该日行业缺行']
        # T13为日末指标，本案例窗口也以各观测日23:59:59为界；公共T9特征另以18:00为界。
        cutoff = dt.datetime.fromisoformat(day + 'T23:59:59+08:00')
        out = {k: clean(raw.get(k)) for k in self.columns}
        notes = []
        for label in ('中国', '美国'):
            stamp = raw.get('环境_' + label + '可见时刻')
            keys = ['环境_' + label + '10年20日变化bp', '环境_' + label + '久期适配bp']
            if any(out.get(k) is not None for k in keys) and not visible(stamp, cutoff):
                for key in keys:
                    out[key] = None
                notes.append(label + '宏观可见时刻缺失或晚于观测日，屏蔽数值')
        if out.get('环境_大盘攻守') is not None and not visible(raw.get('市场可见时刻'), cutoff):
            out['环境_大盘攻守'] = None
            notes.append('市场状态实际可见时刻缺失或晚于观测日，屏蔽攻守')
        if out.get('热度_概念出现次数') is not None and not visible(raw.get('社区可见时刻'), cutoff):
            out['热度_概念出现次数'] = None
            notes.append('社区实际可见时刻缺失或晚于观测日，屏蔽概念数')
        return out, notes

    def window(self, start, code):
        dates = self.days[max(0, start - 20):start]
        rows = []
        for day in dates:
            row, notes = self.row(day, code)
            rows.append({'date': day, '原值': row, '可见性说明': notes})
        counts = {}
        for name, prefixes in DIMENSIONS.items():
            cols = [c for c in self.columns if c.startswith(prefixes) and '可见时刻' not in c]
            counts[name] = {c: sum(r['原值'] is not None and r['原值'].get(c) is not None for r in rows)
                            for c in cols}
        source = 'data/主线/历史信号表_28.csv；同日行业最后修订，初建非严格时点版本'
        years={day[:4] for day in dates}
        # 未来无关年片只进入运行清单，不改变旧窗口的来源文字或制造案例修订。
        shards = [str(p.relative_to(self.path.parent.parent.parent)) for p in self.paths if p != self.path and p.stem in years]
        if shards:
            source += '；基表后依次叠加年度分片：' + '；'.join(shards)
        return {'窗口': 'T-20..T-1，严格排除开始日；各观测日23:59:59收尾',
                '开始': dates[0] if dates else None, '结束': dates[-1] if dates else None,
                '应有交易日': 20, '已覆盖交易日': len(dates),
                '有T13行交易日': sum(r['原值'] is not None for r in rows), '各字段有效日数': counts,
                '来源': source,
                '读数': rows}


def latest_records(path):
    if not Path(path).exists():
        return {}
    return {r['事件id']: r for r in read_csv(path)}


def append_records(path, records, now):
    """保留原始字节与每列最近非空人工判读；失效案例也只追加状态修订。"""
    with write_lock(path):
        return _append_records_locked(path, records, now)


def _append_records_locked(path, records, now):
    path = Path(path)
    old = path.read_bytes() if path.exists() else b''
    existing = list(csv.DictReader(io.StringIO(old.decode('utf-8-sig')))) if old else []
    if old and next(csv.reader(io.StringIO(old.decode('utf-8-sig')))) != FIELDS:
        raise ValueError('主线案例表头不符，拒绝覆盖历史')
    latest = {r['事件id']: r for r in existing}
    human = {}
    for row in existing:
        values = human.setdefault(row['事件id'], {})
        for key in HUMAN_FIELDS:
            if row.get(key):
                values[key] = row[key]
    incoming = {r['事件id']: r for r in records}
    for event_id, previous in latest.items():
        if event_id not in incoming:
            invalid = dict(previous)
            invalid['状态'] = '输入修订后不再满足'
            invalid['程序说明'] = '最新输入重算不再产生该事件；旧版本与人工判读保留，现行总表不计入'
            incoming[event_id] = invalid
    additions = []
    # 若最后一版人工列空而更早版非空，需要追加恢复列，即使机器指标不变。
    ignored = ['追加时间', '更新原因']
    for event_id, raw in incoming.items():
        item = {k: cell(raw.get(k)) for k in FIELDS}
        item.update(human.get(event_id, {}))
        previous = latest.get(event_id)
        if previous and all(item[k] == previous[k] for k in FIELDS if k not in ignored):
            continue
        item['追加时间'] = now.isoformat()
        item['更新原因'] = '输入或状态变化，保留旧行与人工非空判读' if previous else '首次记录'
        additions.append(item)
    if not additions and old:
        return 0
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, FIELDS, lineterminator='\n')
    if not old:
        writer.writeheader()
    writer.writerows(additions)
    # 不经通用换行解码，确保原有CRLF、BOM等字节作为前缀原样保存。
    atomic_text(path, old.decode('utf-8') + ('' if not old or old.endswith(b'\n') else '\n') + buffer.getvalue())
    return len(additions)


def dump(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
