"""一致预期共享字段与来源客户端。年度同时认YEAR与YEAR_MARK=E。"""
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.parse

from probe_consensus import HTTP, Table, numeric, parse_ths

BJ=dt.timezone(dt.timedelta(hours=8))
REVIEW='https://github.com/Rsannote/rsan-backtest/pull/17#issuecomment-6513023799'
RULE={'随机样本数':50,'随机种子':6513023799,'两年均通过的比例下限':.90,'EPS相对差上限':.02,
      '年度映射须全部一致':True,'相对差分母':'abs(同花顺EPS)，零对零为0；同花顺为0而东财非0为不通过',
      '缺报或请求失败':'留在50只分母，不重抽；年度未验证不算一致','审核依据':REVIEW}
RULE_SHA=hashlib.sha256(json.dumps(RULE,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def em_record(row,year):
    code=str(row['SECURITY_CODE']);name=row['SECURITY_NAME_ABBR']
    if not re.fullmatch(r'\d{6}',code) or not name:raise ValueError('东财代码/名称无效')
    mapped={};raw={}
    for i in range(1,5):
        y=row.get('YEAR'+str(i));mark=row.get('YEAR_MARK'+str(i));value=row.get('EPS'+str(i))
        raw[str(i)]={'YEAR':y,'YEAR_MARK':mark,'EPS':value}
        if y is None:continue
        if int(y)!=float(y) or int(y) in mapped:raise ValueError(code+'年度无效或重复')
        mapped[int(y)]={'mark':mark,'value':numeric(value),'字段':i}
    forecasts={str(y):mapped[y]['value'] if y in mapped and mapped[y]['mark']=='E' else None for y in (year,year+1)}
    aligned=all(y in mapped and mapped[y]['mark']=='E' for y in (year,year+1))
    count=numeric(row.get('RATING_ORG_NUM'))
    if count is not None and (count<0 or int(count)!=count):raise ValueError('东财计数不是非负整数')
    return {'code':code,'name':name,'eps':forecasts,'年度映射有效':aligned,'年度原字段':raw,
            '机构数_东财':int(count) if count is not None else None,'东财计数口径':'RATING_ORG_NUM，评级统计口径，未确认为去重机构数'}


def fetch_em(client,year):
    rows={};total=None;pages=None;page=1
    while True:
        params={'reportName':'RPT_WEB_RESPREDICT','columns':'WEB_RESPREDICT','pageNumber':page,'pageSize':500,
                'sortTypes':1,'sortColumns':'SECURITY_CODE'}
        address='https://datacenter-web.eastmoney.com/api/data/v1/get?'+urllib.parse.urlencode(params)
        obj=json.loads(client.get(address));result=obj.get('result') or {}
        if obj.get('success') is not True or not isinstance(result.get('data'),list):raise ValueError('东财批量失败/非数组')
        count=int(result['count']);npage=int(result['pages'])
        if not 50<=count<=20000 or not 1<=npage<=100 or npage!=(count+499)//500:raise ValueError('东财全量分页计数异常')
        if total is None:total,pages=count,npage
        if (count,npage)!=(total,pages):raise ValueError('抓取间分页总数漂移，不提交不完整快照')
        expected=500 if page<pages else total-500*(pages-1)
        if len(result['data'])!=expected:raise ValueError('分页行数不全')
        for raw in result['data']:
            row=em_record(raw,year)
            if row['code'] in rows:raise ValueError('分页出现重复代码，不静默去重')
            rows[row['code']]=row
        if page==pages:break
        page+=1
    if len(rows)!=total:raise ValueError('全量计数不一致')
    return {'year':year,'总数':total,'页数':pages,'rows':[rows[k] for k in sorted(rows)],'抓取时间':dt.datetime.now(BJ).isoformat()}


def parse_ths_partial(text,year):
    """保留明确年度表里的单年/缺失预测；格式错误仍失败，不猜无覆盖。"""
    try:return parse_ths(text,year)
    except ValueError as exc:
        if not str(exc).startswith('缺本年/次年EPS'):raise
    match=re.search(r'(?:预测年报每股收益|每股收益预测|预测每股收益)[\s\S]{0,3000}?(<table\b[\s\S]*?</table>)',text,re.I)
    if not match:raise ValueError('未找到EPS表')
    table=Table();table.feed(match[1]);header=None;eps={};counts={};raw={}
    for row in table.rows:
        if '年度' in row and any(c in row for c in ('均值','平均值')):header=row;continue
        if not header or len(row)<len(header):continue
        y=re.search(r'20\d{2}',row[header.index('年度')])
        if not y or int(y[0]) not in (year,year+1):continue
        y=y[0]
        if y in eps:raise ValueError('EPS预测年度重复')
        eps[y]=numeric(row[header.index('均值' if '均值' in header else '平均值')]);raw[y]=row
        col=next((header.index(c) for c in ('预测机构数','机构数') if c in header),None)
        value=numeric(row[col]) if col is not None else None
        if value is not None and (value<0 or int(value)!=value):raise ValueError('机构数无效')
        counts[y]=int(value) if value is not None else None
    return {'状态':'EPS表有效，部分年度缺预测','eps':eps,'机构数':counts,'字段':header,'EPS原表行':raw,'来源更新时间':None}


def fetch_ths(client,code,year,allow_partial=False):
    address=f'https://basic.10jqka.com.cn/new/{code}/worth.html'
    text=client.get(address,'gbk')
    title=re.search(r'<title>(.*?)</title>',text,re.S|re.I)
    if not title or code not in title[1]:raise ValueError('同花顺页面证券身份无法核验')
    result=parse_ths_partial(text,year) if allow_partial else parse_ths(text,year)
    result.update(来源=address,抓取时间=dt.datetime.now(BJ).isoformat())
    return result


def relative_difference(em,ths):
    if em is None or ths is None:return None
    if ths==0:return 0.0 if em==0 else None
    return abs(em-ths)/abs(ths)


def compare_row(em,ths,year):
    diffs={str(y):relative_difference(em['eps'].get(str(y)),ths.get('eps',{}).get(str(y))) for y in (year,year+1)}
    aligned=em['年度映射有效'] and all(str(y) in ths.get('eps',{}) for y in (year,year+1))
    counts={str(y):em['机构数_东财'] is not None and em['机构数_东财']==ths.get('机构数',{}).get(str(y)) for y in (year,year+1)}
    return {'code':em['code'],'name':em['name'],'东财':em,'同花顺':ths,'相对差':diffs,
            '年度映射一致':aligned,'两年EPS均通过':aligned and all(v is not None and v<=RULE['EPS相对差上限']+1e-12 for v in diffs.values()),'机构数逐年相同':counts}


def decide(rows):
    if len(rows)!=50 or len({r['code'] for r in rows})!=50:raise ValueError('必须是固定50只、不重复、不删失败项')
    passed=sum(r['两年EPS均通过'] for r in rows);aligned=all(r['年度映射一致'] for r in rows)
    return {'主来源':'东财批量' if passed>=45 and aligned else '同花顺','两年EPS均通过数':passed,
            '分母':50,'通过比例':passed/50,'年度映射全部一致':aligned,
            '机构数口径一致':all(all(r['机构数逐年相同'].values()) for r in rows),
            '规则':RULE,'规则SHA256':RULE_SHA,'审核依据':REVIEW}


def industry_members(root):
    root=Path(root);path=root/'data/v3/申万成分.json';raw=json.loads(path.read_text());names={}
    # 只读取已有选股程序的常量名称，不执行其akshare依赖或选股逻辑。
    for node in ast.parse((root/'scripts/v3_select.py').read_text()).body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SW' for t in node.targets):names=ast.literal_eval(node.value)
    result={}
    for industry,obj in raw.items():
        for item in obj['rows']:
            code=item['证券代码']
            if code in result and result[code]['申万一级代码']!=industry:raise ValueError('股票成分跨一级行业重复')
            result[code]={'code':code,'name':item['证券名称'],'申万一级代码':industry,'申万一级':names.get(industry,industry),'成分观测日':obj['date']}
    if not result:raise ValueError('申万成分为空')
    return result,hashlib.sha256(path.read_bytes()).hexdigest()
