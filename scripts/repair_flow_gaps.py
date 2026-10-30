"""PR21指定缺口重抓：只在Actions取真实源，只追加缺失日期，不估算成交额。"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import hashlib
import requests

from auto_judgment_common import BJ, atomic_text
from flow_source import NEW, START, START31, RAW, append_rows, parse_raw, verify_tls
from flow_metrics import read_inputs
from probe_flow import FlowHTTP, SW

ROOT=Path(__file__).resolve().parents[1]


def find_gaps(C,A,names):
    result={}
    for day in A.index[A.index>=START]:
        codes=sorted(set(names)-(NEW if day<START31 else set()))
        absent=[c for c in codes if C.loc[day,c]!=C.loc[day,c] or A.loc[day,c]!=A.loc[day,c]]
        if absent:result[day]=absent
    return result


def run(root=ROOT):
    if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('缺口重抓仅允许Actions联网')
    root=Path(root);out=root/'logs/test/T8b/复审/缺口重抓';out.mkdir(parents=True,exist_ok=True)
    latest=json.loads((root/'logs/资金轮动_最新.json').read_text());target=latest['data_date']
    B,names,C,A=read_inputs(root,target);gaps=find_gaps(C,A,names)
    bundle,tls=verify_tls(root,out);os.environ['FLOW_CA_BUNDLE']=str(bundle)
    client=FlowHTTP(time.monotonic()+900);found={};errors=[]
    for code in sorted(set(c for values in gaps.values() for c in values)):
        address=SW+'trend/?'+urllib.parse.urlencode({'swindexcode':code,'period':'DAY'})
        try:
            rows=parse_raw(json.loads(client.get(address)),code)
            found[code]={r['date']:r for r in rows if r['date'] in gaps and code in gaps[r['date']]}
        except Exception as exc:errors.append({'code':code,'错误':str(exc)})
    # 按日接口独立复查19个日期；无直接bargainsum时绝不拿成交量或比例估金额。
    daily={}
    for day in gaps:
        address=SW.replace('index_publish/','index_analysis/')+'index_analysis_report/?'+urllib.parse.urlencode({'page':1,'page_size':100,'index_type':'一级行业','start_date':day,'end_date':day,'type':'DAY','swindexcode':'all'})
        try:
            time.sleep(.6)
            response=requests.get(address,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.swsresearch.com/'},verify=str(bundle),timeout=15)
            trace={'url':address,'HTTP':response.status_code,'HTTPS证书验证':True,'SHA256':hashlib.sha256(response.content).hexdigest()};client.trace.append(trace)
            response.raise_for_status()
            if len(response.content)>2*1024*1024:raise ValueError('按日响应超过2MiB')
            obj=response.json();data=obj['data'];rows=data['results']
            if len(rows)!=int(data['count']):raise ValueError('按日响应分页不完整')
            atomic_text(out/(day+'.json'),json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
            exact=[r for r in rows if str(r.get('bargaindate',''))[:10]==day]
            daily[day]={'按日原始行数':len(exact),'含直接成交额行数':sum(r.get('bargainsum') is not None for r in exact)}
            for row in exact:
                code=str(row.get('swindexcode',''))
                if code in gaps[day] and day not in found.get(code,{}) and row.get('bargainsum') is not None:
                    parsed=parse_raw({'data':[row]},code)[0];found.setdefault(code,{})[day]=parsed
        except Exception as exc:daily[day]={'错误':str(exc)}
    stamp=dt.datetime.now(BJ).isoformat();added=0;summary=[]
    for day,codes in gaps.items():
        recovered=[]
        for code in codes:
            row=found.get(code,{}).get(day)
            if row is not None:
                added+=append_rows(root/'data/资金轮动/原始/申万'/f'{code}.csv',RAW,[row],['date'],stamp,revision_reason='PR21审核：缺口日期从申万官网重新取得，追加修订，不改旧字节')
                recovered.append(code)
        summary.append({'date':day,'原缺代码':codes,'补回代码':recovered,'仍缺代码':[c for c in codes if c not in recovered],**daily.get(day,{})})
    report={'北京时间':stamp,'Actions运行':os.environ.get('GITHUB_RUN_ID'),'数据截止':target,'缺口日期数':len(gaps),'缺口行业日数':sum(map(len,gaps.values())),'补回行数':added,'日期':summary,'请求':client.trace,'TLS':tls,'请求错误':errors,'结论口径':'仅说明本次官网历史/按日接口响应；接口缺失不等同交易所当天没有行情。没有直接成交额时保持缺失，不推算。'}
    atomic_text(out/'结果.json',json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('日期','请求','TLS')},ensure_ascii=False))
    return report


if __name__=='__main__':run()
