#!/usr/bin/env python3
"""T12 一致预期来源探测；仅Actions联网，只写logs，不接入正式快照。"""
import argparse
import csv
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
BJ=dt.timezone(dt.timedelta(hours=8))
MISSING={'','--','-','—','暂无','None','null','NaN','nan'}


def numeric(value):
    text=re.sub(r'\s+','',str(value)).replace(',','')
    if text in MISSING or isinstance(value,bool):return None
    result=float(text)
    if not math.isfinite(result):raise ValueError('EPS非有限数值')
    return result


class Table(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        if tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,text):
        if self.cell is not None:self.cell.append(text)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(re.sub(r'\s+','', ''.join(self.cell)));self.cell=None
        if tag=='tr' and self.row is not None:self.rows.append(self.row);self.row=None


def parse_ths(text,year):
    if '本年度暂无机构做出业绩预测' in text:
        return {'状态':'来源明确暂无预测','eps':{},'机构数':{},'来源更新时间':None}
    match=re.search(r'(?:预测年报每股收益|每股收益预测|预测每股收益)[\s\S]{0,3000}?(<table\b[\s\S]*?</table>)',text,re.I)
    if not match:raise ValueError('未定位EPS预测表，不用净利润/历史每股收益/首个任意表格代替')
    table=Table();table.feed(match[1]);header=None;eps={};counts={};raw_rows={}
    for row in table.rows:
        if '年度' in row and any(c in row for c in ('均值','平均值')):
            header=row;continue
        if not header or len(row)<len(header):continue
        y=re.search(r'20\d{2}',row[header.index('年度')])
        if not y:continue
        y=int(y[0]);mean=header.index('均值' if '均值' in header else '平均值')
        value=numeric(row[mean])
        if value is not None:eps[y]=value
        count_col=next((header.index(c) for c in ('预测机构数','机构数') if c in header),None)
        count=numeric(row[count_col]) if count_col is not None else None
        if count is not None and (count<0 or int(count)!=count):raise ValueError('机构数不是非负整数')
        counts[y]=int(count) if count is not None else None
        raw_rows[y]=row
    if any(y not in eps for y in (year,year+1)):raise ValueError('缺本年/次年EPS均值；历史或单年预测不算完整覆盖')
    return {'状态':'两年一致预期EPS可解析','eps':{str(y):eps[y] for y in (year,year+1)},
            '机构数':{str(y):counts.get(y) for y in (year,year+1)},'字段':header,
            'EPS原表行':{str(y):raw_rows[y] for y in (year,year+1)},
            '聚合口径':'来源EPS预测表的均值，未自行平均研报','来源更新时间':None}


def parse_secondary(text,year):
    obj=json.loads(text);result=obj.get('result') or {};records=result.get('data')
    if obj.get('success') is False or not isinstance(records,list) or not records:raise ValueError('东财空返回或失败')
    actual=[]
    for row in records:
        eps={}
        for n in range(1,5):
            y=row.get('YEAR'+str(n));v=row.get('EPS'+str(n))
            if y is not None and numeric(v) is not None:eps[int(y)]=numeric(v)
        if year in eps and year+1 in eps:
            actual.append({'code':row.get('SECURITY_CODE'),'name':row.get('SECURITY_NAME_ABBR'),
                           'eps':{str(y):eps[y] for y in (year,year+1)},'评级报告计数原字段':row.get('RATING_ORG_NUM'),
                           '机构数':None,'说明':'RATING_ORG_NUM的去重机构/研报统计口径未核验，不冒充机构数'})
    return {'本页返回行数':len(records),'两年EPS可解析行数':len(actual),'接口宣称总数':result.get('count'),
            '总页数':result.get('pages'),'仅请求页数':1,'完整遍历全市场':False,
            '字段':list(records[0]),'样例':actual[:5],'允许作主来源':False,
            '说明':'仅对照探测；本页覆盖不外推全市场，年度按YEAR字段定位，不固定把EPS1当本年'}


