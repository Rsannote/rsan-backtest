"""T13真实输入核对：固定种子3行业×3日期、最新样本、双口径、只读及幂等。"""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import sys
import tempfile

import pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from mainline_sources import registry,NEW,csv_rows,split,number,Sources
from mainline_signals import run,dump,flow_latest,RANK
from auto_judgment_common import atomic_text


def latest(path):return pd.read_csv(path,dtype={'date':str,'code':str},low_memory=False).drop_duplicates(['date','code'],keep='last')

def same(actual,expected):
    assert (expected is None and pd.isna(actual)) or (expected is not None and math.isclose(float(actual),float(expected),abs_tol=1e-10,rel_tol=1e-9)),(actual,expected)


def independent_quarter(root,industry,day,cfg):
    m=json.loads((root/'data/v3/申万成分.json').read_text()).get(industry)
    if not m or m['date']>day:return None
    valid=passed=0;stocks=sorted({r['证券代码'] for r in m['rows']});details=[];asof=dt.date.fromisoformat(day)
    for code in stocks:
        rows=csv_rows(root/'data/财报/单季'/f'{code}.csv') or [];q={}
        for r in rows:
            d=dt.date.fromisoformat(r['报告期']);month,date={3:(4,30),6:(8,31),9:(10,31),12:(4,30)}[d.month];visible=dt.date(d.year+(d.month==12),month,date)
            if r.get('公告日'):visible=max(visible,dt.date.fromisoformat(r['公告日']))
            if visible>=asof:continue
            profit=number(r.get('扣非净利润'))
            if profit is None:profit=number(r.get('净利润'))
            q[d]={'profit':profit,'revenue':number(r.get('营业总收入'))}
        dates=sorted(q)
        if len(dates)<2:continue
        last,prev=dates[-1],dates[-2]
        # 抽查日2026-10-29最近已过截止期为2026-06-30，直接显示核对期。
        if last.isoformat()!='2026-06-30':continue
        def yoy(d,k):
            old=dt.date(d.year-1,d.month,d.day)
            if old not in q or q[old][k] is None or q[old][k]<=0 or q[d][k] is None:return None
            return q[d][k]/q[old][k]-1
        g,gprev,grev=yoy(last,'profit'),yoy(prev,'profit'),yoy(last,'revenue')
        if any(x is None for x in (g,gprev,grev)):continue
        ok=cfg['g_min']<g<=cfg['g_max'] and g>gprev and gprev>cfg['prev_min'] and grev>0
        valid+=1;passed+=ok
        details.append({'code':code,'报告期':last.isoformat(),'本期扣非或回退净利':q[last]['profit'],'去年同期':q[dt.date(last.year-1,last.month,last.day)]['profit'],'扣非同比':g,'上季同比':gprev,'营收同比':grev,'符合原v3加速条件':ok})
    return {'成分':len(stocks),'可评估':valid,'加速':passed,'样本比例':passed/valid if valid else None,'全行业比例':passed/len(stocks) if valid==len(stocks) and stocks else None,'逐只核对':details}


