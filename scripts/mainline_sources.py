"""T13只读输入适配；不联网、不改Claude表，不把缺来源变成0。"""
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re

import numpy as np
import pandas as pd

from earnings_history import deadline, load_v3, visibility
from earnings_coverage import listed_members, read_listings, read_members

BJ=dt.timezone(dt.timedelta(hours=8))
NEW={'801960','801970','801980'}


def csv_rows(path):
    if not Path(path).is_file():return None
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames:raise ValueError('CSV无表头：'+str(path))
        return list(reader)


def number(value):
    try:
        x=float(value);return x if math.isfinite(x) else None
    except (TypeError,ValueError):return None


def timestamp(value):
    x=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    return x.replace(tzinfo=BJ) if x.tzinfo is None else x.astimezone(BJ)


def registry(root):
    rows=csv_rows(Path(root)/'system/行业清单.csv')
    required={'申万代码','行业','久期','风格','新闻标签','概念关键词','主线判定'}
    if not rows or not required.issubset(rows[0]):raise ValueError('Claude行业清单缺失/表头不完整，拒绝创建空表')
    out={r['申万代码']:r for r in rows}
    if len(out)!=31 or len(out)!=len(rows) or not NEW.issubset(out):raise ValueError('Claude行业清单必须为完整31行业，无重复')
    return out


def calendar(root):
    rows=csv_rows(Path(root)/'system/催化日历.csv')
    if rows is None:raise ValueError('Claude催化日历不存在，拒绝创建空表')
    result=[]
    for r in rows:
        exact=bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',r.get('日期','')))
        if exact:dt.date.fromisoformat(r['日期'])
        result.append(dict(r,具体日期=exact))
    return result


def split(value):return [x.strip() for x in str(value or '').split(';') if x.strip()]


