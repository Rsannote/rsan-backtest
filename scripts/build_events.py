#!/usr/bin/env python3
"""T9 历史大调整/反弹事件与当前数字对照；只读已存数据，不联网。"""
import argparse
import bisect
import csv
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import statistics

from auto_judgment_common import BJ, atomic_text, read_csv

ROOT=Path(__file__).resolve().parents[1]
VERSION='事件v1'
PUBLIC=['事件id','类型','对象','开始','结束','幅度','持续交易日','启动前60日涨幅','启动时成交额分位','启动时融资余额变化','启动时情绪温度','启动时中国10年国债','启动时美国10年国债','触发事件','传导链或上位类型','资金驱动或事件驱动','退潮原因','备注']
HUMAN=['触发事件','传导链或上位类型','传导链','资金驱动或事件驱动','退潮原因','备注']
FEATURES=['启动前60日涨幅','启动时成交额分位','启动时融资余额变化','启动时情绪温度','启动时中国10年国债','启动时美国10年国债']
EXTRA=['传导链','启动前120日涨幅','启动时20日成交额均值','结束时20日成交额均值','成交额变化','启动时融资余额亿','结束时融资余额亿','融资余额变化','成立日期','确认结束日期','状态','计算截止','特征来源','程序说明','程序版本','追加时间','更新原因']
FIELDS=PUBLIC+EXTRA


def finite(value):
    if value is None or str(value).strip() in ('','NaN','nan','None'):
        return None
    result=float(value)
    if not math.isfinite(result):raise ValueError('非有限数值')
    return result


def points(path,field):
    result={}
    for row in read_csv(path):
        day=dt.date.fromisoformat(row['date']).isoformat()
        if day in result:raise ValueError(f'{path}日期重复：{day}')
        value=finite(row.get(field))
        if value is not None:result[day]=(value,row)
    return result


def episodes(days,values,drawdown=.15,rebound=None):
    """交替的方向变化分段；确认日与事后极值日分别保留。"""
    rebound=drawdown if rebound is None else rebound
    if len(days)!=len(values) or not days or not 0<drawdown<1 or rebound<=0:
        raise ValueError('价格或阈值无效')
    if days!=sorted(set(days)) or any(not math.isfinite(v) or v<=0 for v in values):
        raise ValueError('价格须为唯一递增日期上的有限正数')
    direction=None;high=low=0;start=extreme=formed=None;result=[]
    def event(kind,start,end,formed,confirmed):
        return {'kind':kind,'start':start,'end':end,'formed':formed,'confirmed':confirmed,
                'amplitude':values[end]/values[start]-1,'duration':end-start}
    for i,value in enumerate(values):
        if direction is None:
            if value>values[high]:high=i
            if value<values[low]:low=i
            if 1-value/values[high]>=drawdown-1e-12:
                direction='down';start=high;extreme=i;formed=i
            elif value/values[low]-1>=rebound-1e-12:
                direction='up';start=low;extreme=i;formed=i
        elif direction=='down':
            if value<values[extreme]:extreme=i
            if value/values[extreme]-1>=rebound-1e-12:
                result.append(event('down',start,extreme,formed,i))
                direction='up';start=extreme;extreme=i;formed=i
        else:
            if value>values[extreme]:extreme=i
            if 1-value/values[extreme]>=drawdown-1e-12:
                result.append(event('up',start,extreme,formed,i))
                direction='down';start=extreme;extreme=i;formed=i
    if direction:
        result.append(event(direction,start,extreme,formed,None))
    return result


