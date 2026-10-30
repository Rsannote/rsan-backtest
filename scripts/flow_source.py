"""申万来源及原始版本追加；每次HTTPS前验证当前服务器链，不禁用校验。"""
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.parse

from auto_judgment_common import BJ, atomic_text, read_csv
from probe_flow import FlowHTTP, SW, industry_list

RAW=['date','open','high','low','close','volume','amount','来源','采集时间','修订原因']
REGISTRY=['code','name','来源','采集时间','修订原因']
NEW={'801960','801970','801980'}
START='2014-02-21'
START31='2021-12-13'


def cell(value):
    if value is None:return ''
    if isinstance(value,float):
        if not math.isfinite(value):return ''
        return format(value,'.12g')
    return str(value)


def append_rows(path,fields,rows,keys,stamp):
    path=Path(path);old=path.read_bytes() if path.exists() else b''
    text=old.decode('utf-8');previous={tuple(r[k] for k in keys):r for r in csv.DictReader(io.StringIO(text))} if text else {}
    if text and next(csv.reader(io.StringIO(text)))!=fields:raise ValueError(str(path)+'表头不符，不覆盖')
    pending=[]
    for raw in rows:
        row={f:cell(raw.get(f)) for f in fields};key=tuple(row[k] for k in keys);before=previous.get(key)
        comparable=[f for f in fields if f not in ('采集时间','生成时间','修订原因')]
        if before and all(row[f]==before[f] for f in comparable):continue
        if '采集时间' in row:row['采集时间']=stamp
        if '生成时间' in row:row['生成时间']=stamp
        row['修订原因']='上游或计算结果变化，保留旧版本' if before else '首次记录'
        pending.append(row);previous[key]=row
    if not pending and old:return 0
    out=io.StringIO(newline='');writer=csv.DictWriter(out,fields,lineterminator='\n')
    if not old:writer.writeheader()
    writer.writerows(pending)
    # 解码/编码不改变已有UTF8字节，包括CRLF和人工字段；新记录只追加。
    atomic_text(path,text+('' if not text or text.endswith('\n') else '\n')+out.getvalue())
    return len(pending)


def verify_tls(root,work):
    if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('官网联网只允许Actions')
    root=Path(root);work=Path(work);work.mkdir(parents=True,exist_ok=True)
    pem=root/'system/证书/申万中间CA.pem';meta=json.loads((pem.parent/'申万中间CA_来源.json').read_text())
    if hashlib.sha256(pem.read_bytes()).hexdigest()!=meta['PEM_SHA256']:raise ValueError('中间证书文件SHA不符')
    response=subprocess.run(['openssl','s_client','-connect','www.swsresearch.com:443','-servername','www.swsresearch.com','-showcerts'],input=b'',capture_output=True,timeout=20)
    match=re.search(b'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',response.stdout,re.S)
    if not match:raise ValueError('服务器未返回叶证书')
    leaf=work/'leaf.pem';leaf.write_bytes(match[0]+b'\n')
    ca=ssl.get_default_verify_paths().cafile
    if not ca or not Path(ca).is_file():raise ValueError('系统根CA不可用')
    verify=subprocess.run(['openssl','verify','-CAfile',ca,'-untrusted',str(pem),'-purpose','sslserver','-verify_hostname','www.swsresearch.com',str(leaf)],capture_output=True,text=True,timeout=15)
    log={'北京时间':dt.datetime.now(BJ).isoformat(),'中间CA_DER_SHA256':meta['证书DER_SHA256'],'服务器叶证书PEM_SHA256':hashlib.sha256(leaf.read_bytes()).hexdigest(),
         '系统根CA':ca,'verify_exit':verify.returncode,'验证输出':verify.stdout+verify.stderr,'策略':'系统根+中间CA、sslserver用途、当前www.swsresearch.com主机名，全部必须通过'}
    atomic_text(work/'TLS验证.json',json.dumps(log,ensure_ascii=False,indent=2)+'\n')
    if verify.returncode:raise ValueError('证书链/有效期/主机名验证失败：'+verify.stdout+verify.stderr)
    bundle=work/'verified-ca.pem';bundle.write_bytes(Path(ca).read_bytes()+b'\n'+pem.read_bytes());return bundle,log


