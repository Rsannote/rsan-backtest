#!/usr/bin/env python3
"""T6b 宏观数据正式接入：仅Actions联网，历史只追加，按可见时刻计算。"""
import argparse
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
from pathlib import Path
import random
import re
import time
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

import requests
from auto_judgment_common import BJ, TradingCalendar, atomic_text, read_csv
from probe_macro import china_bond, csv_series, day, number, Tables, url

ROOT = Path(__file__).resolve().parents[1]
START = dt.date(2015, 1, 1)
FIELDS = ['date', 'value', 'source', '可见日期', '可见时刻', 'note', '记录时间']
UNITS = {'中国10年国债':'%', '美国10年国债':'%', '美元兑人民币':'人民币/美元（中间价）',
         '美元兑人民币_FRED历史补充':'人民币/美元（纽约午间价）', '美元指数':'指数点',
         '布伦特原油':'美元/桶', 'COMEX黄金':'美元/盎司', 'M1':'亿元', 'M2':'亿元',
         'CPI同比':'%', 'PPI同比':'%', '制造业PMI':'指数点'}
MONTHLY = {'M1':('RPT_ECONOMY_CURRENCY_SUPPLY','CURRENCY'),
           'M2':('RPT_ECONOMY_CURRENCY_SUPPLY','BASIC_CURRENCY'),
           'CPI同比':('RPT_ECONOMY_CPI','NATIONAL_SAME'),
           'PPI同比':('RPT_ECONOMY_PPI','BASE_SAME'),
           '制造业PMI':('RPT_ECONOMY_PMI','MAKE_INDEX')}


class Client:
    """串行、会话复用、限流、大小/超时上限；不绕过来源拒绝。"""
    def __init__(self):
        self.session = requests.Session()
        self.trace, self.cache = [], {}
        self.deadline = time.monotonic()+1200

    def get(self, address):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('实际联网取数只能在GitHub Actions运行')
        if time.monotonic() > self.deadline:
            raise TimeoutError('宏观取数总预算已用尽')
        time.sleep(1.1+random.random()*.3 if 'eastmoney.com' in address else .2)
        entry = {'url':address, '北京时间':dt.datetime.now(BJ).isoformat()}
        self.trace.append(entry)
        headers = {'User-Agent':'Mozilla/5.0 (compatible; RsanMacro/1.0)', 'Referer':address.split('/')[0]+'//'+address.split('/')[2]+'/'}
        try:
            if 'fred.stlouisfed.org' in address:
                # 沿用已探测通过的标准库传输；FRED对Session请求本轮实测超时。
                for attempt in range(2):
                    try:
                        req=urllib.request.Request(address,headers={'User-Agent':'Mozilla/5.0 (compatible; RsanMacroProbe/1.0)','Accept':'*/*'})
                        with urllib.request.urlopen(req,timeout=30) as response:
                            raw=response.read(8*1024*1024+1);entry['HTTP']=response.status
                        if len(raw)>8*1024*1024: raise ValueError('FRED响应超过8MiB')
                        entry.update(字节数=len(raw),SHA256=hashlib.sha256(raw).hexdigest(),尝试次数=attempt+1)
                        return raw.decode('utf-8-sig')
                    except (OSError,urllib.error.URLError):
                        if attempt: raise
                        time.sleep(2)
            with self.session.get(address, headers=headers, timeout=(10,30), stream=True) as response:
                entry['HTTP'] = response.status_code
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw)>8*1024*1024:
                        raise ValueError('响应超过8MiB，不使用截断数据')
                entry.update(字节数=len(raw), SHA256=hashlib.sha256(raw).hexdigest())
                return raw.decode('utf-8-sig')
        except Exception as exc:
            entry['错误'] = str(exc)
            raise

    def em(self, report, date_key='REPORT_DATE'):
        key = (report,date_key)
        if key in self.cache:
            return self.cache[key]
        result = []
        for page in range(1, 31):
            address = url('https://datacenter-web.eastmoney.com/api/data/v1/get', reportName=report,
                          columns='ALL', pageSize=500, pageNumber=page, sortColumns=date_key,
                          sortTypes='-1', source='WEB', client='WEB')
            obj = json.loads(self.get(address))
            batch = (obj.get('result') or {}).get('data')
            pages = int((obj.get('result') or {}).get('pages') or 0)
            if obj.get('success') is False or not batch or pages<1:
                raise ValueError(f'{report}空返回/接口失败，不写入')
            for row in batch:
                result.append((row,address))
            if min(day(r[date_key]) for r in batch) <= START.isoformat() or page>=pages:
                break
        else:
            raise ValueError('分页超出上限，不能把截断历史算成功')
        self.cache[key] = result
        return result