class Data:
    def __init__(self,root,now):
        self.root=Path(root);self.now=now;self.paths={}
        path=self.root/'data/行情库/指数/sh000300.csv';raw=read_csv(path)
        self.add_path(path)
        latest_day=now.date() if now.time()>=dt.time(18) else now.date()-dt.timedelta(days=1)
        selected={}
        for r in raw:
            d=dt.date.fromisoformat(r['date']).isoformat()
            if d in selected:raise ValueError('沪深300日期重复')
            if d<'2005-01-01' or d>latest_day.isoformat():continue
            close,factor=finite(r.get('close')),finite(r.get('factor'))
            if close is None or factor is None or close<=0 or factor<=0:raise ValueError('沪深300close/factor无效')
            selected[d]=close*factor
        self.days=sorted(selected);self.values=[selected[d] for d in self.days]
        if not self.days:raise ValueError('无已收尾沪深300行情')
        self.index={d:i for i,d in enumerate(self.days)}
        self.amount=self.load('data/市场状态/日指标.csv','成交额亿')
        self.margin=self.load('data/市场状态/融资余额.csv','融资余额亿')
        self.temperature=self.load('data/社区情绪/温度计.csv','温度')
        self.macro={}
        for name in ('中国10年国债','美国10年国债'):
            p=self.root/'data/宏观'/f'{name}.csv';self.add_path(p)
            rows=read_csv(p)
            if not rows:raise ValueError('宏观序列为空：'+name)
            for row in rows:
                if finite(row['value']) is None:raise ValueError('宏观序列数值缺失：'+name)
                dt.date.fromisoformat(row['date'])
                stamp=dt.datetime.fromisoformat(row['可见时刻'])
                if stamp.utcoffset() is None:raise ValueError('宏观可见时刻缺时区')
            self.macro[name]=rows
        self.cache={}

    def add_path(self,path):
        self.paths[str(path.relative_to(self.root))]=hashlib.sha256(path.read_bytes()).hexdigest()

    def load(self,name,field):
        path=self.root/name;self.add_path(path)
        return points(path,field)

    def known(self,series,cutoff):
        dates=sorted(series);i=bisect.bisect_right(dates,cutoff)-1
        return (dates[i],series[dates[i]][0],series[dates[i]][1]) if i>=0 else (None,None,{})

    def amount_mean(self,index):
        if index<19:return None
        dates=self.days[index-19:index+1]
        if any(d not in self.amount for d in dates):return None
        return statistics.fmean(self.amount[d][0] for d in dates)

    def features(self,index):
        if index in self.cache:return self.cache[index]
        day=self.days[index];cutoff=dt.datetime.fromisoformat(day+'T18:00:00').replace(tzinfo=BJ)
        result={k:None for k in FEATURES};evidence={};notes=[]
        for n in (60,120):
            result[f'启动前{n}日涨幅']=self.values[index]/self.values[index-n]-1 if index>=n else None
        result['启动时20日成交额均值']=self.amount_mean(index)
        if day in self.amount:
            history=[(d,v[0]) for d,v in sorted(self.amount.items()) if d<=day][-250:]
            if len(history)>=120:
                value=self.amount[day][0]
                result['启动时成交额分位']=sum(v<=value for _,v in history)/len(history)*100
            evidence['成交额']={'date':day,'path':'data/市场状态/日指标.csv','原始标记':self.amount[day][1].get('来源','未知')}
        # 余额最早从下一交易日使用，实际日期保留，不把缺报误当休市。
        date,balance,_=self.known(self.margin,self.days[index-1]) if index else (None,None,{})
        result['启动时融资余额亿']=balance
        previous=self.known(self.margin,self.days[index-6])[1] if index>=6 else None
        if balance is not None and previous not in (None,0):result['启动时融资余额变化']=balance/previous-1
        evidence['融资余额']={'date':date,'path':'data/市场状态/融资余额.csv','可见口径':'严格前一A股交易日及更早'}
        tdate,value,_=self.known(self.temperature,day)
        result['启动时情绪温度']=value;evidence['情绪']={'date':tdate,'path':'data/社区情绪/温度计.csv'}
        for name,rows in self.macro.items():
            visible={}
            for row in rows:
                if dt.datetime.fromisoformat(row['可见时刻'])<=cutoff:visible[row['date']]=row
            row=visible[max(visible)] if visible else None
            result['启动时'+name]=finite(row['value']) if row else None
            evidence[name]={'date':row['date'] if row else None,'可见时刻':row['可见时刻'] if row else None,'path':f'data/宏观/{name}.csv'}
        for key in FEATURES+['启动前120日涨幅','启动时20日成交额均值']:
            if result[key] is None:notes.append(key+'缺数据')
        if any('估算' in str(v) for v in evidence.values()):notes.append('成交额来源含回补估算')
        result['特征来源']=json.dumps(evidence,ensure_ascii=False,sort_keys=True)
        result['程序说明']='；'.join(notes)
        self.cache[index]=result
        return result


