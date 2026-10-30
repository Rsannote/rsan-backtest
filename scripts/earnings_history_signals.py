"""T13b 正式CSV追加前精确预算；沿用原字段、精度和修订语义。"""
import csv
import io
from pathlib import Path

from flow_source import cell

LIMIT = 100 * 1024 * 1024


def append_plan(path, fields, rows, stamp, reason):
    path = Path(path)
    previous = {}
    if path.exists() and path.stat().st_size:
        with path.open(encoding='utf-8', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != fields:
                raise ValueError(str(path) + '表头不符，不覆盖')
            for row in reader:
                previous[(row['date'], row['code'])] = row
    compare = [f for f in fields if f not in ('采集时间', '生成时间', '修订原因')]
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fields, lineterminator='\n')
    if not path.exists() or not path.stat().st_size:
        writer.writeheader()
    count = 0
    for raw in rows:
        row = {f: cell(raw.get(f)) for f in fields}
        key = (row['date'], row['code'])
        before = previous.get(key)
        if before and all(row[f] == before[f] for f in compare):
            continue
        row['生成时间'] = stamp
        row['修订原因'] = reason
        writer.writerow(row)
        previous[key] = row
        count += 1
    prefix = b''
    old_size = path.stat().st_size if path.exists() else 0
    if old_size and count:
        with path.open('rb') as stream:
            stream.seek(-1, 2)
            if stream.read(1) != b'\n':
                prefix = b'\n'
    payload = prefix + output.getvalue().encode('utf-8')
    return {'原字节': old_size, '追加字节': len(payload), '预计总字节': old_size + len(payload),
            '追加行': count, '上限字节': LIMIT, '允许追加': old_size + len(payload) < LIMIT}


def preserve_unchanged_notes(path, table):
    """经济列/状态/来源均未变时保留原注记，避免纯版本说明制造整表修订。"""
    path=Path(path)
    if not path.exists():return table
    with path.open(encoding='utf-8',newline='') as stream:
        previous={(r['date'],r['code']):r for r in csv.DictReader(stream)}
    compare=[c for c in table.columns if c!='使用时点说明']
    result=table.copy()
    for index,raw in table.iterrows():
        before=previous.get((str(raw['date']),str(raw['code'])))
        if before and all(cell(raw.get(f))==before.get(f,'') for f in compare):
            result.at[index,'使用时点说明']=before.get('使用时点说明','')
    return result