def next_open(calendar, value):
    current = dt.date.fromisoformat(value) if isinstance(value,str) else value
    for _ in range(370):
        current += dt.timedelta(days=1)
        if calendar.is_open(current):
            return current
    raise ValueError('未找到可确认的下一A股交易日')


def visible_at(name, period, calendar):
    if name in MONTHLY:
        y,m = map(int,period[:7].split('-'))
        month = dt.date(y+1,1,1) if m==12 else dt.date(y,m+1,1)
        return dt.datetime.combine(month.replace(day=1 if name=='制造业PMI' else 15),dt.time(23,59,59),BJ)
    d = dt.date.fromisoformat(period)
    if name=='中国10年国债':
        return dt.datetime.combine(d,dt.time(18),BJ)
    if name=='美元兑人民币':
        return dt.datetime.combine(d,dt.time(9,15),BJ)
    if name=='美元兑人民币_FRED历史补充':
        d += dt.timedelta(days=7)
    return dt.datetime.combine(next_open(calendar,d),dt.time(9,30),BJ)


def validated(points, monthly=False):
    values = {}
    today = dt.datetime.now(BJ).date().isoformat()
    for raw_day, raw_value, source in points:
        period = day(raw_day)[:7] if monthly else day(raw_day)
        value = number(raw_value)
        if period>today or period<'2015-01':
            if period>today: raise ValueError('来源含未来统计期/观测日')
            continue
        if not source.startswith('https://'):
            raise ValueError('来源须为实际HTTPS网址')
        if period in values and values[period][0]!=value:
            raise ValueError('同次抓取同日冲突')
        values[period]=(value,source)
    if not values:
        raise ValueError('没有有效历史数据')
    return [(p,*values[p]) for p in sorted(values)]


def em_points(client, report, field, date_key='REPORT_DATE', monthly=False):
    return validated([(r[date_key],r[field],source) for r,source in client.em(report,date_key) if r.get(field) is not None],monthly)


def fetch_cn(client, root, now, full=False):
    old = root/'data/宏观/中国10年国债.csv'
    first = START
    if old.exists() and not full:
        first = max(first,dt.date.fromisoformat(max(r['date'] for r in read_csv(old)))-dt.timedelta(days=40))
    result = []
    cutoff=now.date() if now.timetz().replace(tzinfo=None)>=dt.time(18) else now.date()-dt.timedelta(days=1)
    while first<=cutoff:
        # 沿用探测已通过的自然年窗口，避免360日切片刚好留下未公布的当日空尾段。
        end = min(dt.date(first.year,12,31),cutoff)
        address = url('https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery',startDate=first.isoformat(),endDate=end.isoformat(),gjqx='0',qxId='ycqx',locale='cn_ZH')
        values = china_bond(client.get(address))
        if any(not first.isoformat()<=d<=end.isoformat() for d in values):
            raise ValueError('中债返回日期超出请求窗口')
        result.extend((d,v,address) for d,v in values.items())
        first = end+dt.timedelta(days=1)
    return validated(result)


def fetch_fred(client, code):
    address = url('https://fred.stlouisfed.org/graph/fredgraph.csv',id=code,cosd=START.isoformat())
    text = client.get(address)
    header = next(csv.reader(io.StringIO(text)))
    values = csv_series(text,code,'observation_date' if 'observation_date' in header else 'DATE')
    return validated([(d,v,address) for d,v in values.items()])