def record(data,event,dd):
    kind='大调整' if event['kind']=='down' else '大反弹'
    start,end=data.days[event['start']],data.days[event['end']]
    tag=f'D{round(dd*100)}' if kind=='大调整' else 'R20'
    result={k:'' for k in FIELDS}
    result.update(data.features(event['start']))
    formed=event['formed'] if kind=='大调整' else next(i for i in range(event['start'],event['end']+1) if data.values[i]/data.values[event['start']]-1>=.20-1e-12)
    result.update({'事件id':f'EVT-sh000300-{tag}-{start}','类型':kind,'对象':'沪深300','开始':start,'结束':end,
                   '幅度':event['amplitude'],'持续交易日':event['duration'],'成立日期':data.days[formed],
                   '确认结束日期':data.days[event['confirmed']] if event['confirmed'] is not None else '',
                   '状态':'已确认结束' if event['confirmed'] is not None else '待反向确认',
                   '计算截止':data.days[event['confirmed']] if event['confirmed'] is not None else data.days[-1],'程序版本':VERSION})
    at_end=data.features(event['end']);result['结束时20日成交额均值']=at_end['启动时20日成交额均值']
    a,b=result['启动时20日成交额均值'],result['结束时20日成交额均值']
    result['成交额变化']=b/a-1 if a not in (None,0) and b is not None else None
    a,b=result['启动时融资余额亿'],at_end['启动时融资余额亿'];result['结束时融资余额亿']=b
    result['融资余额变化']=b/a-1 if a not in (None,0) and b is not None else None
    if event['start']==0:result['程序说明']+='；样本左边界，之前峰谷未知'
    return result


def cell(value):
    if value is None:return ''
    if isinstance(value,float):return format(value,'.12g')
    return str(value)


def append_records(path,records,now):
    old=path.read_bytes().decode('utf-8') if path.exists() else ''
    existing=list(csv.DictReader(io.StringIO(old))) if old else []
    if old and next(csv.reader(io.StringIO(old)))!=FIELDS:raise ValueError('事件CSV表头不符，不覆盖')
    latest={r['事件id']:r for r in existing};additions=[]
    for raw in records:
        item={k:cell(raw.get(k)) for k in FIELDS};previous=latest.get(item['事件id'])
        if previous:
            for key in HUMAN:item[key]=previous[key]
            compared=[k for k in FIELDS if k not in HUMAN+['追加时间','更新原因']]
            if all(previous[k]==item[k] for k in compared):continue
        item['追加时间']=now.isoformat();item['更新原因']='输入或状态变化，保留旧行' if previous else '首次记录'
        additions.append(item)
    buffer=io.StringIO(newline='');writer=csv.DictWriter(buffer,fieldnames=FIELDS,lineterminator='\n')
    if not old:writer.writeheader()
    writer.writerows(additions)
    atomic_text(path,old+('' if not old or old.endswith('\n') or not additions else '\n')+buffer.getvalue())
    return len(additions)


def distances(current,records):
    eligible=[];excluded=[]
    for row in records:
        missing=[f for f in FEATURES if row.get(f) is None or row.get(f)=='']
        if row['状态']!='已确认结束':missing.append('事件未确认结束')
        if missing:excluded.append({'事件id':row['事件id'],'原因':missing})
        else:eligible.append(row)
    current_missing=[f for f in FEATURES if current.get(f) is None]
    if current_missing or not eligible:return [],excluded,{'当前缺失':current_missing,'候选数':len(eligible)}
    means={f:statistics.fmean(float(r[f]) for r in eligible) for f in FEATURES}
    deviations={f:statistics.pstdev(float(r[f]) for r in eligible) for f in FEATURES}
    constants=[f for f in FEATURES if deviations[f]==0]
    if len(constants)==len(FEATURES):
        return [],excluded,{'当前缺失':[],'候选数':len(eligible),'原因':'全部维度为常数，无法标准化，不输出虚假零距离','常数维':constants}
    scored=[]
    for row in eligible:
        contributions={f:((float(current[f])-float(row[f]))/deviations[f])**2 if deviations[f]>0 else 0 for f in FEATURES}
        scored.append({'事件id':row['事件id'],'类型':row['类型'],'开始':row['开始'],'结束':row['结束'],
                       '距离':math.sqrt(sum(contributions.values())),'平方差贡献':contributions,
                       '历史特征':{f:row[f] for f in FEATURES},'像在哪、不像在哪':'【待判读】'})
    scored.sort(key=lambda r:(r['距离'],r['事件id']))
    return scored,excluded,{'当前缺失':[],'候选数':len(eligible),'均值':means,'总体标准差':deviations,'特征':FEATURES,'常数维':constants}