def parse_raw(obj,code):
    rows=obj.get('data')
    if not isinstance(rows,list) or not rows:raise ValueError(code+'日线为空或非数组')
    seen=set();result=[]
    for raw in rows:
        date=dt.date.fromisoformat(str(raw['bargaindate'])[:10]).isoformat()
        if str(raw.get('swindexcode'))!=code or date in seen:raise ValueError(code+'身份错配或日期重复')
        seen.add(date);item={'date':date,'来源':SW+'trend/?'+urllib.parse.urlencode({'swindexcode':code,'period':'DAY'})}
        for output,key in [('open','openindex'),('high','maxindex'),('low','minindex'),('close','closeindex'),('volume','bargainamount'),('amount','bargainsum')]:
            value=raw.get(key)
            if value is None:
                if output in ('close','amount'):raise ValueError(code+'缺收盘或成交额')
                item[output]=None;continue
            value=float(value)
            if not math.isfinite(value) or value<0 or (output=='close' and value==0):raise ValueError(code+'数值无效')
            item[output]=value
        result.append(item)
    return sorted(result,key=lambda r:r['date'])


def fetch_all(root,work,target):
    root=Path(root);work=Path(work);bundle,tls=verify_tls(root,work)
    os.environ['FLOW_CA_BUNDLE']=str(bundle);client=FlowHTTP(time.monotonic()+720)
    listing=industry_list(client.get(SW+'current/?'+urllib.parse.urlencode({'page':1,'page_size':100,'indextype':'一级行业'})))
    if len(listing)!=31 or not NEW.issubset(listing):raise ValueError('一级行业集合改变，不静默改变28/31口径')
    collected={}
    for code in sorted(listing):
        obj=json.loads(client.get(SW+'trend/?'+urllib.parse.urlencode({'swindexcode':code,'period':'DAY'})))
        collected[code]=[r for r in parse_raw(obj,code) if r['date']<=target];print('申万取数 '+code+' '+str(len(collected[code]))+'行',flush=True)
    # 所有接口先验证通过再追加；中断可幂等续跑，不覆盖历史。
    stamp=dt.datetime.now(BJ).isoformat();base=root/'data/资金轮动/原始';inserted={}
    for code,rows in collected.items():inserted[code]=append_rows(base/'申万'/f'{code}.csv',RAW,rows,['date'],stamp)
    append_rows(base/'行业清单.csv',REGISTRY,[{'code':c,'name':n,'来源':SW+'current/'} for c,n in sorted(listing.items())],['code'],stamp)
    proof={'TLS':tls,'追加行数':inserted,'请求':client.trace,'完整行业数':len(collected)}
    atomic_text(work/'抓取证据.json',json.dumps(proof,ensure_ascii=False,indent=2)+'\n');return proof


def ingest_capture(root,capture):
    """仅用于已有Actions捕获的原始响应离线复现；不会声称本次TLS已重新验证。"""
    root=Path(root);capture=Path(capture);listing=json.loads((capture/'行业列表.json').read_text())
    if len(listing)!=31 or not NEW.issubset(listing):raise ValueError('捕获行业集合无效')
    stamp=dt.datetime.now(BJ).isoformat();base=root/'data/资金轮动/原始'
    for code in sorted(listing):
        rows=parse_raw(json.loads((capture/'原始'/f'{code}.json').read_text()),code)
        append_rows(base/'申万'/f'{code}.csv',RAW,rows,['date'],stamp)
    append_rows(base/'行业清单.csv',REGISTRY,[{'code':c,'name':n,'来源':SW+'current/'} for c,n in sorted(listing.items())],['code'],stamp)
