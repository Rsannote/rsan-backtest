#!/usr/bin/env python3
"""T13五维数值信号；当前申万31、历史固定28，缺项显式留空，无阶段判定。"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from auto_judgment_common import atomic_text
from flow_source import append_rows
from mainline_sources import BJ,NEW,Sources,registry,number
from flow_source import START,START31
from mainline_signal_store import plan_revisions,write_revisions

ROOT=Path(__file__).resolve().parents[1]
FLOW={'成交额原值':'资金_成交额亿','占比':'资金_成交占比','总额':'资金_行业合计亿','占比5日变化':'资金_占比5日变化','占比20日变化':'资金_占比20日变化',
      '占比5日均':'资金_占比5日均','占比20日均':'资金_占比20日均','占比5日均较20日均':'资金_占比5均较20均','占比60日分位':'资金_占比60日分位',
      '相对沪深300_20':'资金_相对沪深300_20','相对沪深300_60':'资金_相对沪深300_60','占比一年分位':'拥挤_占比一年分位','偏离60日线':'拥挤_偏离60日线'}
RANK=['环境_中国久期适配bp','环境_美国久期适配bp','业绩_EPS本年上修占比','业绩_EPS次年上修占比','业绩_利润加速占比','业绩_样本加速占比',
      '热度_新闻4周差量','热度_新闻4周变化率','热度_概念出现次数','资金_占比5日变化','资金_相对沪深300_20','资金_相对沪深300_60','拥挤_占比一年分位','拥挤_偏离60日线']


def flow_latest(root,asof=None):
    path=Path(root)/'data/资金轮动/日表.csv'
    x=pd.read_csv(path,dtype={'date':str,'code':str},low_memory=False)
    # T8按同键追加修订，任何聚合、排名和日期筛选之前先取文件最后一条。
    x=x.drop_duplicates(['date','code'],keep='last').sort_values(['date','code'])
    if asof:x=x[x.date<=asof]
    if x.empty:raise ValueError('T8日表无可用日期')
    return x


def add_ranks(frame,expected):
    active=frame['主线判定'].eq('参与')
    for col in RANK:
        values=pd.to_numeric(frame[col],errors='coerce').where(active)
        values=values.where(np.isfinite(values))
        ranks=values.groupby(frame.date).rank(method='first',ascending=False)
        count=values.notna().groupby(frame.date).transform('sum')
        frame[col+'_有效排名']=ranks
        frame[col+'_全排名']=ranks.where(count.eq(expected))
        frame[col+'_有效行业数']=count
    return frame


def build(flow,names,sources,scope):
    codes=sorted(set(names)-(NEW if scope==28 else set()));start=START if scope==28 else START31
    selected=flow[flow.code.isin(codes)&(flow.date>=start)].copy()
    # 保留每个交易日的完整观察集合，整行业缺行也显示为缺而不是消失。
    days=sorted(selected.date.unique());lookup=selected.set_index(['date','code']).to_dict('index');rows=[]
    for day in days:
        for code in codes:
            item=names[code];raw=lookup.get((day,code),{})
            out={'date':day,'code':code,'行业':item['行业'],'口径':str(scope),'风格':item['风格'],'久期':item['久期'],'主线判定':item['主线判定']}
            out.update(sources.for_day(day,code))
            for key,label in FLOW.items():
                source=key+'_'+str(scope) if key not in ('成交额原值','相对沪深300_20','相对沪深300_60','偏离60日线') else key
                out[label]=number(raw.get(source))
            # 综合只保留占比合计核对项，不进入候选维度/排名。
            if item['主线判定']!='参与':
                for key in list(out):
                    if key.startswith(('环境_','业绩_','热度_','拥挤_')) or (key.startswith('资金_') and key not in ('资金_成交占比','资金_行业合计亿','资金_成交额亿')):out[key]=None
            missing=[]
            for label,cols in [('环境适配',['环境_中国10年20日变化bp','环境_美国10年20日变化bp','环境_大盘攻守']),('业绩预期',['业绩_EPS本年上修占比','业绩_EPS次年上修占比','业绩_利润加速占比','业绩_样本加速占比']),('热度催化',['热度_新闻4周变化率','热度_概念出现次数']),('资金',['资金_成交占比','资金_相对沪深300_20','资金_相对沪深300_60']),('拥挤度',['拥挤_占比一年分位','拥挤_偏离60日线'])]:
                absent=sum(out.get(c) is None or out.get(c)=='' for c in cols)
                out[label+'_状态']='排除' if item['主线判定']!='参与' else ('缺' if absent==len(cols) else '部分缺' if absent else '有值')
                if absent and item['主线判定']=='参与':missing.append(label+('缺' if absent==len(cols) else '部分缺'))
            out['缺失说明']='；'.join(missing)
            out['T8数据说明']=str(raw.get('数据说明','')) if pd.notna(raw.get('数据说明')) else ''
            out['使用时点说明']='收盘后指标；市场/社区实际可见时刻另列；初次历史建库非逐日版本'
            quarter_note=sources.quarter_notes.get((day,code),'')
            if quarter_note or item['久期']=='中':
                out['使用时点说明']='T13b-v2'+quarter_note+(';中性定义，非缺失' if item['久期']=='中' else '')
            rows.append(out)
    result=pd.DataFrame(rows)
    if result.empty:return result
    return add_ranks(result,sum(names[c]['主线判定']=='参与' for c in codes)).sort_values(['date','code'])


def json_safe(value):
    if isinstance(value,dict):return {str(k):json_safe(v) for k,v in value.items()}
    if isinstance(value,list):return [json_safe(v) for v in value]
    if isinstance(value,(float,np.floating)):return float(value) if np.isfinite(value) else None
    if isinstance(value,np.integer):return int(value)
    return value


def dump(path,value):atomic_text(path,json.dumps(json_safe(value),ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def run(root=ROOT,asof=None,now=None,estimate_only=False):
    root=Path(root).resolve();now=(now or dt.datetime.now(BJ)).astimezone(BJ);stamp=now.isoformat();names=registry(root)
    inputs=[root/'system/行业清单.csv',root/'system/催化日历.csv'];before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    flow=flow_latest(root,asof);last=flow.date.max()
    if not set(flow.code).issubset(names):raise ValueError('T8输入含Claude申万清单以外代码，不以ETF替代申万')
    if last>now.date().isoformat():raise ValueError('T8输入数据日期在当前时间之后')
    sources=Sources(root,names);tables={scope:build(flow,names,sources,scope) for scope in (31,28)}
    base=root/'data/主线';added={};plans={};changes={}
    for scope,table in tables.items():
        if table.empty:continue
        table,updates,plan=plan_revisions(root,scope,table,stamp)
        tables[scope]=table;changes.update(updates);plans[str(scope)]=plan;added[str(scope)]=plan['追加行']
    dump(root/'logs/test/T13b/信号追加预算.json',{'生成时间':stamp,'逐口径':plans,'说明':'原CSV字节保持不变；只追加真正变化行到年度修订分片；逐分片精确预检100MiB'})
    if estimate_only:return {'追加预算':plans,'errors':[]}
    write_revisions(root,changes,plans)
    current=tables[31][tables[31].date==last].to_dict('records') if not tables[31].empty else []
    errors=[];warnings=[]
    if last>=START31 and len(current)!=31:errors.append('最新31行业信号行数不是31，见T8日期范围')
    if current and not any(number(r.get('资金_成交占比')) is not None for r in current if r['主线判定']=='参与'):
        errors.append('最新所有候选的申万资金占比均缺，不能仅凭文件有行报正常')
    if not (root/'data/一致预期/行业上修.csv').exists():warnings.append('T12b尚未提供主分支一致预期上修表，相应维度缺失，不读未合并分支')
    warnings+=['当前成分快照回溯有幸存者偏差，非历史成分/PIT财报库；利润加速占比仅在可评估>=10家且>=60%成分时=加速/可评估，否则缺','新闻历史不能回补，历史潜伏的先行维度只能用业绩；中久期适配0为中性定义，非缺失，原中美利率缺值仍保留','人工行业映射为当前分类回溯；T8按要求读取最后修订，历史不是严格逐日版本']
    for path,digest in before.items():
        if hashlib.sha256((root/path).read_bytes()).hexdigest()!=digest:raise AssertionError('Claude表被改动：'+path)
    counts={d:{s:sum(r[d+'_状态']==s for r in current) for s in ('有值','部分缺','缺','排除')} for d in ('环境适配','业绩预期','热度催化','资金','拥挤度')}
    report={'title':'主线信号表','generated_at':stamp,'data_date':last,'status':'失败' if errors else '正常','errors':errors,'warnings':warnings,'观察对象':'申万一级31行业；综合排除候选，只参与合计','历史回放口径':'固定28行业，排除2021新增三行业；独立历史信号表_28.csv，不与31列拼接','维度覆盖':counts,'新增行':added,'追加预算':plans,'当前行':current,'维护表SHA256':before,'输入T8_SHA256':hashlib.sha256((root/'data/资金轮动/日表.csv').read_bytes()).hexdigest(),'催化日历':sources.catalysts,'解释':'【待判读】；不判阶段、不设主线参数、不生成判断'}
    dump(base/'最新.json',report);dump(root/'logs/主线信号_最新.json',report)
    lines=['# 主线信号表','',f'数据日期：{last}；生成：{stamp}（北京时间）。当前31，历史回放固定28分文件。','', '|行业|代码|主线判定|环境适配|业绩预期|热度催化|资金|拥挤度|占比|20日相对|一年分位|','|---|---|---|---|---|---|---|---|---:|---:|---:|']
    def show(v):return '缺' if v is None or (isinstance(v,float) and not np.isfinite(v)) or v=='' else str(round(v,8)) if isinstance(v,float) else str(v)
    for r in current:lines.append('|'+ '|'.join(show(r.get(k)) for k in ('行业','code','主线判定','环境适配_状态','业绩预期_状态','热度催化_状态','资金_状态','拥挤度_状态','资金_成交占比','资金_相对沪深300_20','拥挤_占比一年分位'))+'|')
    lines+=['','## 催化日历（Claude维护，仅具体日期可参与将来的事件判定）','','|日期|事件|相关行业|确定性|具体日期|','|---|---|---|---|---|']
    for r in sources.catalysts:lines.append('|'+ '|'.join(str(r.get(k,'')) for k in ('日期','事件','相关行业','确定性','具体日期'))+'|')
    lines+=['','解释【待判读】。历史指标按数据可用程度留空；排名同时显示有效样本数，只有全部候选有值才给全行业排名。','',*['- '+x for x in warnings],'']
    atomic_text(base/'最新.md','\n'.join(lines));atomic_text(root/'logs/主线信号_最新.md','\n'.join(lines))
    print(json.dumps({'数据日期':last,'31唯一行':len(tables[31]),'28唯一行':len(tables[28]),'新增':added,'维度覆盖':counts},ensure_ascii=False));return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--asof');p.add_argument('--estimate-only',action='store_true',help='只计算精确追加字节，不写正式信号/最新派生');p.add_argument('--backfill',action='store_true',help='一条命令全量回补两套独立口径，幂等仅追加修订');a=p.parse_args();r=run(a.root,a.asof,estimate_only=a.estimate_only);return 1 if r['errors'] else 0


if __name__=='__main__':raise SystemExit(main())
