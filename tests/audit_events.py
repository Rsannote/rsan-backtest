"""离线验收：独立算距离、隔离复跑、保护输入；不联网。"""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import build_events as events


def main():
    report=json.loads((ROOT/'logs/事件库_最新.json').read_text())
    inputs=report['输入SHA256']
    for name,digest in inputs.items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    paths=['data/事件库/'+n for n in ('大调整.csv','大反弹.csv','大调整_10.csv')]
    original={p:(ROOT/p).read_bytes() for p in paths}
    counts={}
    for path in paths:
        with (ROOT/path).open(newline='',encoding='utf-8') as stream:
            rows=list(csv.DictReader(stream))
        assert list(rows[0])[:len(events.PUBLIC)]==events.PUBLIC
        counts[path]=len({row['事件id'] for row in rows})
    # 用隔离目录复跑，不改仓库CSV，也不让临时生成时间混入验收产物。
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for name in list(inputs)+paths+['system/事件库格式.md']:
            dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,dest)
        # 已存人工原文可以非空；只检查程序本次生成的候选记录留空。
        make_record=events.record
        def checked_record(*args,**kwargs):
            row=make_record(*args,**kwargs)
            assert all(not row[f] for f in events.HUMAN),'程序生成了人工判读'
            return row
        with patch.object(events,'record',side_effect=checked_record):
            for _ in range(2):
                result=events.run(root,events.dt.datetime.fromisoformat(report['generated_at']))
                assert all(n==0 for n in result['inserted'].values()),result['inserted']
                for name,raw in original.items():assert (root/name).read_bytes()==raw,name
    # 不调用distances：直接按JSON中历史原值和总体标准差重算六个平方差。
    current=report['当前特征'];sigma=report['标准化']['总体标准差']
    verification=[]
    for row in report['主表排名'][:5]:
        terms={f:((current[f]-float(row['历史特征'][f]))/sigma[f])**2 if sigma[f] else 0 for f in events.FEATURES}
        distance=math.sqrt(sum(terms.values()))
        assert math.isclose(distance,row['距离'],rel_tol=0,abs_tol=1e-12)
        verification.append({'事件id':row['事件id'],'六维平方贡献':terms,'平方和':sum(terms.values()),'独立开方':distance,'程序距离':row['距离'],'绝对差':abs(distance-row['距离'])})
    run_id=os.environ.get('GITHUB_RUN_ID')
    proof={'源Actions':f'https://github.com/Rsannote/rsan-backtest/actions/runs/{run_id}' if run_id else None,
           '数据基准':report['data_date'],'输入SHA256逐项一致':True,'事件数':counts,
           '程序生成候选人工六列全空':True,'既有人工原文允许非空且逐字节保留':True,'隔离同输入复跑次数':2,'CSV原字节不变':True,
           '六维距离独立核对':verification}
    out=ROOT/'logs/test/T9';out.mkdir(parents=True,exist_ok=True)
    (out/'离线核对.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    lines=['# T9 离线核对','',f"数据基准：{report['data_date']}；六项输入SHA256逐项一致。",'',
           '公共字段顺序与程序新生成候选的六个人工列留空已检查；既有人工原文允许非空。隔离目录连续复跑两次，三个CSV无新增且包含人工原文的旧字节一致。人工原文/CRLF保留另有单元测试。','',
           '|事件id|六维平方差之和|独立开方|程序距离|绝对差|','|---|---:|---:|---:|---:|']
    for row in verification:
        lines.append(f"|{row['事件id']}|{row['平方和']:.12g}|{row['独立开方']:.12g}|{row['程序距离']:.12g}|{row['绝对差']:.3g}|")
    lines+=['','计算方法：每维 `(当前值-事件启动值)²/历史总体方差`，六维求和后开方；历史标准差只由完整已闭合候选拟合。逐维数值见同名JSON。','']
    (out/'离线核对.md').write_text('\n'.join(lines));print(json.dumps(proof,ensure_ascii=False))


if __name__=='__main__':main()