def run(root=ROOT,now=None):
    root=Path(root).resolve();now=(now or dt.datetime.now(BJ)).astimezone(BJ);data=Data(root,now)
    output=root/'data/事件库';output.mkdir(parents=True,exist_ok=True)
    main=[record(data,event,.15) for event in episodes(data.days,data.values,.15) if event['kind']=='down' or event['amplitude']>=.20-1e-12]
    declines=[r for r in main if r['类型']=='大调整'];rebounds=[r for r in main if r['类型']=='大反弹']
    alternate=[record(data,event,.10) for event in episodes(data.days,data.values,.10) if event['kind']=='down']
    # 所有计算先完成，再准备追加；人工文字在写入时继承。
    inserted={name:append_records(output/name,rows,now) for name,rows in [('大调整.csv',declines),('大反弹.csv',rebounds),('大调整_10.csv',alternate)]}
    current=data.features(len(data.days)-1)
    ranked,excluded,scale=distances(current,main)
    other,other_excluded,other_scale=distances(current,alternate)
    report={'title':'历史事件库','generated_at':now.isoformat(),'data_date':data.days[-1],'status':'正常','errors':[],
            'counts':{'大调整15':len(declines),'大反弹20':len(rebounds),'大调整10':len(alternate)},'inserted':inserted,
            '输入SHA256':data.paths,'当前特征':current,'主表排名':ranked,'未参与主表':excluded,'标准化':scale,
            '10%排名':other,'未参与10%':other_excluded,'10%标准化':other_scale,'程序版本':VERSION}
    if scale.get('当前缺失'):
        report['status']='失败';report['errors']=['当前对照特征缺数据：'+'、'.join(scale['当前缺失'])]
    lines=['# 当前对照','',f'生成时间：{now.isoformat()}；特征基准：{data.days[-1]}18:00（最近已存且收尾的A股日，不一定是今天）。','',
           '|当前特征|数值|','|---|---:|',*[f'|{f}|{cell(current[f]) or "缺数据"}|' for f in FEATURES],
           '', '源日期与可见时刻：`'+current['特征来源']+'`。','', '## 主表最接近的5个已闭合事件','',
           '|事件id|类型|开始|结束|标准化欧氏距离|像在哪、不像在哪|','|---|---|---|---|---:|---|']
    for r in ranked[:5]:lines.append(f"|{r['事件id']}|{r['类型']}|{r['开始']}|{r['结束']}|{r['距离']:.8g}|【待判读】|")
    lines+=['',f'完整候选{len(ranked)}个，缺失或未确认结束{len(excluded)}个；只用历史候选拟合均值/标准差，缺失不补0、不按少数维度混排。',
            '', '## 未参与主对照的事件','',*[f"- {r['事件id']}：{'、'.join(r['原因'])}" for r in excluded],
            '', '## 10%敏感性表单独对照','', '|事件id|开始|结束|标准化欧氏距离|解释|','|---|---|---|---:|---|']
    for r in other[:5]:lines.append(f"|{r['事件id']}|{r['开始']}|{r['结束']}|{r['距离']:.8g}|【待判读】|")
    lines+=['','完整数值、标准化参数和各维贡献见当前对照.json；事件识别为事后统计，不是预测或交易回测。','']
    atomic_text(output/'当前对照.md','\n'.join(lines))
    atomic_text(output/'当前对照.json',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    schema=(root/'system/事件库格式.md').read_text(encoding='utf-8')
    summary=[schema,'\n# 事件总表\n','|表|事件id|开始|结束（极值）|幅度|持续交易日|成立日期|确认结束|状态|','|---|---|---|---|---:|---:|---|---|---|']
    for name,rows in [('大调整15%',declines),('大反弹20%',rebounds),('大调整10%',alternate)]:
        for r in rows:summary.append(f"|{name}|{r['事件id']}|{r['开始']}|{r['结束']}|{r['幅度']:.4%}|{r['持续交易日']}|{r['成立日期']}|{r['确认结束日期']}|{r['状态']}|")
    atomic_text(output/'说明.md','\n'.join(summary)+'\n')
    atomic_text(root/'logs/事件库_最新.json',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    atomic_text(root/'logs/事件库_最新.md',f"# 事件库运行\n\n北京时间：{now.isoformat()}\n\n数据基准：{data.days[-1]}；状态：{report['status']}\n\n{json.dumps(report['counts'],ensure_ascii=False)}\n\n当前完整候选：{len(ranked)}；前五数字见data/事件库/当前对照.md。\n\n{json.dumps(report['errors'],ensure_ascii=False)}\n")
    print('事件库：'+json.dumps(report['counts'],ensure_ascii=False)+f'；主对照完整候选{len(ranked)}',flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);args=parser.parse_args()
    try:
        result=run(args.root)
        return 1 if result['errors'] else 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        now=dt.datetime.now(BJ);root=args.root.resolve()
        atomic_text(root/'logs/事件库_最新.json',json.dumps({'generated_at':now.isoformat(),'data_date':now.date().isoformat(),'status':'失败','errors':[str(exc)]},ensure_ascii=False,indent=2)+'\n')
        parser.exit(1,f'事件库失败：{exc}\n')


if __name__=='__main__':raise SystemExit(main())
