"""主线信号统一存储：原基表不变，年度修订只追加，读取同键最后版本。"""
import csv
import datetime as dt
import io
from pathlib import Path
import re

import pandas as pd

from flow_source import cell
from earnings_history_transaction import commit_files

LIMIT = 100 * 1024 * 1024


def paths(root,scope=28):
    if scope not in (28,31):raise ValueError('信号口径只能28或31')
    base=Path(root)/'data/主线'
    return (base/('历史信号表_28.csv' if scope==28 else '信号表.csv'),
            base/('历史信号修订' if scope==28 else '信号修订'))


def records(root,scope=28):
    base,directory=paths(root,scope);fields=None;latest={}
    files=([base] if base.exists() else [])+sorted(directory.glob('*.csv'))
    for path in files:
        if path!=base and not re.fullmatch(r'\d{4}\.csv',path.name):
            raise ValueError('年度修订文件名无效：'+str(path))
        with path.open(encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream)
            if not reader.fieldnames or not {'date','code'}.issubset(reader.fieldnames) or len(set(reader.fieldnames))!=len(reader.fieldnames):
                raise ValueError('主线信号表头无效：'+str(path))
            if fields is not None and fields!=reader.fieldnames:raise ValueError('主线基表/分片表头不一致')
            fields=reader.fieldnames
            for row in reader:
                if None in row or any(v is None for v in row.values()):raise ValueError('主线信号行字段数不符')
                dt.date.fromisoformat(row['date'])
                if path!=base and row['date'][:4]!=path.stem:raise ValueError('修订行与年度文件不符')
                latest[(row['date'],row['code'])]=row
    return fields,latest


def read_signal_table(root,scope=28,asof=None):
    """先基表后年度分片取last；最后筛asof，缺值修订不会回退旧值。"""
    fields,latest=records(root,scope)
    if fields is None:return pd.DataFrame()
    values=[row for key,row in sorted(latest.items()) if asof is None or key[0]<=asof]
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fields);writer.writeheader();writer.writerows(values)
    return pd.read_csv(io.StringIO(output.getvalue()),dtype={'date':str,'code':str},low_memory=False)


def necessary_time(note):
    return tuple(re.findall(r'(?:^|;)([DVMK]=[^;]+)',str(note or '')))


def plan_revisions(root,scope,table,stamp,reason='T13b-v3'):
    """仅数值、状态、来源或必要时点真正变化的键追加，不因版本注记追加。"""
    base,directory=paths(root,scope);old_fields,latest=records(root,scope)
    fields=list(table.columns)+['生成时间','修订原因']
    if old_fields is not None and old_fields!=fields:raise ValueError('主线信号表头不符，不改原表')
    compare=[f for f in table.columns if f!='使用时点说明']
    pending={};result=table.copy()
    seen=set()
    for index,raw in table.iterrows():
        row={f:cell(raw.get(f)) for f in fields};key=(row['date'],row['code'])
        if key in seen:raise ValueError('新信号包含重复日期代码')
        seen.add(key);dt.date.fromisoformat(row['date'])
        before=latest.get(key)
        unchanged=before and all(row[f]==before[f] for f in compare) and necessary_time(row.get('使用时点说明'))==necessary_time(before.get('使用时点说明'))
        if unchanged:
            result.at[index,'使用时点说明']=before.get('使用时点说明','')
            continue
        row['生成时间']=stamp;row['修订原因']=reason
        pending.setdefault(row['date'][:4],[]).append(row)
    changes={};files={}
    for year,rows in sorted(pending.items()):
        target=directory/(year+'.csv');old=target.read_bytes() if target.exists() else b''
        output=io.StringIO(newline='');writer=csv.DictWriter(output,fields,lineterminator='\n')
        if not old:writer.writeheader()
        writer.writerows(rows)
        payload=(b'\n' if old and not old.endswith(b'\n') else b'')+output.getvalue().encode()
        changes[target]=old+payload
        files[str(target.relative_to(root))]={'原字节':len(old),'追加字节':len(payload),'预计总字节':len(old)+len(payload),'追加行':len(rows),'上限字节':LIMIT,'允许追加':len(old)+len(payload)<LIMIT}
    report={'原基表':str(base.relative_to(root)),'原基表字节':base.stat().st_size if base.exists() else 0,
            '原基表保持不变':True,'年度分片':files,'追加行':sum(len(r) for r in pending.values()),
            '追加字节':sum(f['追加字节'] for f in files.values()),'允许追加':all(f['允许追加'] for f in files.values())}
    return result,changes,report


def write_revisions(root,changes,plans):
    if any(not plan['允许追加'] for plan in plans.values()):
        raise ValueError('年度主线修订CSV预计达到100MiB，正式信号未写')
    commit_files(root,changes)
