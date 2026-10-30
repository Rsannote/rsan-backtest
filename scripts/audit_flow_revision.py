"""PR21复审：同一历史样本逐年覆盖率、关键列健康前后对照、旧CSV保护。"""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import pandas as pd
import numpy as np

from auto_judgment_common import atomic_text
from flow_metrics import SW_COLUMNS
from flow_source import NEW,START31
from health_check import key_value_coverage,load_actual
ROOT=Path(__file__).resolve().parents[1]
BASE='1aa06e3aa0411f993461344b6b08be473d2b238a'


def git_bytes(path):return subprocess.check_output(['git','show',BASE+':'+path],cwd=ROOT)


def coverage_rows(before,after):
    columns=[c for c in SW_COLUMNS if c not in ('date','code','name','来源','数据说明')];result=[]
    for col in columns:
        for year in sorted(before.date.str[:4].unique()):
            old=before[before.date.str.startswith(year)];new=after[after.date.str.startswith(year)]
            if col.endswith('_28'):old=old[~old.code.isin(NEW)];new=new[~new.code.isin(NEW)]
            if col.endswith('_31'):old=old[old.date>=START31];new=new[new.date>=START31]
            if len(old)!=len(new) or set(zip(old.date,old.code))!=set(zip(new.date,new.code)):raise ValueError('前后覆盖率样本不一致')
            n=len(old);a=int(np.isfinite(pd.to_numeric(old[col],errors='coerce')).sum());b=int(np.isfinite(pd.to_numeric(new[col],errors='coerce')).sum())
            result.append({'年份':year,'列':col,'应有':n,'修复前有值':a,'修复后有值':b,'修复前比例':a/n if n else None,'修复后比例':b/n if n else None})
    return result


def main():
    out=ROOT/'logs/test/T8b/复审';out.mkdir(parents=True,exist_ok=True)
    raw=git_bytes('data/资金轮动/日表.csv');before=pd.read_csv(io.BytesIO(raw),dtype={'date':str,'code':str},low_memory=False).drop_duplicates(['date','code'],keep='last')
    after=pd.read_csv(ROOT/'data/资金轮动/日表.csv',dtype={'date':str,'code':str},low_memory=False).drop_duplicates(['date','code'],keep='last')
    after=after[after.date<=before.date.max()];rows=coverage_rows(before,after)
    detailed=['# 各数值列逐年覆盖率（PR21复审）','','每个行业×交易日一票，按日期/代码最后修订；28排除新三行业，31起点前不适用。所有数字与同次覆盖率CSV由相同rows输出。','']
    for column in dict.fromkeys(r['列'] for r in rows):
        detailed+=['## '+column,'','|年份|应有|修复前有值|修复后有值|修复前比例|修复后比例|','|---|---:|---:|---:|---:|---:|']
        for row in [r for r in rows if r['列']==column]:
            before_ratio='—' if row['修复前比例'] is None else f"{row['修复前比例']:.2%}"
            after_ratio='—' if row['修复后比例'] is None else f"{row['修复后比例']:.2%}"
            detailed.append('|'+ '|'.join(str(v) for v in [row['年份'],row['应有'],row['修复前有值'],row['修复后有值'],before_ratio,after_ratio])+'|')
        detailed+=['']
    atomic_text(out/'覆盖率_逐列完整表.md','\n'.join(detailed)+'\n')
    buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=list(rows[0]),lineterminator=chr(10));w.writeheader();w.writerows(rows);atomic_text(out/'覆盖率_逐年各列.csv',buf.getvalue())
    keys=['占比60日分位_28','占比一年分位_28','占比60日分位_31','占比一年分位_31','偏离60日线']
    lines=['# PR21滚动缺口修复覆盖率','',f'比较基线{BASE}与当前，日期固定截至{before.date.max()}；每个行业×交易日一票，按日/代码取最后修订。28排除新三行业，31仅2021-12-13起；不适用用—，不算缺失。所有数值列的逐年分子/分母见覆盖率_逐年各列.csv。','', '|年份|'+ '|'.join(keys)+'|','|---|'+ '|'.join(['---:']*len(keys))+'|']
    for year in sorted(before.date.str[:4].unique()):
        cells=[]
        for key in keys:
            r=next(r for r in rows if r['年份']==year and r['列']==key)
            cells.append('—' if not r['应有'] else f"{r['修复前比例']:.2%} → {r['修复后比例']:.2%}")
        lines.append('|'+year+'|'+ '|'.join(cells)+'|')
    lines+=['','固定60/250个交易日窗口，最少有效数54/225；起步期也须满足该门槛，不提前凑值；当日原始缺失不填，故覆盖率不冒充100%。缺口恢复详情见缺口重抓/结果.json。','']
    atomic_text(out/'覆盖率_修复前后.md','\n'.join(lines))
    # 真实旧行前缀检查，派生CSV允许按修订追加，禁止删改。
    paths=subprocess.check_output(['git','-c','core.quotepath=false','ls-tree','-r','--name-only',BASE,'data/资金轮动'],cwd=ROOT).decode().splitlines()
    protected=[]
    for path in paths:
        if not path.endswith('.csv'):continue
        old=git_bytes(path);current=(ROOT/path).read_bytes();assert current.startswith(old),path
        protected.append({'文件':path,'原字节':len(old),'新字节':len(current),'原SHA256':hashlib.sha256(old).hexdigest(),'新SHA256':hashlib.sha256(current).hexdigest(),'旧字节保留':True})
    assert len(protected)==35,len(protected)
    atomic_text(out/'原字节保护.json',json.dumps(protected,ensure_ascii=False,indent=2)+'\n')
    policy=json.loads((ROOT/'system/运行清单.d/资金轮动.json').read_text())['items'][0]['key_value_coverage'];day=before.date.max();comparison={}
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);target=root/policy['path'];target.parent.mkdir(parents=True);target.write_bytes(raw)
        old,issues=key_value_coverage(root,policy,day);comparison['修复前']={'关键列有值比例':old,'按新规则': '部分失败' if issues else '正常','原因':issues}
    new,issues=key_value_coverage(ROOT,policy,day);comparison['修复后']={'关键列有值比例':new,'按新规则':'部分失败' if issues else '正常','原因':issues}
    atomic_text(out/'关键列健康对照.json',json.dumps(comparison,ensure_ascii=False,indent=2)+'\n')
    assert not issues,comparison
    print('逐年全数值列覆盖率、关键列健康及35份CSV旧字节保护通过')


if __name__=='__main__':main()