def cfets(text, source):
    obj = json.loads(text)
    if str(obj['head']['rep_code'])!='200':
        raise ValueError('外汇交易中心返回失败')
    records = [r for r in obj['records'] if r.get('vrtEName')=='USD/CNY' and r.get('vrtName')=='美元/人民币']
    if len(records)!=1:
        raise ValueError('缺唯一USD/CNY中间价，不用其他币种代替')
    return validated([(obj['data']['lastDate'][:10],records[0]['price'],source)])


def fetch_yahoo(client, symbol, now):
    address = url('https://query1.finance.yahoo.com/v8/finance/chart/'+requests.utils.quote(symbol,safe=''),period1=1420070400,period2=int(now.timestamp()),interval='1d')
    obj = json.loads(client.get(address))['chart']['result'][0]
    if obj['meta']['symbol']!=symbol:
        raise ValueError('Yahoo标的身份不符')
    tz = ZoneInfo(obj['meta']['exchangeTimezoneName'])
    regular_end = ((obj['meta'].get('currentTradingPeriod') or {}).get('regular') or {}).get('end')
    result = []
    for stamp,value in zip(obj['timestamp'],obj['indicators']['quote'][0]['close']):
        d = dt.datetime.fromtimestamp(stamp,tz).date()
        end = dt.datetime.combine(d,dt.time(18),tz)
        if regular_end and dt.datetime.fromtimestamp(regular_end,tz).date()==d:
            end = dt.datetime.fromtimestamp(regular_end,tz)
        if value is not None and end<=now:
            result.append((d.isoformat(),value,address))
    return validated(result)


def note_for(name):
    note = '历史初次建库可含修订；非历史逐日版本'
    if name in MONTHLY:
        note += '；可见日期=规则推定，真实发布日期未取得'
    if name=='M1':
        note += '；2025起统计口径调整，跨口径不可比'
    if name in ('美元指数','布伦特原油','COMEX黄金'):
        note += '；二级；连续合约，换月有跳空' if name!='美元指数' else '；二级；美元指数，非期货连续合约'
    if name=='美元兑人民币_FRED历史补充':
        note += '；历史补充，纽约午间价不是中间价；可见日按滞后7日保守推定，不作实时替代'
    return note


def prepare(path, name, points, calendar, now):
    """在内存中准备整份追加内容；校验失败不改既有文件。"""
    old = path.read_bytes().decode('utf-8') if path.exists() else ''
    existing = list(csv.DictReader(io.StringIO(old))) if old else []
    if old and next(csv.reader(io.StringIO(old)))!=FIELDS:
        raise ValueError('已有宏观CSV表头不符，不覆盖')
    latest = {r['date']:r for r in existing}
    added = []
    for period,value,source in points:
        previous = latest.get(period)
        if previous and number(previous['value'])==value:
            continue
        visible = visible_at(name,period,calendar)
        note = note_for(name)
        if previous:
            visible = max(visible,now,dt.datetime.fromisoformat(previous['可见时刻']))
            note += '；来源修订，追加记录，不修改原行'
        # 未来可见但已取得的观测可以存储；latest/asof必须按可见时刻过滤。
        row = dict(zip(FIELDS,[period,repr(value),source,visible.date().isoformat(),visible.isoformat(),note,now.isoformat()]))
        added.append(row)
    buf = io.StringIO(newline=''); writer = csv.DictWriter(buf,fieldnames=FIELDS,lineterminator='\n')
    if not old: writer.writeheader()
    writer.writerows(added)
    content = old+('' if not old or old.endswith('\n') or not added else '\n')+buf.getvalue()
    return content,len(added)


def asof(rows, moment):
    """先按可见时刻筛选修订版本，再按观测期选最新；绝不前视。"""
    selected = {}
    for row in rows:
        if dt.datetime.fromisoformat(row['可见时刻'])<=moment:
            selected[row['date']] = row
    return [selected[d] for d in sorted(selected)]