def sample_codes(root,count):
    with (root/'data/标的清单.csv').open(encoding='utf-8-sig',newline='') as f:
        universe={r['code']:r.get('name','') for r in csv.DictReader(f) if r.get('type')=='股票'}
    codes=sorted(universe)
    if not codes:raise ValueError('股票样本范围为空')
    chosen=[]
    for prefix in ('000','002','300','600','601','603','688','920'):
        hit=next((c for c in codes if c.startswith(prefix)),None)
        if hit and hit not in chosen:chosen.append(hit)
    for i in range(count):
        hit=codes[round(i*(len(codes)-1)/max(count-1,1))]
        if hit not in chosen:chosen.append(hit)
    return [{'code':c,'name':universe[c]} for c in chosen[:count]],len(codes)


class HTTP:
    def __init__(self,deadline):self.deadline=deadline;self.trace=[]
    def get(self,address,encoding='utf-8'):
        if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('仅允许Actions联网探测')
        if time.monotonic()>self.deadline-20:raise TimeoutError('本轮预算不足，未测项保留未完成')
        time.sleep(1.3 if 'eastmoney.com' in address else .6)
        entry={'url':address,'北京时间':dt.datetime.now(BJ).isoformat()};self.trace.append(entry)
        headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
                 'Accept':'*/*','Referer':urllib.parse.urljoin(address,'/')}
        began=time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(address,headers=headers),timeout=12) as response:
                raw=response.read(2*1024*1024+1);entry['HTTP']=response.status
                if len(raw)>2*1024*1024:raise ValueError('响应超过2MiB，不解析截断内容')
                charset=response.headers.get_content_charset() or encoding
            text=raw.decode(charset,errors='replace')
            entry.update(字节数=len(raw),SHA256=hashlib.sha256(raw).hexdigest(),内容开头=re.sub(r'\s+',' ',text[:350]))
            return text
        except Exception as exc:
            if isinstance(exc,urllib.error.HTTPError):entry['HTTP']=exc.code
            entry['错误']=type(exc).__name__+': '+str(exc);raise
        finally:entry['耗时秒']=round(time.monotonic()-began,3)


