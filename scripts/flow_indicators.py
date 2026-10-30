#!/usr/bin/env python3
"""T8b申万主源、ETF专列备用、28/31口径日表与描述统计。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from auto_judgment_common import BJ, TradingCalendar, atomic_text
from flow_source import START, START31, append_rows, fetch_all, ingest_capture
from flow_metrics import SW_COLUMNS, ETF_COLUMNS, compute_sw, compute_etf, read_inputs, unit_check
from flow_stats import describe, markdown

ROOT=Path(__file__).resolve().parents[1]


def clean(obj):
    if isinstance(obj,dict):return {str(k):clean(v) for k,v in obj.items()}
    if isinstance(obj,(list,tuple)):return [clean(v) for v in obj]
    if obj is None:return None
    if isinstance(obj,(float,np.floating)):return float(obj) if np.isfinite(obj) else None
    if isinstance(obj,np.integer):return int(obj)
    if isinstance(obj,np.bool_):return bool(obj)
    return obj


def dump(path,obj):atomic_text(path,json.dumps(clean(obj),ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def target_day(root,now):
    cal=TradingCalendar(root);last=cal.days[-1].isoformat();today=now.date()
    cutoff=today.isoformat() if cal.is_open(today) and now.time()>=dt.time(16) else cal.previous(today).isoformat()
    return min(last,cutoff)


def run(root=ROOT,offline=False,capture=None,now=None):
    root=Path(root).resolve();now=(now or dt.datetime.now(BJ)).astimezone(BJ);stamp=now.isoformat();target=target_day(root,now)
    work=root/'logs/test/T8b/执行'/os.environ.get('GITHUB_RUN_ID','离线复现');work.mkdir(parents=True,exist_ok=True);errors=[];warnings=[];source={};verified=False
    if capture:
        if not offline:raise ValueError('捕获输入只允许显式离线复现模式')
        ingest_capture(root,capture)
    if not offline:
        try:source=fetch_all(root,work,target);verified=True
        except Exception as exc:
            errors.append('申万本次抓取/证书失败：'+type(exc).__name__+': '+str(exc));warnings.append('资金维度切换ETF近似，申万列不接入ETF')
            dump(work/'失败.json',{'北京时间':stamp,'错误':errors[-1]})
    benchmark,names,C,A=read_inputs(root,target);daily,metrics=compute_sw(C,A,benchmark,names);etf=compute_etf(root,benchmark)
    current=daily[daily['date']==target].copy();etf_current=etf[etf['date']==target].copy()
    scope='31' if target>=START31 else '28';column='占比_'+scope;expected=31 if scope=='31' else 28
    complete=len(current)==expected and current[column].notna().sum()==expected
    use_primary=complete and (verified or offline)
    if not complete:errors.append(f'申万{target}的{scope}口径不完整，资金维度仅看ETF专列')
    if not use_primary and etf_current.empty:errors.append('ETF备用也无当日数据，不沿用旧值')
    base=root/'data/资金轮动';base.mkdir(parents=True,exist_ok=True)
    sw_added=append_rows(base/'日表.csv',SW_COLUMNS+['生成时间','修订原因'],daily.to_dict('records'),['date','code'],stamp)
    etf_added=append_rows(base/'ETF日表.csv',ETF_COLUMNS+['生成时间','修订原因'],etf.to_dict('records'),['date','code'],stamp)
    units=unit_check(root,A,target) if names else {'rows':[],'全部通过':False,'口径':'无申万数据不能核对单位'}
    if not units['全部通过']:warnings.append('最近10日单位量级核对未全部通过，原字段不擅自换算')
    stats,pairs=describe(C,benchmark,names,metrics)
    dump(base/'统计.json',stats);atomic_text(base/'统计.md',markdown(stats,pairs))
    atomic_text(base/'领先滞后.csv',pairs.to_csv(index=False,lineterminator='\n'));dump(base/'领先滞后.json',pairs.to_dict('records'))
    dump(root/'logs/test/T8b/成交额单位核对.json',units)
    lines=['# 31行业成交额单位核对','',units['口径'],'', '|日期|申万31合计原值|市场成交额亿|市场来源|相对偏差|±15%内|','|---|---:|---:|---|---:|---|']
    for r in units['rows']:lines.append(f"|{r['date']}|{r['申万31合计原值']:.8f}|{r['市场成交额亿']:.4f}|{r['市场来源']}|{r['相对偏差']:.6%}|{r['±15%内']}|")
    atomic_text(root/'logs/test/T8b/成交额单位核对.md','\n'.join(lines)+'\n')
    primary_rows=current.to_dict('records') if use_primary else []
    report={'title':'资金轮动','generated_at':stamp,'data_date':target,'status':'失败' if errors else '正常','errors':errors,'warnings':warnings,
            '资金来源':'申万官网' if use_primary else 'ETF 近似','申万口径':scope if use_primary else None,'本次在线TLS验证通过':verified,
            '执行模式':'离线复现，不冒称本次TLS验证' if offline else 'Actions在线抓取',
            '申万':primary_rows,'ETF备用':etf_current.to_dict('records'),'申万当日完整':complete,'申万当日有效行业数':int(current[column].notna().sum()) if column in current else 0,
            '单位核对通过':units['全部通过'],'新增日表行':sw_added,'新增ETF行':etf_added,'数据起点':START,
            '统计口径':'固定28行业，描述性条件统计；无信号测试、无交易回测','说明':'资金指标与拥挤度；解释【待判读】',
            '输入SHA256':{'沪深300':hashlib.sha256((root/'data/行情库/指数/sh000300.csv').read_bytes()).hexdigest(),'市场日指标':hashlib.sha256((root/'data/市场状态/日指标.csv').read_bytes()).hexdigest()},
            '采集证据':{'证书':source.get('TLS'),'行业数':source.get('完整行业数')}}
    if not offline:
        dump(base/'最新.json',report);dump(root/'logs/资金轮动_最新.json',report)
    else:dump(root/'logs/test/T8b/离线复现/最新.json',report)
    display=['# 资金轮动最新','',f'北京时间：{stamp}；数据日期：{target}；资金来源：{report["资金来源"]}。','',
             '两套申万列独立，ETF值只在备用表显示；缺失不补0。解释【待判读】。','', '|行业|代码|占比_28|占比_31|相对沪深300_20|占比一年分位_31|偏离60日线|','|---|---|---:|---:|---:|---:|---:|']
    def v(n):return '缺' if n is None or pd.isna(n) else str(round(float(n),8))
    for r in primary_rows:display.append('|'+ '|'.join([r['name'],r['code']]+[v(r[k]) for k in ('占比_28','占比_31','相对沪深300_20','占比一年分位_31','偏离60日线')])+'|')
    display+=['','## ETF备用（独立近似列）','', '|ETF|代码|ETF_占比|ETF_相对沪深300_20|ETF_偏离60日线|','|---|---|---:|---:|---:|']
    for r in etf_current.to_dict('records'):display.append('|'+ '|'.join([r['name'],r['code']]+[v(r[k]) for k in ('ETF_占比','ETF_相对沪深300_20','ETF_偏离60日线')])+'|')
    display+=['','错误：'+json.dumps(errors,ensure_ascii=False),'警告：'+json.dumps(warnings,ensure_ascii=False),'']
    if not offline:
        atomic_text(base/'最新.md','\n'.join(display));atomic_text(root/'logs/资金轮动_最新.md','\n'.join(display))
    else:atomic_text(root/'logs/test/T8b/离线复现/最新.md','\n'.join(display))
    alert={'checked_at':stamp,'items':[{'id':'flow','name':'资金轮动','status':report['status'],'expected':{'申万':'本次TLS通过且当日完整'},
                                     'actual':{'资金来源':report['资金来源'],'data_date':target},'reasons':errors or ['申万完整，本次运行正常']}]}
    if not offline:dump(root/'logs/资金轮动_告警.json',alert)
    print(json.dumps({'数据日期':target,'资金来源':report['资金来源'],'日表':len(daily),'ETF日表':len(etf),'errors':errors,'warnings':warnings},ensure_ascii=False),flush=True)
    return clean(report)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--offline',action='store_true');parser.add_argument('--capture',type=Path);args=parser.parse_args()
    if not args.offline and os.environ.get('GITHUB_ACTIONS')!='true':parser.error('联网采集仅允许Actions；本机可显式--offline重算')
    r=run(args.root,args.offline,args.capture)
    return 1 if r['资金来源']=='ETF 近似' and not r['ETF备用'] else 0


if __name__=='__main__':raise SystemExit(main())