def derived(series, calendar, now):
    # 指标供A股主线环境使用，窗口统一为20个A股交易日。
    dates = []
    current = START
    while current<=now.date():
        if calendar.is_open(current) and dt.datetime.combine(current,dt.time(18),BJ)<=now:
            dates.append(current)
        current += dt.timedelta(days=1)
    result = []
    for name in ('中国10年国债','美国10年国债'):
        rows = series.get(name,[])
        # 按可见时刻排序，修订在实际可见后才生效。
        events = sorted(rows,key=lambda r:(r['可见时刻'],r['记录时间']))
        index, current_rows, aligned = 0,{},[]
        for d in dates:
            moment = dt.datetime.combine(d,dt.time(18),BJ)
            while index<len(events) and dt.datetime.fromisoformat(events[index]['可见时刻'])<=moment:
                current_rows[events[index]['date']]=events[index]; index+=1
            aligned.append(current_rows[max(current_rows)] if current_rows else None)
        for i in range(20,len(dates)):
            first,last=aligned[i-20],aligned[i]
            if first is None or last is None: continue
            value = (number(last['value'])-number(first['value']))*100
            result.append({'date':dates[i].isoformat(),'指标':name,'value':format(value,'.10g'),'unit':'bp',
                           '起点A股日':dates[i-20].isoformat(),'起点观测日':first['date'],'终点观测日':last['date'],
                           'source':last['source'],'可见日期':dates[i].isoformat(),'可见时刻':dt.datetime.combine(dates[i],dt.time(18),BJ).isoformat(),
                           'note':'20个A股交易日18:00已可见值之差×100；向后asof，真实观测日期保留'})
    return result


def plain(text):
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', text, flags=re.S|re.I)
    return re.sub(r'[\s\u3000\xa0]+','',html.unescape(re.sub('<[^>]+>','',text)))


