#!/usr/bin/env python3
"""T8仅探测：申万一级日线和原始成交额；只能Actions联网，只写logs。"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.parse
import requests

from auto_judgment_common import BJ, TradingCalendar, atomic_text
from probe_consensus import HTTP

ROOT=Path(__file__).resolve().parents[1]
SW='https://www.swsresearch.com/institute-sw/api/index_publish/'


class FlowHTTP(HTTP):
    def get(self,address,encoding='utf-8'):
        if not address.startswith(SW):return super().get(address,encoding)
        if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('仅允许Actions联网')
        if time.monotonic()>self.deadline-20:raise TimeoutError('预算不足，未发起请求')
        time.sleep(.6);started=time.monotonic()
        entry={'url':address,'北京时间':dt.datetime.now(BJ).isoformat(),'HTTPS证书验证':True,'客户端':'requests标准CA包'};self.trace.append(entry)
        try:
            with requests.get(address,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.swsresearch.com/'},timeout=12,stream=True) as response:
                entry['HTTP']=response.status_code;response.raise_for_status()
                raw=b''
                for chunk in response.iter_content(65536):
                    raw+=chunk
                    if len(raw)>2*1024*1024:raise ValueError('响应超过2MiB')
            entry.update(字节数=len(raw),SHA256=hashlib.sha256(raw).hexdigest(),内容开头=raw[:350].decode(encoding,errors='replace'))
            return raw.decode(encoding)
        except Exception as exc:entry['错误']=type(exc).__name__+': '+str(exc);raise
        finally:entry['耗时秒']=round(time.monotonic()-started,3)


def number(value,positive=False):
    if value is None or str(value).strip() in ('','--','-'):raise ValueError('缺数值')
    n=float(value)
    if not math.isfinite(n) or n<0 or (positive and n==0):raise ValueError('非法日线数值')
    return n


def industry_list(text):
    obj=json.loads(text);data=obj['data'];rows=data['results']
    if len(rows)!=int(data['count']):raise ValueError('一级行业列表未取全')
    result={}
    for row in rows:
        code=str(row.get('swindexcode',row.get('swIndexCode','')))
        name=row.get('swindexname',row.get('swIndexName'))
        if not code.isdigit() or len(code)!=6 or not name or code in result:raise ValueError('行业代码/名称或重复异常')
        result[code]=name
    if not result:raise ValueError('空行业列表')
    return result


def parse_history(text,source,symbol):
    if source=='申万官网':
        raw=json.loads(text)['data']
        if not isinstance(raw,list):raise ValueError('官网历史data不是数组')
        rows=[{'date':str(r['bargaindate'])[:10],'close':r['closeindex'],
               'amount':r.get('bargainsum'),'volume':r.get('bargainamount')} for r in raw]
        if any(str(r.get('swindexcode',symbol))!=symbol for r in raw):raise ValueError('返回指数代码错配')
        field='bargainsum';unit='原字段单位未由响应声明；不换算，正式接入前核对'
    elif source.startswith('腾讯'):
        obj=json.loads(text)
        if obj.get('code')!=0:raise ValueError('腾讯接口报错：'+str(obj.get('msg')))
        raw=obj['data'][symbol]['day']
        # 腾讯第7字段可能是对象/换手率，不得当成交额，也不估算量×价。
        rows=[{'date':r[0],'close':r[2],'amount':None,'volume':r[5]} for r in raw]
        field=None;unit=None
    else:
        text=text.strip()
        if not text.startswith('['):raise ValueError('新浪返回非JSON日线数组')
        raw=json.loads(text)
        rows=[{'date':str(r['day'])[:10],'close':r['close'],'amount':r.get('amount'), 'volume':r.get('volume')} for r in raw]
        field='amount' if any('amount' in r for r in raw) else None;unit='原字段单位未声明' if field else None
    if not rows:raise ValueError('空日线，不算取得历史')
    seen=set();amount_ok=True
    for row in rows:
        row['date']=dt.date.fromisoformat(row['date']).isoformat()
        if row['date'] in seen:raise ValueError('日线日期重复')
        seen.add(row['date']);row['close']=number(row['close'],True)
        try:row['amount']=number(row['amount'])
        except ValueError:amount_ok=False;row['amount']=None
    rows.sort(key=lambda r:r['date'])
    return {'行数':len(rows),'首日':rows[0]['date'],'末日':rows[-1]['date'],
            '成交额原字段':field,'单位':unit,'全部日期含有效成交额':amount_ok,
            '样例':[rows[0],rows[len(rows)//2],rows[-1]],
            '规范数据SHA256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
            '_rows':rows}


def run(root=ROOT):
    root=Path(root);now=dt.datetime.now(BJ);client=FlowHTTP(time.monotonic()+840)
    calendar=TradingCalendar(root);target=now.date()
    if now.time()<dt.time(18) or not calendar.is_open(target):target=calendar.previous(target)
    obj={'任务':'T8申万一级行业日线及成交额探测；未接生产','北京时间':now.isoformat(),
         'Actions':f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
         '源提交':os.environ['GITHUB_SHA'],'要求完整行情日':target.isoformat(),'官方行业列表':{},
         '探测':[],'请求':client.trace,'正式接入':False,
         '接口依据':'https://github.com/akfamily/akshare/blob/main/akshare/index/index_research_sw.py',
         '限制':['短时重复不是跨交易日稳定性。','保留成交额原字段，不用成交量×价格估算。','主来源及是否ETF近似由审核决定，不自动启用。']}
    out=root/'logs/资金轮动探测.txt'
    def save():atomic_text(out,'资金轮动来源探测（北京时间）\n'+json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    try:
        address=SW+'current/?'+urllib.parse.urlencode({'page':1,'page_size':100,'indextype':'一级行业'})
        codes=industry_list(client.get(address));obj['官方行业列表']={'来源':address,'数量':len(codes),'行业':codes}
    except Exception as exc:
        # 只作为代码候选，不把业绩加速的14行业冒充全31行业。
        local=json.loads((root/'data/v3/申万成分.json').read_text());codes={k:'本地成分代码，官方名称未取得' for k in local}
        obj['官方行业列表']={'失败':str(exc),'候选代码来源':'data/v3/申万成分.json','候选数量':len(codes),'完整31行业覆盖未证明':True}
    save();sample=sorted(codes)[:3]
    for source in ('申万官网','腾讯sw前缀','腾讯sz前缀','新浪sw前缀'):
        viable=False
        # 每源先测3只；有完整成交额的源再遍历官方已取得的行业清单。
        todo=list(sample)
        while todo:
            code=todo.pop(0);symbol=('sz' if source=='腾讯sz前缀' else 'sw')+code
            if source=='申万官网':
                symbol=code;address=SW+'trend/?'+urllib.parse.urlencode({'swindexcode':code,'period':'DAY'})
            elif source.startswith('腾讯'):
                address='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+urllib.parse.urlencode({'param':f'{symbol},day,2013-01-01,,900,'})
            else:
                address='https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?'+urllib.parse.urlencode({'symbol':symbol,'scale':240,'ma':'no','datalen':1023})
            item={'来源':source,'code':code,'名称':codes[code],'url':address,'轮次':1,'合格价格及成交额':False};obj['探测'].append(item)
            before=len(client.trace)
            try:
                result=parse_history(client.get(address),source,symbol);rows=result.pop('_rows');item.update(result)
                item['收尾日覆盖']=any(r['date']==target.isoformat() for r in rows)
                item['2013年历史覆盖']=result['首日']<='2013-01-04'
                item['合格价格及成交额']=result['全部日期含有效成交额'] and item['收尾日覆盖']
                if item['合格价格及成交额'] and not viable:
                    viable=True;todo.extend(c for c in sorted(codes) if c not in sample)
            except Exception as exc:item['失败']=type(exc).__name__+': '+str(exc)
            item['已发起请求']=len(client.trace)>before
            save();print(f"{source} {code}：{item.get('失败',item['合格价格及成交额'])}",flush=True)
        # 三次独立重取；同源相同规范历史SHA才计短时一致。
        for first in [r for r in obj['探测'] if r['来源']==source and r['轮次']==1][:3]:
            item={'来源':source,'code':first['code'],'轮次':2,'合格价格及成交额':False};obj['探测'].append(item)
            before=len(client.trace)
            try:
                symbol=first['code'] if source=='申万官网' else ('sz' if source=='腾讯sz前缀' else 'sw')+first['code']
                result=parse_history(client.get(first['url']),source,symbol);result.pop('_rows');item.update(result)
                item['规范历史与首轮一致']=result['规范数据SHA256']==first.get('规范数据SHA256')
                item['合格价格及成交额']=result['全部日期含有效成交额'] and first.get('收尾日覆盖',False)
            except Exception as exc:item['失败']=type(exc).__name__+': '+str(exc)
            item['已发起请求']=len(client.trace)>before
            save()
    obj['汇总']={}
    for source in ('申万官网','腾讯sw前缀','腾讯sz前缀','新浪sw前缀'):
        rows=[r for r in obj['探测'] if r['来源']==source];first=[r for r in rows if r['轮次']==1]
        obj['汇总'][source]={'计划探测行业数':len(first),'实测行业数':sum(r['已发起请求'] for r in first),'价格及成交额合格数':sum(r['合格价格及成交额'] for r in first),
                            '2013覆盖数':sum(r.get('2013年历史覆盖',False) for r in first),
                            '重复合格且一致数':sum(r.get('规范历史与首轮一致',False) and r['合格价格及成交额'] for r in rows if r['轮次']==2)}
    obj['程序完成']=True;obj['结束北京时间']=dt.datetime.now(BJ).isoformat();save();return obj


if __name__=='__main__':
    if os.environ.get('GITHUB_ACTIONS')!='true':sys.exit('仅允许Actions联网探测；本机只跑离线解析测试')
    run()
