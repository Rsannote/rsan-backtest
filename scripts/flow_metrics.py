"""离线资金/拥挤度指标；28/31口径独立，不填缺失，不混入ETF。"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from flow_source import NEW, START, START31


SW_COLUMNS=['date','code','name','来源','收盘','成交额原值']
for _s in ('28','31'):
    SW_COLUMNS += ['占比_'+_s,'总额_'+_s,'占比5日变化_'+_s,'占比5日均_'+_s,'占比20日变化_'+_s,'占比20日均_'+_s,
                   '占比5日均较20日均_'+_s,'占比5日变化排名_'+_s,'占比60日分位_'+_s,'占比一年分位_'+_s]
SW_COLUMNS += ['相对沪深300_5','相对沪深300_20','相对沪深300_60']
for _s in ('28','31'):SW_COLUMNS += ['相对强弱排名_20_'+_s,'相对强弱排名_60_'+_s,'动量排名_20_'+_s]
SW_COLUMNS += ['偏离60日线','数据说明']
ETF_COLUMNS=['date','code','name','来源','近似口径','ETF_成交额代理原值','ETF_占比','ETF_可用只数','ETF_占比5日变化','ETF_占比20日变化',
             'ETF_相对沪深300_5','ETF_相对沪深300_20','ETF_相对沪深300_60','ETF_占比60日分位','ETF_占比一年分位','ETF_偏离60日线']


def series(path,column):
    frame=pd.read_csv(path,dtype={'date':str})
    frame=frame.drop_duplicates('date',keep='last').set_index('date').sort_index()
    values=pd.to_numeric(frame[column],errors='raise')
    if not np.isfinite(values).all():raise ValueError(str(path)+'非有限数值')
    return values


def read_inputs(root,target):
    root=Path(root);benchmark=pd.read_csv(root/'data/行情库/指数/sh000300.csv',dtype={'date':str}).set_index('date').sort_index()
    if benchmark.index.duplicated().any():raise ValueError('基准日期重复')
    benchmark=(benchmark['close']*benchmark['factor']).loc[:target]
    if not np.isfinite(benchmark).all() or (benchmark<=0).any():raise ValueError('基准价无效')
    base=root/'data/资金轮动/原始';registry=base/'行业清单.csv'
    if not registry.exists():return benchmark,{},pd.DataFrame(index=benchmark.index),pd.DataFrame(index=benchmark.index)
    names=pd.read_csv(registry,dtype={'code':str}).drop_duplicates('code',keep='last').set_index('code')['name'].to_dict()
    if len(names)!=31 or not NEW.issubset(names):raise ValueError('原始行业清单不是审核的31行业')
    closes={};amounts={}
    for code in sorted(names):
        path=base/'申万'/f'{code}.csv'
        closes[code]=series(path,'close');amounts[code]=series(path,'amount')
    C=pd.DataFrame(closes).reindex(benchmark.index);A=pd.DataFrame(amounts).reindex(benchmark.index)
    return benchmark,names,C,A


def rank_complete(values,codes):
    subset=values.reindex(columns=sorted(codes));rank=subset.rank(axis=1,ascending=False,method='first')
    rank.loc[subset.count(axis=1)!=len(codes),:]=np.nan
    return rank.reindex(columns=values.columns)


def share_matrix(amount,codes,start):
    subset=amount.reindex(columns=sorted(codes));total=subset.sum(axis=1,min_count=len(codes))
    total=total.where((total>0)&(total.index>=start))
    return subset.div(total,axis=0).reindex(columns=amount.columns),total


def valid_rolling(values,window,method='mean'):
    """保留固定交易日窗口，完整跨度内至少90%有效；绝不压缩/填补缺日。"""
    minimum=(window*9+9)//10
    roll=values.rolling(window,min_periods=minimum)
    result=roll.mean() if method=='mean' else roll.rank(method='max',pct=True)*100
    # 暖启动不以225个观测假装已有250日历史；新31口径独立起算。
    def mature(series):
        valid=series.notna().to_numpy();positions=np.arange(len(series));first=int(np.argmax(valid)) if valid.any() else len(series)
        return pd.Series(positions-first+1>=window,index=series.index)
    mask=values.apply(mature) if isinstance(values,pd.DataFrame) else mature(values)
    return result.where(mask)


def window_notes(values,windows,prefix):
    """缺日保留在交易日轴，暖启动与成熟窗口缺口分别标注。"""
    notes=pd.DataFrame('',index=values.index,columns=values.columns)
    positions=np.arange(len(values))
    for window in windows:
        counts=values.rolling(window,min_periods=1).count()
        for code in values:
            first=values[code].first_valid_index()
            if first is None:continue
            begin=values.index.get_loc(first);mature=positions-begin+1>=window
            text=pd.Series('',index=values.index)
            if window in (60,250):text.loc[(positions>=begin)&~mature]=f'{prefix}{window}日窗口不足{window}交易日；'
            missing=window-counts[code];mask=mature&(missing>0)
            text.loc[mask]=f'{prefix}{window}日窗口内缺 '+missing.loc[mask].astype(int).astype(str)+' 天；'
            notes[code]+=text
    return notes


def compute_sw(C,A,benchmark,names):
    if not names:return pd.DataFrame(columns=SW_COLUMNS),{}
    codes28=sorted(set(names)-NEW);codes31=sorted(names);metrics={};shares={}
    for scope,codes,start in [('28',codes28,START),('31',codes31,START31)]:
        S,total=share_matrix(A,codes,start);shares[scope]=S
        metrics['占比_'+scope]=S;metrics['总额_'+scope]=total
        for n in (5,20):
            metrics[f'占比{n}日变化_'+scope]=S-S.shift(n)
            metrics[f'占比{n}日均_'+scope]=valid_rolling(S,n)
        metrics['占比5日均较20日均_'+scope]=metrics['占比5日均_'+scope]/metrics['占比20日均_'+scope]-1
        metrics['占比5日变化排名_'+scope]=rank_complete(metrics['占比5日变化_'+scope],codes)
        for n,label in [(60,'60日分位'),(250,'一年分位')]:metrics['占比'+label+'_'+scope]=valid_rolling(S,n,'rank')
    relative={}
    for n in (5,20,60):relative[n]=(C/C.shift(n)-1).sub(benchmark/benchmark.shift(n)-1,axis=0);metrics[f'相对沪深300_{n}']=relative[n]
    for scope,codes in [('28',codes28),('31',codes31)]:
        for n in (20,60):metrics[f'相对强弱排名_{n}_'+scope]=rank_complete(relative[n],codes)
        metrics['动量排名_20_'+scope]=metrics['相对强弱排名_20_'+scope]
    metrics['偏离60日线']=C/valid_rolling(C,60)-1
    notes=window_notes(C,[60],'收盘')
    for scope in ('28','31'):notes=notes+window_notes(shares[scope],[5,20,60,250],'占比_'+scope+'：')
    parts=[]
    dates=C.index[C.index>=START]
    for code in sorted(names):
        index=dates[dates>=START31] if code in NEW else dates
        part=pd.DataFrame({'date':index,'code':code,'name':names[code],'来源':'申万官网'},index=index)
        part['收盘']=C[code].reindex(index);part['成交额原值']=A[code].reindex(index)
        for label,value in metrics.items():part[label]=value[code].reindex(index) if isinstance(value,pd.DataFrame) else value.reindex(index)
        part['数据说明']=np.where(part['收盘'].isna()|part['成交额原值'].isna(),'申万当日缺数据；不填ETF或旧值；','')+notes[code].reindex(index).fillna('')
        parts.append(part)
    return pd.concat(parts,ignore_index=True).sort_values(['date','code'])[SW_COLUMNS],metrics


def compute_etf(root,benchmark):
    root=Path(root);listing=pd.read_csv(root/'data/标的清单.csv',dtype=str);listing=listing[listing['group']=='行业ETF']
    C={};A={};names={}
    for row in listing.to_dict('records'):
        code=row['code'];path=root/'data/行情库/ETF'/f'{code}.csv';names[code]=row['name']
        if not path.exists():continue
        df=pd.read_csv(path,dtype={'date':str}).set_index('date').sort_index()
        if df.index.duplicated().any():raise ValueError(code+'ETF日期重复')
        C[code]=(df['close']*df['factor']).where(df['close']>0);A[code]=(df['volume']*df['close']).where(df['volume']>=0)
    if not C:return pd.DataFrame(columns=ETF_COLUMNS)
    C=pd.DataFrame(C).reindex(benchmark.index);A=pd.DataFrame(A).reindex(benchmark.index)
    total=A.sum(axis=1,min_count=1).where(lambda s:s>0);S=A.div(total,axis=0);parts=[]
    for code in sorted(C):
        valid=C[code].notna()&A[code].notna();index=C.index[valid]
        p=pd.DataFrame({'date':index,'code':code,'name':names[code],'来源':'ETF 近似','近似口径':'volume×未复权close代理；非真实申万成交额'},index=index)
        p['ETF_成交额代理原值']=A[code].reindex(index);p['ETF_占比']=S[code].reindex(index);p['ETF_可用只数']=A.count(axis=1).reindex(index)
        for n in (5,20):p[f'ETF_占比{n}日变化']=(S[code]-S[code].shift(n)).reindex(index)
        for n in (5,20,60):p[f'ETF_相对沪深300_{n}']=((C[code]/C[code].shift(n)-1)-(benchmark/benchmark.shift(n)-1)).reindex(index)
        p['ETF_占比60日分位']=valid_rolling(S[code],60,'rank').reindex(index)
        p['ETF_占比一年分位']=valid_rolling(S[code],250,'rank').reindex(index)
        p['ETF_偏离60日线']=(C[code]/valid_rolling(C[code],60)-1).reindex(index);parts.append(p)
    return pd.concat(parts,ignore_index=True).sort_values(['date','code'])[ETF_COLUMNS]


def unit_check(root,A,target):
    market=pd.read_csv(Path(root)/'data/市场状态/日指标.csv',dtype={'date':str}).set_index('date')
    total=A.sum(axis=1,min_count=31);common=sorted(d for d in total.dropna().index if START31<=d<=target and d in market.index)[-10:]
    rows=[]
    for day in common:
        value=float(market.loc[day,'成交额亿']);amount=float(total.loc[day]);deviation=amount/value-1
        rows.append({'date':day,'申万31合计原值':amount,'市场成交额亿':value,'市场来源':market.loc[day,'来源'],'相对偏差':deviation,'±15%内':abs(deviation)<=.15})
    return {'样本选择':'两源共有、31行业完整的最近10日，不按差异挑选','rows':rows,'全部通过':len(rows)==10 and all(r['±15%内'] for r in rows),
            '口径':'bargainsum与亿元量级核对，比例分母仍用自身行业合计；回补估算不冒充官方交叉认证'}
