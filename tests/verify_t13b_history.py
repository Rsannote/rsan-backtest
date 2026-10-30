"""T13b离线验收：原始库完整性、基表保护、逐年覆盖和3行业×3日期独立手算。"""
import argparse
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from earnings_history import SOURCE_VERSION,load_v3,members,read_status
from mainline_signal_store import read_signal_table
from mainline_sources import Sources,csv_rows,number,registry
from verify_t13_signals import independent_quarter


def run(root,require_complete=False):
    root=Path(root);names=registry(root);snapshot,codes,digest=members(root)
    state=read_status(root/'data/财报/全市场单季/采集状态.jsonl')
    complete=[c for c in codes if state.get(c,{}).get('状态') in ('成功','无2013起历史')
              and state[c].get('来源版本')==SOURCE_VERSION and state[c].get('成分SHA256')==digest]
    missing=[{'代码':c,'状态':state.get(c,{}).get('状态','尚未采集'),'错误':state.get(c,{}).get('错误','')}
             for c in codes if c not in set(complete)]
    if require_complete:assert not missing,missing
    stock_years=collections.defaultdict(set);raw_rows=0;fallback=0;false_cells=0
    for code in codes:
        latest={r['报告期']:r for r in csv_rows(root/'data/财报/全市场单季'/f'{code}.csv') or []}
        raw_rows+=len(latest)
        for period,row in latest.items():
            stock_years[period[:4]].add(code)
            fallback+=row['利润口径']=='净利润回退'
            false_cells+=sum(row[k]=='False' for k in ('扣非净利润','净利润','营业总收入'))
    protection={}
    baseline=json.loads((root/'logs/test/T13b/分片隔离证明.json').read_text())
    for name in ('历史信号表_28.csv','信号表.csv'):
        raw=(root/'data/主线'/name).read_bytes();old=baseline[name]
        assert len(raw)>=old['字节'] and hashlib.sha256(raw[:old['字节']]).hexdigest()==old['原SHA256'],name
        protection[name]={'原始字节':old['字节'],'当前字节':len(raw),'原始全部字节保留':True}
    before=pd.read_csv(root/'data/主线/历史信号表_28.csv',dtype={'date':str,'code':str},low_memory=False).drop_duplicates(['date','code'],keep='last')
    after=read_signal_table(root,28);assert len(after)==len(after.drop_duplicates(['date','code']))
    coverage=[]
    for year in sorted(after.date.str[:4].unique()):
        for label,selected in [('全部候选',set(after.loc[after['主线判定']=='参与','code'])),('有成分快照的候选',set(snapshot)&set(after.loc[after['主线判定']=='参与','code']))]:
            item={'年':year,'集合':label,'行业数':len(selected)}
            for prefix,frame in [('回补前',before),('回补后',after)]:
                values=frame[frame.date.str.startswith(year)&frame.code.isin(selected)]['业绩_利润加速占比']
                values=pd.to_numeric(values,errors='coerce');n=int(values.map(lambda x:pd.notna(x) and math.isfinite(x)).sum())
                item[prefix+'有值行']=n;item[prefix+'候选行']=len(values)
                item[prefix+'有值比例']=n/len(values) if len(values) else None
            coverage.append(item)
    _,_,cfg=load_v3(root);manual=[];lookup=after.set_index(['date','code'])
    fields={'成分':'业绩_成分家数','可评估':'业绩_可评估家数','加速':'业绩_加速家数','样本比例':'业绩_样本加速占比','全行业比例':'业绩_利润加速占比'}
    for name in ('公用事业','电子','银行'):
        code=next(c for c,r in names.items() if r['行业']==name)
        for day in ('2024-09-02','2025-09-01','2026-09-01'):
            hand=independent_quarter(root,code,day,cfg);assert hand is not None
            row=lookup.loc[(day,code)]
            for key,column in fields.items():
                expected=hand[key] if key!='加速' or hand['可评估'] else None
                actual=number(row[column])
                assert (expected is None and actual is None) or (expected is not None and actual is not None and math.isclose(expected,actual,rel_tol=0,abs_tol=1e-10)),(name,day,key,expected,actual)
            manual.append({'日期':day,'行业':name,'代码':code,'手算':hand,'程序行业比例':number(row['业绩_利润加速占比']),'覆盖率':hand['可评估']/hand['成分']})
    proof={'来源运行':os.environ.get('GITHUB_RUN_ID'),'成分家数':len(codes),'成分行业数':len(snapshot),'完整采集家数':len(complete),'尚未完整':missing,
           '有效最新季度记录':raw_rows,'净利润回退记录':fallback,'False缺值单元格':false_cells,'逐年有财报股票数':{y:len(v) for y,v in sorted(stock_years.items())},
           '原CSV保护':protection,'逐年信号覆盖':coverage,'三行业三日期独立手算':manual,
           '边界':'当前成分快照回溯、法定截止规则可见、非公告原版/PIT；公司有源记录不等于2013起全部季度齐全。缺历史或同比基期不足不补0。'}
    out=root/'logs/test/T13b';out.mkdir(parents=True,exist_ok=True)
    (out/'最终历史核验.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    lines=['# T13b历史回补核验','',f'完整采集 {len(complete)}/{len(codes)} 家；{len(snapshot)}个当前行业；最新季度键{raw_rows}条；净利回退{fallback}条；False缺值{false_cells}格。','',proof['边界'],'',
           '|年|集合|行业数|前有值/候选行|后有值/候选行|前比例|后比例|','|---|---|---:|---:|---:|---:|---:|']
    for r in coverage:
        lines.append('|'+ '|'.join(str(r[k]) for k in ('年','集合','行业数'))+f"|{r['回补前有值行']}/{r['回补前候选行']}|{r['回补后有值行']}/{r['回补后候选行']}|{r['回补前有值比例']}|{r['回补后有值比例']}|")
    lines+=['','|日期|行业|成分|手算有效|手算加速|手算行业比例|程序行业比例|覆盖率|','|---|---|---:|---:|---:|---:|---:|---:|']
    for r in manual:
        h=r['手算'];lines.append('|'+ '|'.join(str(v) if v is not None else '缺' for v in (r['日期'],r['行业'],h['成分'],h['可评估'],h['加速'],h['全行业比例'],r['程序行业比例'],r['覆盖率']))+'|')
    lines+=['','逐股原值、独立同比和门槛判定见同名JSON；行业比例=加速/可评估，须至少10家且覆盖60%。解释【待判读】。','']
    (out/'最终历史核验.md').write_text('\n'.join(lines))
    print(json.dumps({'完整采集':len(complete),'成分':len(codes),'最新季度记录':raw_rows,'独立手算':len(manual),'基表原字节保留':True},ensure_ascii=False))
    return proof


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--require-complete',action='store_true');a=p.parse_args();run(a.root,a.require_complete)