class Sources:
    def __init__(self,root,names):
        self.root=Path(root);self.names=names;self.news={};self.concepts={};self.market={};self.rates={};self.eps={};self.quarter_cache={};self.daily_cache={};self.eps_visibility={}
        self.catalysts=calendar(root)
        self.load_news();self.load_concepts();self.load_market();self.load_rates();self.load_eps();self.load_quarters()
    def load_news(self):
        for p in sorted((self.root/'data/新闻').glob('????-??/????-??-??.jsonl')):
            if not p.stat().st_size:continue
            records={}
            for line in p.read_text().splitlines():
                if line.strip():
                    r=json.loads(line);records[r['id']]=r
            self.news[p.stem]=list(records.values())
    def load_concepts(self):
        for p in sorted((self.root/'data/社区情绪').glob('????-??-??/同花顺热门概念.csv')):
            log=p.parent/'运行记录.txt';rows=csv_rows(p)
            if rows is None or not log.exists():continue
            text=log.read_text();match=re.search(r'抓取时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})',text)
            if not match or '同花顺热门概念' not in text.split('失败：')[0]:continue
            self.concepts[p.parent.name]=(rows,match[1])
    def load_market(self):
        for p in sorted((self.root/'data/市场状态').glob('????-??/????-??-??.json')):
            r=json.loads(p.read_text());self.market[r['数据日期']]=r
    def load_rates(self):
        for r in csv_rows(self.root/'data/宏观/派生.csv') or []:
            if r.get('指标') not in ('中国10年国债','美国10年国债'):continue
            if not r.get('可见时刻'):continue
            key=(r['date'],r['指标']);self.rates.setdefault(key,[]).append(r)
    def load_eps(self):
        for r in csv_rows(self.root/'data/一致预期/行业上修.csv') or []:
            day=r['日期'];self.eps[(day,r['申万一级代码'],r['预测年度'])]=r
            if day not in self.eps_visibility:
                snapshot=csv_rows(self.root/'data/一致预期'/day[:7]/(day+'.csv'))
                stamps=[x.get('抓取时间','') for x in snapshot or []]
                self.eps_visibility[day]=max(stamps) if stamps and all(stamps) else None
    def load_quarters(self):
        self.members={};self.quarters={}
        p=self.root/'data/v3/申万成分.json'
        if p.exists():self.members=json.loads(p.read_text())
        self.members=read_members(self.root,self.members)
        self.listings=read_listings(self.root)
        self.quarter_details={}
        for p in (self.root/'data/财报/单季').glob('*.csv'):self.quarters[p.stem]=csv_rows(p) or []
        # 新目录按报告期最后修订覆盖股票池缓存；缺值修订不回退旧值。
        for p in (self.root/'data/财报/全市场单季').glob('*.csv'):
            existing=self.quarters.get(p.stem,[])
            # 实际公告日是已知信息，新源没有该列时也不能将延迟披露提前。
            announcements={}
            for r in existing:
                if r.get('公告日'):
                    dt.date.fromisoformat(r['公告日'])
                    announcements[r['报告期']]=max(announcements.get(r['报告期'],''),r['公告日'])
            revisions={r['报告期']:r for r in existing}
            for row in csv_rows(p) or []:
                if row.get('法定截止')!=deadline(row['报告期']).isoformat() or row.get('可见时刻')!=visibility(row['报告期']):
                    raise ValueError('全市场季报可见边界错误：'+str(p))
                row=dict(row)
                if row['报告期'] in announcements:row['公告日']=announcements[row['报告期']]
                revisions[row['报告期']]=row
            self.quarters[p.stem]=list(revisions.values())
        self.num,self.q_signal,self.qcfg=load_v3(self.root)
        self.stock_quarter_cache={};self.quarter_notes={}
    def quarter(self,day,code):
        key=(day,code)
        if key in self.quarter_cache:return self.quarter_cache[key]
        member=self.members.get(code);out={'业绩_利润加速占比':None,'业绩_样本加速占比':None,'业绩_成分家数':None,'业绩_可评估家数':None,'业绩_加速家数':None,'业绩_季报覆盖率':None,'业绩_季报期':''}
        if not member or not member.get('date'):return out
        codes=sorted({str(r['证券代码']).zfill(6) for r in member['rows']});asof=dt.date.fromisoformat(day);passed=0;valid=0;periods=set()
        eligible=[]
        for year in (asof.year-1,asof.year):
            for month,dd in ((3,31),(6,30),(9,30),(12,31)):
                report=dt.date(year,month,dd);m,d={3:(4,30),6:(8,31),9:(10,31),12:(4,30)}[month]
                if dt.date(year+(month==12),m,d)<asof:eligible.append(report.isoformat())
        latest_period=max(eligible) if eligible else ''
        # 资金日表早于首个可见季度时，没有可用于冻结上市分母的规则日。
        # 保持季度维度为“缺”，不能把空字符串送进 visibility/deadline 猜一个日期。
        if not latest_period:
            self.quarter_cache[key]=out
            return dict(out)
        listing=listed_members(codes,latest_period,self.listings)
        for stock in listing['已上市代码']:
            original=self.quarters.get(stock,[])
            # 有实际公告日的旧缓存仍逐日过滤；截止日推定数据按季度复用计算。
            stock_key=(stock,day if any(r.get('公告日') for r in original) else latest_period)
            if stock_key not in self.stock_quarter_cache:
                rows=[]
                for r in original:
                    announcement=r.get('公告日','')
                    if announcement and re.fullmatch(r'\d{4}-\d{2}-\d{2}',announcement) and announcement>=day:continue
                    if r.get('可见时刻') and timestamp(r['可见时刻'])>dt.datetime.combine(asof,dt.time.max,BJ):continue
                    rows.append(r)
                self.stock_quarter_cache[stock_key]=self.q_signal(rows,asof)
            sig=self.stock_quarter_cache[stock_key]
            if not sig or sig['q']!=latest_period or any(not math.isfinite(sig[k]) for k in ('g','gprev','grev')):continue
            valid+=1;periods.add(sig['q'])
            passed+=int(self.qcfg['g_min']<sig['g']<=self.qcfg['g_max'] and sig['acc']>0 and sig['gprev']>self.qcfg['prev_min'] and sig['grev']>0)
        denominator=listing['已上市分母']
        coverage=valid/denominator if denominator else None
        out.update({'业绩_成分家数':len(codes),'业绩_可评估家数':valid,'业绩_加速家数':passed if valid else None,'业绩_季报覆盖率':coverage,'业绩_样本加速占比':passed/valid if valid else None,'业绩_利润加速占比':passed/valid if denominator and valid>=10 and valid*5>=denominator*3 else None,'业绩_季报期':';'.join(sorted(periods))})
        self.quarter_details[key]={'date':day,'code':code,'行业':self.names[code]['行业'],'季报期':latest_period,
            **{k:listing[k] for k in ('当前成分总量','已上市分母','未上市家数','上市日期未知家数','规则可见日')},
            '可评估家数':valid,'加速家数':passed if valid else None,'季报覆盖率':coverage,
            '行业利润加速占比':out['业绩_利润加速占比'],
            '说明':'当前成分快照回溯有幸存者偏差；上市日期严格早于本季度规则可见日；未知上市日排除并单列'}
        self.quarter_notes[key]=';D='+deadline(latest_period).isoformat()+';V='+visibility(latest_period)+';M='+member.get('_signal_date',member['date'])+';L='+listing['规则可见日']+';N='+str(denominator)+';U='+str(listing['上市日期未知家数'])
        self.quarter_cache[key]=out;return dict(out)
    def for_day(self,day,code):
        cache_key=(day,code)
        if cache_key in self.daily_cache:return dict(self.daily_cache[cache_key])
        r=self.names[code];out={};cutoff=day+'T23:59:59+08:00';end=dt.datetime.fromisoformat(cutoff)
        for label in ('中国','美国'):
            matches=self.rates.get((day,label+'10年国债'),[])
            visible=[x for x in matches if dt.datetime.fromisoformat(x['可见时刻'])<=end]
            x=visible[-1] if visible else {};delta=number(x.get('value'))
            out['环境_'+label+'10年20日变化bp']=delta
            out['环境_'+label+'可见时刻']=x.get('可见时刻','')
            direction={'长':-1,'短':1,'中':0}.get(r['久期'])
            out['环境_'+label+'久期适配bp']=0 if direction==0 else delta*direction if delta is not None and direction is not None else None
        market=self.market.get(day,{})
        out['环境_大盘攻守']=market.get('市场',{}).get('结论','');out['市场可见时刻']=market.get('生成时间','')
        for year,tag in [(day[:4],'本年'),(str(int(day[:4])+1),'次年')]:
            eps=self.eps.get((day,code,year),{})
            observed=self.eps_visibility.get(day)
            if not observed or timestamp(observed)>end:eps={}
            out['业绩_EPS'+tag+'上修占比']=number(eps.get('上修占比'))
            out['业绩_EPS'+tag+'可比家数']=number(eps.get('可比家数'))
        out.update(self.quarter(day,code))
        tags=set(split(r['新闻标签']));records=self.news.get(day)
        def count(d,score=2):
            entries=self.news.get(d)
            if entries is None or not tags:return None
            return sum(number(x.get('score')) is not None and float(x['score'])>=score and bool(tags.intersection(x.get('tags',{}).get('行业',[]))) for x in entries)
        out['热度_新闻当日条数']=count(day);out['热度_重要度3当日条数']=count(day,3)
        now=dt.date.fromisoformat(day);dates=[(now-dt.timedelta(days=k)).isoformat() for k in range(56)]
        counts=[count(d) for d in dates]
        complete=all(x is not None for x in counts)
        a=sum(counts[:28]) if complete else None;b=sum(counts[28:]) if complete else None
        out.update({'热度_新闻近28日条数':a,'热度_新闻前28日条数':b,'热度_新闻4周差量':a-b if complete else None,'热度_新闻4周变化率':a/b-1 if complete and b else None})
        concepts=self.concepts.get(day);keywords=split(r['概念关键词'])
        if concepts and concepts[1][:10]>day:concepts=None
        out['热度_概念出现次数']=sum(any(k.casefold() in str(x.get('name','')).casefold() for k in keywords) for x in concepts[0]) if concepts and keywords else None
        out['社区可见时刻']=concepts[1] if concepts else ''
        events=[x for x in self.catalysts if x['具体日期'] and x['日期']==day and ('全市场' in split(x['相关行业']) or r['行业'] in split(x['相关行业']))]
        # 人工日历只列已登记命中事件，不把未登记断言为全市场没有事件。
        out['热度_已登记当日催化数']=len(events) if events else None
        out['热度_已登记当日催化']='；'.join(x['事件'] for x in events)
        self.daily_cache[cache_key]=dict(out)
        return out