def main():
    names=registry(ROOT);flow=flow_latest(ROOT);history=latest(ROOT/'data/主线/历史信号表_28.csv');current=latest(ROOT/'data/主线/信号表.csv');report=json.loads((ROOT/'data/主线/最新.json').read_text());end=report['data_date']
    assert not history.code.isin(NEW).any();assert history.date.min()=='2014-02-21';assert current.date.min()=='2021-12-13'
    assert history.groupby('date').size().eq(28).all();assert current.groupby('date').size().eq(31).all()
    rng=random.Random(20261030);codes=rng.sample(sorted(c for c in names if c not in NEW and names[c]['主线判定']=='参与'),3);dates=rng.sample(sorted(history.date.unique()),3)
    checks=[];rates=pd.read_csv(ROOT/'data/宏观/派生.csv',dtype=str)
    for code in codes:
        for day in dates:
            source=flow[(flow.date==day)&~flow.code.isin(NEW)];amount=pd.to_numeric(source['成交额原值'],errors='coerce');den=float(amount.sum()) if len(source)==28 and amount.notna().all() else None
            original=source[source.code==code].iloc[0];numerator=number(original['成交额原值']);expected=numerator/den if numerator is not None and den else None
            row=history[(history.code==code)&(history.date==day)].iloc[0];same(row['资金_成交占比'],expected)
            candidates=rates[(rates.date==day)&(rates['指标']=='中国10年国债')&(rates['可见时刻']<=day+'T23:59:59+08:00')]
            delta=number(candidates.iloc[-1]['value']) if len(candidates) else None;same(row['环境_中国10年20日变化bp'],delta)
            direction={'长':-1,'短':1}.get(names[code]['久期']);fit=delta*direction if delta is not None and direction is not None else None;same(row['环境_中国久期适配bp'],fit)
            checks.append({'日期':day,'代码':code,'行业':names[code]['行业'],'口径':28,'分子亿元':numerator,'分母亿元':den,'手算占比':expected,'程序占比':number(row['资金_成交占比']),'中国20日bp':delta,'久期':names[code]['久期'],'手算适配bp':fit,'程序适配bp':number(row['环境_中国久期适配bp'])})
    sources=Sources(ROOT,names);supplement=[]
    for code in ['801160','801080','801780']:
        row=current[(current.date==end)&(current.code==code)].iloc[0];q=independent_quarter(ROOT,code,end,sources.qcfg)
        same(row['业绩_样本加速占比'],q['样本比例']);same(row['业绩_利润加速占比'],q['全行业比例']);same(row['业绩_可评估家数'],q['可评估']);same(row['业绩_加速家数'],q['加速'])
        articles={}
        p=ROOT/'data/新闻'/end[:7]/(end+'.jsonl')
        for line in p.read_text().splitlines():
            r=json.loads(line);articles[r['id']]=r
        tags=set(split(names[code]['新闻标签']));hit=[r['id'] for r in articles.values() if float(r['score'])>=2 and tags.intersection(r.get('tags',{}).get('行业',[]))]
        same(row['热度_新闻当日条数'],len(hit))
        concepts=csv_rows(ROOT/'data/社区情绪'/end/'同花顺热门概念.csv');words=split(names[code]['概念关键词']);matches=[r['name'] for r in concepts if any(k.casefold() in r['name'].casefold() for k in words)];same(row['热度_概念出现次数'],len(matches))
        supplementary={'日期':end,'行业':names[code]['行业'],'代码':code,'新闻标签':sorted(tags),'重要度2以上标签命中数':len(hit),'概念命中原名':matches,'概念出现次数':len(matches),'季度手算':q};supplement.append(supplementary)
        # 31占比独立用当前31分母，历史仍保留28。
        raw=flow[flow.date==end];den=raw['成交额原值'].sum(min_count=31);same(row['资金_成交占比'],raw[raw.code==code].iloc[0]['成交额原值']/den)
    # 原输入只读，隔离重跑不追加修订、不改已有字节。
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        for folder in ['scripts','system','data/主线','data/宏观','data/新闻','data/社区情绪','data/市场状态','data/财报','data/v3']:
            shutil.copytree(ROOT/folder,root/folder,ignore=shutil.ignore_patterns('__pycache__'))
        p=root/'data/资金轮动/日表.csv';p.parent.mkdir(parents=True);shutil.copyfile(ROOT/'data/资金轮动/日表.csv',p)
        if (ROOT/'data/一致预期').exists():shutil.copytree(ROOT/'data/一致预期',root/'data/一致预期')
        before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'data/主线').glob('*.csv')}
        again=run(root,now=dt.datetime.fromisoformat(report['generated_at'])+dt.timedelta(minutes=5));assert all(n==0 for n in again['新增行'].values())
        assert all(hashlib.sha256((root/p).read_bytes()).hexdigest()==sha for p,sha in before.items())
    out=ROOT/'logs/test/T13';out.mkdir(parents=True,exist_ok=True)
    proof={'随机种子':20261030,'随机行业':codes,'随机日期':dates,'九行手算核对':checks,'最新三行业补充':supplement,'两套口径无拼接':True,'隔离重跑新增0且原CSV字节不变':True,'Claude维护表SHA256':report['维护表SHA256'],'T8最后修订读取':{'源物理行数':sum(1 for _ in (ROOT/'data/资金轮动/日表.csv').open())-1,'源唯一键数':len(flow)}}
    dump(out/'独立核对.json',proof)
    lines=['# T13随机3行业×3日期手算核对','','固定种子20261030；直接按T8原成交额逐行业加总28分母，再独立相除；中久期无系数，适配留缺。','', '|日期|行业|代码|分子亿元|28分母亿元|手算占比|程序占比|中债20日bp|久期|手算适配bp|程序适配bp|','|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|']
    def v(x):return '缺' if x is None else str(round(x,10)) if isinstance(x,float) else str(x)
    for r in checks:lines.append('|'+ '|'.join(v(r[k]) for k in ('日期','行业','代码','分子亿元','分母亿元','手算占比','程序占比','中国20日bp','久期','手算适配bp','程序适配bp'))+'|')
    lines+=['','## 最新日三行业额外核对','', '|行业|成分家数|可评估|加速|手算样本占比|全行业占比|新闻命中|概念命中|','|---|---:|---:|---:|---:|---|---:|---:|']
    for r in supplement:
        q=r['季度手算'];lines.append('|'+ '|'.join(v(x) for x in [r['行业'],q['成分'],q['可评估'],q['加速'],q['样本比例'],q['全行业比例'],r['重要度2以上标签命中数'],r['概念出现次数']])+'|')
    lines+=['','季度逐只原值、同比、阈值判定及概念命中原名见JSON。EPS及新闻4周历史不足留缺，不用0补。全量回补两套CSV隔离重跑新增0，旧字节不变。','']
    atomic_text(out/'手算核对.md','\n'.join(lines));print('9行随机手算、最新3行业季报/热度/31口径及隔离幂等验证通过')


if __name__=='__main__':main()