def links(text, base):
    return list(dict.fromkeys((requests.compat.urljoin(base,href),plain(title))
                for href,title in re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</a>',text,re.S|re.I)))


def h15(text):
    table = Tables(); table.feed(html.unescape(text))
    dates = []
    for row in table.rows:
        parsed = []
        for cell in row:
            cleaned = re.sub(r'\s+',' ',cell).strip()
            match = re.search(r'(20\d\d)\s+([A-Za-z]{3})\s+(\d{1,2})',cleaned)
            if match:
                parsed.append(dt.datetime.strptime(' '.join(match.groups()),'%Y %b %d').date().isoformat())
        if len(parsed)>=5: dates=parsed
        if row and row[0].strip()=='10-year' and dates:
            values = [number(v) for v in row[1:]]
            if len(values)!=len(dates): raise ValueError('H15日期与名义10年列不匹配')
            return dict(zip(dates,values))  # 第一个10-year为名义期限，不取后面的TIPS。
    raise ValueError('H15官方表缺名义10年或五个日期')


def official_month(text, name):
    cleaned = plain(text)
    if name in ('M1','M2'):
        match = re.search(r'[（(]'+name+r'[）)]余额([\d.]+)(万亿元|亿元)',cleaned)
        if not match: raise ValueError(f'人行原文缺{name}余额')
        scale = 10000 if match[2]=='万亿元' else 1
        quantum = scale*10**(-len(match[1].split('.')[1]) if '.' in match[1] else 0)
        return float(match[1])*scale,quantum/2,match[0]
    if name=='制造业PMI':
        match = re.search(r'(?<!非)制造业采购经理指数[（(]PMI[）)]为([\d.]+)%?',cleaned)
        if not match: raise ValueError('统计局原文缺制造业PMI')
        return float(match[1]),1e-8,match[0]
    label = '居民消费价格' if name=='CPI同比' else '工业生产者出厂价格'
    match = re.search(label+r'同比(上涨|下降|持平)([\d.]+)?%?',cleaned)
    if not match: raise ValueError(f'统计局原文缺{name}全国同比')
    value = (float(match[2]) if match[2] else 0)*(-1 if match[1]=='下降' else 1)
    return value,1e-8,match[0]


def period_from_title(title):
    match = re.search(r'(20\d\d)年(\d{1,2})月',title)
    if match: return f'{match[1]}-{int(match[2]):02d}'
    match = re.search(r'(20\d\d)年(前三季度|上半年|一季度|金融统计)',title)
    if match:
        month = {'前三季度':9,'上半年':6,'一季度':3,'金融统计':12}[match[2]]
        return f'{match[1]}-{month:02d}'
    return None


def verify_sources(client, candidates):
    checks = []
    def add(name,period,value,reference,source,tolerance=1e-8,note=''):
        checks.append({'指标':name,'日期/统计期':period,'主值':value,'官方值':reference,'单位':UNITS[name],
                       '差值':round(value-reference,8),'通过':abs(value-reference)<=tolerance+1e-8,
                       '官方原文':source,'核对说明':note})
    cn = candidates['中国10年国债']
    for index in sorted({0,len(cn)//4,len(cn)//2,3*len(cn)//4,len(cn)-1}):
        d,v,_ = cn[index]
        source = url('https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery',startDate=d,endDate=d,gjqx=0,qxId='ycqx',locale='cn_ZH')
        reference = china_bond(client.get(source))[d]
        add('中国10年国债',d,v,reference,source,note='重新请求中债官方单日10年列；同源重采，不冒称独立来源')
    source = 'https://www.federalreserve.gov/releases/h15/'
    reference = h15(client.get(source)); us = {d:v for d,v,_ in candidates['美国10年国债']}
    dates = sorted(reference.keys() & us.keys())[-5:]
    if len(dates)!=5: raise ValueError('FRED与H15共同日期不足5个')
    for d in dates: add('美国10年国债',d,us[d],reference[d],source,note='H15名义恒定期限10年，原始来源美国财政部；不是TIPS')
    index_url = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'
    bank_links = {period_from_title(title):address for address,title in links(client.get(index_url),index_url) if '金融统计数据报告' in title}
    nbs_links = {}
    needed = {(name,d) for name in ('CPI同比','PPI同比','制造业PMI') for d,_,_ in candidates[name][-3:]}
    for page in range(8):
        address = 'https://www.stats.gov.cn/sj/zxfb/'+(f'index_{page}.html' if page else '')
        for link,title in links(client.get(address),address):
            for name,key in [('CPI同比','居民消费价格'),('PPI同比','工业生产者出厂价格'),('制造业PMI','采购经理指数运行情况')]:
                if key in title and period_from_title(title): nbs_links[(name,period_from_title(title))]=link
        if needed<=nbs_links.keys(): break
    fetched = {}
    for name in MONTHLY:
        for period,value,_ in candidates[name][-3:]:
            source = bank_links.get(period) if name in ('M1','M2') else nbs_links.get((name,period))
            if not source: raise ValueError(f'未发现{name}/{period}官方原文，不能伪称核对通过')
            if source not in fetched: fetched[source]=client.get(source)
            expected,tolerance,excerpt = official_month(fetched[source],name)
            add(name,period,value,expected,source,tolerance,note=excerpt+('；按原文四舍五入精度核对（不是逐亿元一致）' if name in ('M1','M2') else ''))
    return checks


def append_derived(path, rows, now):
    if not rows: raise ValueError('中美国债没有可用20日派生窗口')
    fields = list(rows[0])
    old = path.read_bytes().decode('utf-8') if path.exists() else ''
    existing = list(csv.DictReader(io.StringIO(old))) if old else []
    latest = {(r['date'],r['指标']):r for r in existing}
    additions=[]
    for row in rows:
        prev = latest.get((row['date'],row['指标']))
        if prev and all(prev[k]==row[k] for k in ('value','起点观测日','终点观测日')): continue
        if prev:
            row = dict(row,可见时刻=max(now.isoformat(),prev['可见时刻']),可见日期=now.date().isoformat(),note=row['note']+'；追加修订')
        additions.append(row)
    stream=io.StringIO(newline=''); writer=csv.DictWriter(stream,fields,lineterminator='\n')
    if not old: writer.writeheader()
    writer.writerows(additions)
    atomic_text(path,old+stream.getvalue())
    return len(additions)


def run(root=ROOT, verify=False, full=False, client=None, now=None):
    root=Path(root).resolve(); output=root/'data/宏观'; output.mkdir(parents=True,exist_ok=True)
    now=now or dt.datetime.now(BJ); calendar=TradingCalendar(root); client=client or Client()
    report={'title':'宏观数据更新','generated_at':now.isoformat(),'data_date':now.date().isoformat(),
            'status':'正常','errors':[],'warnings':[],'summary':[],'sources':{},'审核依据':'PR #12 Claude 2026-10-30'}
    candidates={}
    def capture(name,fetch):
        print('取数开始：'+name,flush=True)
        try:
            result=fetch()
            if name!='美元兑人民币' and (full or not (output/(name+'.csv')).exists()) and result[0][0]>'2015-01-31':
                raise ValueError('首次建库未覆盖2015年1月，不能把截断历史算成功')
            candidates[name]=result
            report['sources'][name]={'状态':'取得数据','条数':len(result),'起始':result[0][0],'最新观测':result[-1][0]}
            print(f'取数完成：{name}，{len(result)}条，{result[0][0]}—{result[-1][0]}',flush=True)
        except Exception as exc:
            report['errors'].append(name+'：'+str(exc));report['sources'][name]={'状态':'失败','原因':str(exc)}
            print('取数失败：'+name+'：'+str(exc),flush=True)
    capture('中国10年国债',lambda:fetch_cn(client,root,now,full))
    capture('美国10年国债',lambda:fetch_fred(client,'DGS10'))
    address='https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json'
    capture('美元兑人民币',lambda:cfets(client.get(address),address))
    # 与中间价分文件。历史补充只首次建库时取，后续不把滞后一周的序列当实时汇率。
    if full or not (output/'美元兑人民币_FRED历史补充.csv').exists():
        capture('美元兑人民币_FRED历史补充',lambda:fetch_fred(client,'DEXCHUS'))
    for name,symbol in [('美元指数','DX-Y.NYB'),('布伦特原油','BZ=F'),('COMEX黄金','GC=F')]:
        capture(name,lambda s=symbol:fetch_yahoo(client,s,now))
    for name,(data,field) in MONTHLY.items():
        capture(name,lambda r=data,f=field:em_points(client,r,f,monthly=True))
    # 每日中债/东财同日对照；保留主源，超过审核阈值只记警告，不自动择源。
    for name,field in [('中国10年国债','EMM00166466'),('美国10年国债','EMG00001310')]:
        try:
            secondary=dict((d,v) for d,v,_ in em_points(client,'RPTA_WEB_TREASURYYIELD',field,'SOLAR_DATE'))
            common=[(d,v) for d,v,_ in candidates.get(name,[]) if d in secondary]
            if not common: raise ValueError('主源/东财没有同日观测')
            d,v=common[-1]; difference=(v-secondary[d])*100
            report['sources'][name]['东财对照']={'日期':d,'主源':v,'东财':secondary[d],'差值bp':round(difference,8)}
            if name=='中国10年国债' and abs(difference)>.5+1e-9:
                report['warnings'].append(f'{name} {d}与东财差{difference:.4f}bp，超过0.5bp；保留中债主源')
        except Exception as exc: report['errors'].append(name+'对照失败：'+str(exc))
    checks=[]
    if verify:
        try:
            checks=verify_sources(client,candidates)
            report['errors'].extend(str(c) for c in checks if not c['通过'])
        except Exception as exc: report['errors'].append('官方抽查失败：'+str(exc))
    # 抽查失败时整批不晋升；正常增量中其他来源失败也不覆盖已存有效文件。
    staged={}
    if not (verify and report['errors']):
        for name,points in candidates.items():
            try:
                content,added=prepare(output/(name+'.csv'),name,points,calendar,now)
                staged[name]=content;report['sources'][name]['追加条数']=added
            except Exception as exc: report['errors'].append(name+'准备失败：'+str(exc))
        for name,content in staged.items(): atomic_text(output/(name+'.csv'),content)
    stored={name:read_csv(output/(name+'.csv')) for name in UNITS if (output/(name+'.csv')).exists()}
    if '中国10年国债' in stored and '美国10年国债' in stored and not (verify and report['errors']):
        try: report['派生追加条数']=append_derived(output/'派生.csv',derived(stored,calendar,now),now)
        except Exception as exc: report['errors'].append('派生计算失败：'+str(exc))
    report['status']='失败' if report['errors'] else '正常'
    lines=['# 宏观数据最新值','',f'北京时间：{now.isoformat()}；运行状态：{report["status"]}；只列此刻已可见观测。','',
           '|指标|最新观测期|最新值|上期值|变化（原单位）|单位|可见日期/时刻|','|---|---|---:|---:|---:|---|---|']
    for name in UNITS:
        visible=asof(stored.get(name,[]),now)
        if not visible:
            lines.append(f'|{name}|暂无已可见数据||||{UNITS[name]}||');continue
        last=visible[-1];prev=visible[-2] if len(visible)>1 else None
        delta=number(last['value'])-number(prev['value']) if prev else None
        lines.append(f"|{name}|{last['date']}|{number(last['value']):.8g}|{prev['value'] if prev else '暂无'}|{format(delta,'.8g') if delta is not None else '暂无'}|{UNITS[name]}|{last['可见时刻']}|")
    lines+=['','真实发布日期未知；月度可见日期为规则推定。历史初次建库可含修订。FRED纽约午间汇率不与中间价拼接。来源/单位/派生窗口详见说明.md与system/宏观数据格式.md。','',
            '## 失败',*['- '+s for s in report['errors']],'','## 警告',*['- '+s for s in report['warnings']],'']
    atomic_text(output/'最新.md','\n'.join(lines))
    atomic_text(root/'logs/宏观_最新.json',json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    atomic_text(root/'logs/宏观_最新.md','\n'.join(lines))
    atomic_text(root/'logs/宏观请求_最新.json',json.dumps(client.trace,ensure_ascii=False,indent=2)+'\n')
    if verify:
        directory=root/'logs/test/T6b';directory.mkdir(parents=True,exist_ok=True)
        atomic_text(directory/'抽查核对.json',json.dumps(checks,ensure_ascii=False,indent=2)+'\n')
        table=['# 宏观官方原文抽查','', '|指标|日期/统计期|存储值|官方值|单位|差值|通过|官方原文/精度|','|---|---|---:|---:|---|---:|---|---|']
        for c in checks:
            table.append(f"|{c['指标']}|{c['日期/统计期']}|{c['主值']:.10g}|{c['官方值']:.10g}|{c['单位']}|{c['差值']:.8g}|{'是' if c['通过'] else '否'}|[原文]({c['官方原文']})；{c['核对说明']}|")
        atomic_text(directory/'抽查核对.md','\n'.join(table)+'\n')
    print(f"宏观更新：{report['status']}；来源{len(candidates)}项，错误{len(report['errors'])}项，警告{len(report['warnings'])}项；官方核对{len(checks)}行",flush=True)
    for error in report['errors']: print(error,flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--verify',action='store_true',help='执行官方原文抽查，失败不晋升数据')
    parser.add_argument('--full',action='store_true',help='重新请求2015以来全历史；已有行仍只追加修订')
    args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true': parser.error('联网只能在Actions，本地请跑离线单元测试')
    report=run(args.root,args.verify,args.full)
    return 1 if report['errors'] else 0


if __name__=='__main__':
    raise SystemExit(main())
