"""固定28口径的描述性条件统计；不产生交易信号或回测。"""
import numpy as np
import pandas as pd

from flow_source import NEW, START


def distribution(values):
    v=np.asarray(values,dtype=float);v=v[np.isfinite(v)]
    if not len(v):return {'样本数':0,'均值':None,'中位数':None,'胜率':None,'25分位':None,'75分位':None}
    return {'样本数':len(v),'均值':float(v.mean()),'中位数':float(np.median(v)),'胜率':float((v>0).mean()),'25分位':float(np.quantile(v,.25)),'75分位':float(np.quantile(v,.75))}


def describe(C,benchmark,names,metrics):
    if not names:return {'来源':'固定28行业、占比5日变化排名','事件表现':[],'领先滞后':[],'说明':'没有申万数据，不计算'},pd.DataFrame()
    codes=sorted(set(names)-NEW);rank=metrics['占比5日变化排名_28'][codes]
    entry=(rank.le(5)&rank.shift(1).gt(5)).astype(float).where(rank.notna()&rank.shift(1).notna())
    entry=entry.loc[entry.index>=START];performance=[]
    for code in codes:
        for n in (5,10,20):
            future=(C[code].shift(-n)/C[code]-1)-(benchmark.shift(-n)/benchmark-1)
            complete=(C[code].rolling(n+1,min_periods=n+1).count().shift(-n)==n+1)&(benchmark.rolling(n+1,min_periods=n+1).count().shift(-n)==n+1)
            future=future.where(complete)
            values=future.reindex(entry.index).where(entry[code]==1).dropna()
            performance.append({'code':code,'行业':names[code],'后续交易日':n,**distribution(values)})
    pairs=[]
    for n in (5,10,20):
        ahead={}
        for b in codes:
            windows=pd.concat([entry[b].shift(-k) for k in range(1,n+1)],axis=1)
            ahead[b]=windows.max(axis=1).where(windows.count(axis=1)==n)
        for a in codes:
            for b in codes:
                if a==b:continue
                valid=entry[a].notna()&ahead[b].notna();condition=valid&(entry[a]==1)
                base_n=int(valid.sum());n_condition=int(condition.sum());hits=int(ahead[b].loc[condition].sum())
                baseline=float(ahead[b].loc[valid].mean()) if base_n else None;prob=hits/n_condition if n_condition else None
                pairs.append({'A代码':a,'A行业':names[a],'B代码':b,'B行业':names[b],'后续窗口':n,'条件样本':n_condition,'B随后进入次数':hits,
                              '条件概率':prob,'基准样本':base_n,'无条件基准':baseline,'概率差':prob-baseline if prob is not None and baseline is not None else None})
    return {'来源':'固定28行业、占比5日变化排名由>5进入≤5；前日未知不算进入','事件表现':performance,
            '说明':'仅描述统计，窗口可重叠；未检验显著性，不是预测、因果或信号测试。解释【待判读】。'},pd.DataFrame(pairs)


def markdown(report,pairs):
    lines=['# 资金轮动描述统计','',report['来源'],'',report['说明'],'',
           '|代码|行业|后续交易日|样本数|均值|中位数|胜率|25分位|75分位|','|---|---|---:|---:|---:|---:|---:|---:|---:|']
    def f(v):return '缺' if v is None else str(round(v,8)) if isinstance(v,float) else str(v)
    for r in report['事件表现']:lines.append('|'+ '|'.join(f(r[k]) for k in ('code','行业','后续交易日','样本数','均值','中位数','胜率','25分位','75分位'))+'|')
    lines+=['','## 简单领先滞后条件计数','', '全量配对及基准见领先滞后.csv/json，不筛收益最好的阈值。以下仅按A、B代码和窗口列前20条便于抽查。','',
           '|A|B|窗口|条件样本|随后进入次数|条件概率|无条件基准|','|---|---|---:|---:|---:|---:|---:|']
    for r in pairs.head(20).to_dict('records'):lines.append('|'+ '|'.join(f(r[k]) for k in ('A代码','B代码','后续窗口','条件样本','B随后进入次数','条件概率','无条件基准'))+'|')
    return '\n'.join(lines)+'\n'