def run(root=ROOT,count=8):
    root=Path(root);now=dt.datetime.now(BJ);year=now.year
    chosen,pool_count=sample_codes(root,count)
    client=HTTP(time.monotonic()+600)
    report={'任务':'T12一致预期来源探测；尚未接入','北京时间':now.isoformat(),
            'Actions':f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}",
            '源提交':os.environ.get('GITHUB_SHA'),'预测年':[year,year+1],'已批准接入':False,'程序完成':False,
            '范围说明':'仅探测；只写logs，无正式快照/行业上修表/定时接入/主源选择',
            '样本':chosen,'仓库股票范围':pool_count,'样本说明':'按已有股票清单板块前缀及等距位置选取；不是全A随机样本，不外推全市场覆盖',
            '接口依据':['https://github.com/akfamily/akshare/blob/main/akshare/stock/research/stock_profit_forecast_ths.py',
                        'https://github.com/akfamily/akshare/blob/main/akshare/stock/research/stock_profit_forecast_em.py'],
            '探测':[],'来源汇总':{},'请求':client.trace}
    output=root/'logs/一致预期探测.txt';output.parent.mkdir(parents=True,exist_ok=True)
    def save():output.write_text('一致预期EPS来源探测（北京时间）\n'+json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    save()
    for source in ('同花顺','新浪','腾讯'):
        for round_no in (1,2):
            # 第一轮测全部样本，第二轮独立重复前三只，避免把同一响应充作稳定性。
            for stock in chosen if round_no==1 else chosen[:3]:
                code=stock['code'];symbol=('sh' if code.startswith(('6','9')) else 'sz')+code
                addresses={'同花顺':f'https://basic.10jqka.com.cn/new/{code}/worth.html',
                           '新浪':f'https://vip.stock.finance.sina.com.cn/q/go.php/vIR_StockSearch/key/{code}.phtml',
                           '腾讯':f'https://gu.qq.com/{symbol}/gp'}
                item={'来源':source,'code':code,'name':stock['name'],'轮次':round_no,'两年EPS覆盖':False,'机构数字段齐全':False,'状态':'进行中'}
                report['探测'].append(item)
                before=len(client.trace)
                try:
                    text=client.get(addresses[source],'gbk' if source in ('同花顺','新浪') else 'utf-8')
                    if source=='同花顺':
                        tables=[]
                        for block in re.findall(r'<table\b[\s\S]*?</table>',text,re.I)[:3]:
                            table=Table();table.feed(block);tables.append(table.rows[:5])
                        item['候选表格样例']=tables
                        result=parse_ths(text,year);item.update(result)
                        item['两年EPS覆盖']=len(result['eps'])==2
                        item['机构数字段齐全']=item['两年EPS覆盖'] and all(v is not None and v>0 for v in result['机构数'].values())
                    else:
                        # 网页可达与来源EPS聚合语义分开；不将研报单条预测或TTM EPS当一致预期。
                        item.update(状态='页面可达，未取得可验证的两年一致预期均值',字段线索=sorted(set(re.findall(r'(?:20\d{2}[^<>]{0,35}(?:EPS|每股收益)|一致预期|盈利预测|预测机构数|TTM)',text,re.I)))[:15],
                                    限制='新浪检索可能是逐篇研报，腾讯页可能只含历史/TTM；未核实聚合与年份映射，不计成功')
                    item['本轮抓取时刻']=dt.datetime.now(BJ).isoformat()
                except Exception as exc:item.update(状态='失败或缺目标字段',原因=type(exc).__name__+': '+str(exc))
                item['已发起请求']=len(client.trace)>before
                save();print(f"{source} {code} 第{round_no}轮：{item['状态']}",flush=True)
    # 东财只试一页一次；不全量遍历，也不作为主源。
    params={'reportName':'RPT_WEB_RESPREDICT','columns':'WEB_RESPREDICT','pageNumber':1,'pageSize':500,'sortTypes':-1,'sortColumns':'RATING_ORG_NUM','p':1,'pageNo':1,'pageNum':1}
    address='https://datacenter-web.eastmoney.com/api/data/v1/get?'+urllib.parse.urlencode(params)
    try:report['东财对照']=parse_secondary(client.get(address),year)
    except Exception as exc:report['东财对照']={'状态':'失败','原因':str(exc),'允许作主来源':False}
    for source in ('同花顺','新浪','腾讯'):
        items=[i for i in report['探测'] if i['来源']==source];first=[i for i in items if i['轮次']==1]
        repeated=[i for i in items if i['轮次']==2];repeat_equal=0
        for second in repeated:
            one=next(i for i in first if i['code']==second['code'])
            if one.get('两年EPS覆盖') and second.get('两年EPS覆盖') and one.get('eps')==second.get('eps') and one.get('机构数')==second.get('机构数'):repeat_equal+=1
        report['来源汇总'][source]={'计划样本数':len(first),'实测样本数':sum(i['已发起请求'] for i in first),'两年EPS覆盖数':sum(i['两年EPS覆盖'] for i in first),
            'EPS及机构数均齐全数':sum(i['机构数字段齐全'] for i in first),'计划重复样本数':len(repeated),'重复样本数':sum(i['已发起请求'] for i in repeated),'两轮均有效且数值一致数':repeat_equal,
            '更新频率':'未核实，页面抓取时刻不是预测更新时间','跨日稳定性':'未验证；仅本轮独立重复，不证明跨日稳定',
            '全市场覆盖':'未知，未穷举；不得按样本覆盖率外推'}
    report['程序完成']=True;report['结束北京时间']=dt.datetime.now(BJ).isoformat()
    report['来源验收']='有候选可供审核，尚未批准' if any(r['两年EPS覆盖数'] for r in report['来源汇总'].values()) else '主候选尚未取得有效目标数据，不能正式接入'
    report['审核事项']=['逐一审核主来源、聚合机构口径、年度映射、更新时间与缺报行为。','东财仅本页对照，不得未经批准改为主源。','全A覆盖与五个交易日快照属于后续正式接入验收，本探测未实施。']
    save();print('探测程序已完成；'+report['来源验收'],flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--sample-count',type=int,default=8);args=parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS')!='true':parser.error('联网探测只允许Actions；本地请跑tests/test_probe_consensus.py')
    if not 3<=args.sample_count<=20:parser.error('样本数限制3–20，避免无边界抓取')
    run(count=args.sample_count)
    return 0


if __name__=='__main__':raise SystemExit(main())
