#!/usr/bin/env python3
"""一致预期每日完整快照：准备→有界分批抓取→原子新增和行业四周变化。"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import sys
import time

from auto_judgment_common import TradingCalendar, atomic_text, read_csv
from consensus_common import BJ, HTTP, RULE, RULE_SHA, compare_row, decide, fetch_em, fetch_ths, industry_members

ROOT=Path(__file__).resolve().parents[1]
FIELDS=['code','name','申万一级','eps_本年','eps_次年','机构数','来源','抓取时间','预测本年','预测次年','申万一级代码','成分观测日','成分SHA256','机构数_东财','机构数_同花顺_本年','机构数_同花顺_次年','东财计数抓取时间','缺失说明','来源URL']
INDUSTRY=['日期','申万一级代码','申万一级','预测年度','基准日期','行业成分家数','当日有预测家数','可比家数','上修家数','下修家数','不变家数','上修占比','下修占比','说明']
BATCH_SIZE=240


def dump(path,obj):atomic_text(path,json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def load(path):return json.loads(Path(path).read_text())
def fingerprint(root):
    return hashlib.sha256(b''.join((Path(root)/'scripts'/p).read_bytes() for p in ('consensus_common.py','update_consensus.py','probe_consensus.py'))).hexdigest()
def csv_text(fields,rows):
    out=io.StringIO(newline='');writer=csv.DictWriter(out,fields,lineterminator='\n');writer.writeheader();writer.writerows(rows);return out.getvalue()


def checked_decision(report):
    if not report.get('程序完成') or report.get('规则SHA256')!=RULE_SHA:raise ValueError('来源比对未完成或规则指纹不符')
    universe=report['抽样总体代码']
    if universe!=sorted(set(universe)) or len(universe)!=report['东财全量']['总数']:raise ValueError('抽样总体不完整或重复')
    chosen=random.Random(RULE['随机种子']).sample(universe,50)
    if chosen!=report['固定抽样代码'] or chosen!=[r['code'] for r in report['比对']]:raise ValueError('50只与事先固定的随机抽样不一致')
    rows=[compare_row(r['东财'],r['同花顺'],report['year']) for r in report['比对']]
    result=decide(rows)
    if result!=report['来源决策']:raise ValueError('来源决策与50只原始值重算不一致')
    return result


def prepare(root,work,comparison=None,now=None,input_root=None):
    root=Path(root);work=Path(work);work.mkdir(parents=True,exist_ok=True);now=(now or dt.datetime.now(BJ)).astimezone(BJ)
    inputs=Path(input_root) if input_root else root
    date=now.date().isoformat();cal=TradingCalendar(inputs)
    if not cal.is_open(now.date()):
        obj={'执行':False,'日期':date,'原因':'交易所休市，不抓取'};dump(work/'准备.json',obj);return obj
    path=root/f'data/一致预期/{date[:7]}/{date}.csv'
    if path.exists():
        proof=root/f'data/一致预期/验收/{date}.json'
        if not read_csv(path) or not proof.exists() or load(proof)['快照SHA256']!=hashlib.sha256(path.read_bytes()).hexdigest():raise ValueError('已有快照缺完整验收或SHA不符，不覆盖')
        obj={'执行':False,'日期':date,'原因':'当日快照已完整提交，不覆盖不重抓'};dump(work/'准备.json',obj);return obj
    if comparison:report=load(comparison)
    else:
        candidates=[]
        for p in sorted((root/'data/一致预期/来源选择').glob('*.json')):
            r=load(p)
            if r.get('year')==now.year and r.get('规则SHA256')==RULE_SHA:candidates.append(r)
        if candidates:report=candidates[-1]
        else:
            from compare_consensus import run
            report=run(root,work/'来源比对')
    decision=checked_decision(report)
    if report['year']!=now.year:raise ValueError('跨年须重新50只比对，不沿用旧年决策')
    members,member_sha=industry_members(inputs);warnings=[];client=HTTP(time.monotonic()+900)
    # 同花顺主源不依赖东财可用性；辅助计数取不到只留空警告。
    try:em=fetch_em(client,now.year)
    except Exception as exc:
        if decision['主来源']=='东财批量':raise
        em={'rows':[],'抓取时间':None};warnings.append('辅助东财计数不可用：'+str(exc))
    em_rows={r['code']:r for r in em['rows']}
    stocks=[dict(r,**members.get(r['code'],{})) for r in em['rows']] if decision['主来源']=='东财批量' else [members[c] for c in sorted(members)]
    batches=(len(stocks)+BATCH_SIZE-1)//BATCH_SIZE
    obj={'执行':True,'日期':date,'year':now.year,'主来源':decision['主来源'],'来源比对':report,
         'stocks':stocks,'成员映射':members,'成分SHA256':member_sha,'来源输入提交':os.environ.get('CONSENSUS_INPUT_SHA'),
         '东财全量家数':len(em_rows),'东财计数':{c:r['机构数_东财'] for c,r in em_rows.items()},'东财计数抓取时间':em['抓取时间'],
         '总数':len(stocks),'批次数':batches,'每批上限':BATCH_SIZE,'实现指纹':fingerprint(root),
         'warnings':warnings,'交易日序号':sum(d<now.date() for d in cal.days),'源请求':client.trace}
    obj['原页抽查代码']=[s['code'] for s in random.Random(now.date().toordinal()).sample(stocks,min(5,len(stocks))) ]
    dump(work/'准备.json',obj);return obj


def snapshot_row(stock,context,ths=None):
    code=stock['code'];year=context['year'];row={f:'' for f in FIELDS};ths=ths or {}
    eps=stock.get('eps',{}) if context['主来源']=='东财批量' else ths.get('eps',{})
    a,b=eps.get(str(year)),eps.get(str(year+1));counts=ths.get('机构数',{})
    missing=[f'{y}年EPS缺' for y,v in ((year,a),(year+1,b)) if v is None]
    if not stock.get('申万一级'):missing.append('不在现存14个申万一级成分范围，行业未分类')
    row.update({'code':code,'name':stock['name'],'申万一级':stock.get('申万一级',''),'eps_本年':a,'eps_次年':b,
                '来源':context['主来源'],'抓取时间':ths.get('抓取时间') or context['东财计数抓取时间'],
                '预测本年':year,'预测次年':year+1,'申万一级代码':stock.get('申万一级代码',''),'成分观测日':stock.get('成分观测日',''),
                '成分SHA256':context['成分SHA256'],'机构数_东财':context['东财计数'].get(code),
                '机构数_同花顺_本年':counts.get(str(year)),'机构数_同花顺_次年':counts.get(str(year+1)),
                '东财计数抓取时间':context['东财计数抓取时间'],'缺失说明':'；'.join(missing),
                '来源URL':ths.get('来源') or 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_WEB_RESPREDICT'})
    for key in ('eps_本年','eps_次年'):
        if row[key] is not None and not math.isfinite(float(row[key])):raise ValueError('EPS非有限值')
    return row


def batch(context,index,output):
    now=dt.datetime.now(BJ)
    if now.date().isoformat()!=context['日期']:raise ValueError('已跨日，不为昨天补造快照')
    if not 0<=index<context['批次数']:raise ValueError('批次编号越界')
    stocks=context['stocks'][index*BATCH_SIZE:(index+1)*BATCH_SIZE];client=HTTP(time.monotonic()+3000)
    obj={'日期':context['日期'],'批次':index,'主来源':context['主来源'],'实现指纹':context['实现指纹'],
         '预期代码':[s['code'] for s in stocks],'rows':[],'errors':[],'warnings':[],'请求':client.trace,'完成':False,'抽查':[],'原页核对':[]}
    def save():dump(output,obj)
    save()
    for stock in stocks:
        try:
            ths=None
            if context['主来源']=='同花顺':
                for attempt in range(2):
                    try:ths=fetch_ths(client,stock['code'],context['year'],allow_partial=True);break
                    except ValueError:raise
                    except Exception:
                        if attempt:raise
                        time.sleep(2)
            obj['rows'].append(snapshot_row(stock,context,ths))
            if stock['code'] in context['原页抽查代码']:
                obj['原页核对'].append({'code':stock['code'],'快照':obj['rows'][-1],
                                      '来源原字段':ths if ths is not None else stock,
                                      '响应SHA256':client.trace[-1].get('SHA256') if ths is not None else None})
        except Exception as exc:obj['errors'].append(stock['code']+'：'+type(exc).__name__+': '+str(exc))
        if len(obj['rows'])%20==0:save()
        if time.monotonic()>client.deadline-20:
            obj['errors'].append('批次50分钟预算已到，未完成部分不填旧值');break
    if context['主来源']=='东财批量' and index==0:
        start=context['交易日序号']*20%len(context['stocks'])
        for offset in range(min(20,len(context['stocks']))):
            stock=context['stocks'][(start+offset)%len(context['stocks'])]
            try:
                ths=fetch_ths(client,stock['code'],context['year']);r=compare_row(stock,ths,context['year']);obj['抽查'].append(r)
                if not r['两年EPS均通过']:obj['warnings'].append(stock['code']+'轮换抽查缺报或差异超过2%')
            except Exception as exc:
                obj['抽查'].append({'code':stock['code'],'失败':str(exc)});obj['warnings'].append(stock['code']+'轮换抽查失败：'+str(exc))
    obj['完成']=not obj['errors'] and len(obj['rows'])==len(stocks);obj['结束北京时间']=dt.datetime.now(BJ).isoformat();save();return obj


def eps_for_year(row,year):
    for suffix in ('本年','次年'):
        if int(row['预测'+suffix])==year:
            value=row['eps_'+suffix]
            return float(value) if value not in ('',None) else None
    return None


def industry_changes(date,current,history,members):
    cutoff=(dt.date.fromisoformat(date)-dt.timedelta(days=28)).isoformat()
    available=sorted(d for d in history if d<=cutoff);baseline=available[-1] if available else None
    previous={r['code']:r for r in history[baseline]} if baseline else {};groups={}
    for row in members.values():groups.setdefault((row['申万一级代码'],row['申万一级']),set()).add(row['code'])
    year=int(date[:4]);result=[]
    for (code,name),pool in sorted(groups.items()):
        now_rows=[r for r in current if r['申万一级代码']==code]
        for y in (year,year+1):
            row={f:'' for f in INDUSTRY};row.update({'日期':date,'申万一级代码':code,'申万一级':name,'预测年度':y,'基准日期':baseline or '',
                     '行业成分家数':len(pool),'当日有预测家数':sum(eps_for_year(r,y) is not None for r in now_rows)})
            if baseline is None:row['说明']='不足四周，无基准快照，变化和比例留空'
            else:
                changes=[]
                for r in now_rows:
                    old=previous.get(r['code']);a=eps_for_year(r,y)
                    if not old or old['申万一级代码']!=code or old['来源']!=r['来源']:continue
                    b=eps_for_year(old,y)
                    if a is not None and b is not None:changes.append(a-b)
                n=len(changes);up=sum(v>0 for v in changes);down=sum(v<0 for v in changes)
                row.update({'可比家数':n,'上修家数':up if n else '', '下修家数':down if n else '', '不变家数':n-up-down if n else '',
                            '上修占比':up/n if n else '', '下修占比':down/n if n else '',
                            '说明':'只计同股票/预测年度/一级行业/来源的双端有效EPS' if n else '基准存在但无同口径可比样本，变化及比例留空'})
            result.append(row)
    return result


def assemble(root,context,batch_dir,now=None):
    root=Path(root);now=(now or dt.datetime.now(BJ)).astimezone(BJ);date=context['日期'];errors=[];warnings=list(context['warnings']);rows=[];audits=[];original_checks=[]
    if fingerprint(root)!=context['实现指纹']:errors.append('准备与汇总程序指纹不同，不混用结果')
    if now.date().isoformat()!=date:errors.append('跨日，不补造昨天快照')
    if not TradingCalendar(root).is_open(dt.date.fromisoformat(date)):errors.append('非真实交易日，拒绝写快照')
    members=context.get('成员映射')
    if members is None:
        members,member_sha=industry_members(root)
        if member_sha!=context['成分SHA256']:errors.append('准备后申万成分变更，不混用两个范围')
    checked_decision(context['来源比对'])
    for i in range(context['批次数']):
        p=Path(batch_dir)/f'批次_{i}.json'
        if not p.exists():errors.append(f'缺批次{i}');continue
        obj=load(p)
        if obj.get('批次')!=i or obj.get('日期')!=date or obj.get('主来源')!=context['主来源'] or obj.get('实现指纹')!=context['实现指纹']:errors.append(f'批次{i}身份/日期/指纹异常');continue
        errors.extend(obj['errors']);warnings.extend(obj['warnings']);audits.extend(obj.get('抽查',[]))
        original_checks.extend(obj.get('原页核对',[]))
        expected=[r['code'] for r in context['stocks'][i*BATCH_SIZE:(i+1)*BATCH_SIZE]]
        if not obj.get('完成') or sorted(r['code'] for r in obj['rows'])!=sorted(expected):errors.append(f'批次{i}不完整或代码不符')
        rows.extend(obj['rows'])
    for row in rows:
        if row.get('来源')!=context['主来源'] or dt.datetime.fromisoformat(row['抓取时间']).astimezone(BJ).date().isoformat()!=date:errors.append(row.get('code','未知')+'来源或抓取日期不符')
        for f in ('eps_本年','eps_次年'):
            if row[f] not in (None,'') and not math.isfinite(float(row[f])):errors.append(row['code']+'非有限EPS')
    if len(rows)!=context['总数'] or len({r['code'] for r in rows})!=len(rows):errors.append('全量股票数量不足或重复')
    report={'title':'一致预期快照','generated_at':now.isoformat(),'data_date':date,'status':'失败' if errors else '正常','errors':errors,
            'warnings':warnings,'主来源':context['主来源'],'股票数':len(rows),'范围应有':context['总数'],'实现指纹':context['实现指纹'],
            '来源决策':context['来源比对']['来源决策'],'抽查':audits,'有效两年EPS家数':sum(r['eps_本年'] is not None and r['eps_次年'] is not None for r in rows)}
    if not errors:
        base=root/'data/一致预期';path=base/date[:7]/f'{date}.csv';rows.sort(key=lambda r:r['code'])
        content=csv_text(FIELDS,rows)
        if path.exists() and path.read_text()!=content:raise ValueError('当日已有不同快照，拒绝改写')
        history={p.stem:read_csv(p) for p in base.glob('????-??/????-??-??.csv') if p.stem<date}
        changes=industry_changes(date,rows,history,members);table=base/'行业上修.csv';old=table.read_text() if table.exists() else ''
        if old and next(csv.reader(io.StringIO(old)))!=INDUSTRY:raise ValueError('行业上修表头异常，不覆盖')
        existing={(r['日期'],r['申万一级代码'],r['预测年度']):r for r in csv.DictReader(io.StringIO(old))} if old else {}
        additions=[]
        for r in changes:
            key=(date,r['申万一级代码'],str(r['预测年度']))
            if key in existing:
                normalized={k:'' if v is None else str(v) for k,v in r.items()}
                if existing[key]!=normalized:raise ValueError('已有行业结果不同，拒绝改写')
            else:additions.append(r)
        stream=io.StringIO(newline='');w=csv.DictWriter(stream,INDUSTRY,lineterminator='\n')
        if not old:w.writeheader()
        w.writerows(additions)
        decision=context['来源比对'];selection_day=decision['北京时间'][:10];selection=base/'来源选择'/f'{selection_day}.json'
        if selection.exists() and load(selection)!=decision:raise ValueError('已有来源选择不同，不覆盖')
        # 先完成全部校验再写；中断后同输入重跑幂等补全，历史记录不删除。
        if not selection.exists():dump(selection,decision)
        if not path.exists():atomic_text(path,content)
        atomic_text(table,old+('' if not old or old.endswith('\n') else '\n')+stream.getvalue())
        full_scope=set(r['code'] for r in context['stocks'])==set(members) if context['主来源']=='同花顺' else len(rows)==context.get('东财全量家数',0)>=50
        acceptance={'日期':date,'状态':'完整真实交易日' if full_scope else '小样本，不计五日','完整范围':context['总数'],'主来源':context['主来源'],'实现指纹':context['实现指纹'],
                    '规则SHA256':RULE_SHA,'快照SHA256':hashlib.sha256(content.encode()).hexdigest(),'Actions':f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}",
                    '来源输入提交':context.get('来源输入提交'),'成分SHA256':context['成分SHA256'],
                    '计入五交易日验收':full_scope,'完成北京时间':now.isoformat()}
        proof=base/'验收'/f'{date}.json'
        if not proof.exists():dump(proof,acceptance)
        audit_path=base/'抽查'/f'{date}.json'
        if not audit_path.exists():dump(audit_path,{'日期':date,'主来源':context['主来源'],'轮换20抽查':audits,'原页核对':original_checks,'warnings':warnings})
        report['行业数']=len({r['申万一级代码'] for r in rows if r['申万一级代码']});report['行业未分类家数']=sum(not r['申万一级代码'] for r in rows)
        report['行业上修四周可用']=any(r['基准日期'] for r in changes)
        dump(base/'最新.json',report)
        atomic_text(base/'最新.md',render(report))
    dump(root/'logs/一致预期_最新.json',report);atomic_text(root/'logs/一致预期_最新.md',render(report));return report


def render(r):
    return '# 一致预期快照\n\n北京时间：'+r['generated_at']+'\n\n数据日期：'+r['data_date']+'；状态：'+r['status']+'；主来源：'+r['主来源']+f"\n\n股票数：{r['股票数']}/{r['范围应有']}；两年EPS有效：{r['有效两年EPS家数']}。\n\n错误："+json.dumps(r['errors'],ensure_ascii=False)+'\n\n警告：'+json.dumps(r['warnings'],ensure_ascii=False)+'\n\n四周不足的上修/下修比例留空；来源更新时间未知；指标解释【待判读】。\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--work',type=Path,required=True);p.add_argument('--comparison',type=Path);p.add_argument('--inputs-root',type=Path)
    p=sub.add_parser('batch');p.add_argument('--prepared',type=Path,required=True);p.add_argument('--index',type=int,required=True);p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('assemble');p.add_argument('--prepared',type=Path,required=True);p.add_argument('--batches',type=Path,required=True)
    args=parser.parse_args()
    if args.mode in ('prepare','batch') and os.environ.get('GITHUB_ACTIONS')!='true':parser.error('联网仅限Actions；本机请运行离线测试')
    if args.mode=='prepare':
        obj=prepare(args.root,args.work,args.comparison,input_root=args.inputs_root)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('execute='+str(obj['执行']).lower()+'\nmatrix='+json.dumps(list(range(obj.get('批次数',0))))+'\n')
        print(json.dumps({k:v for k,v in obj.items() if k in ('执行','原因','日期','主来源','总数','批次数')},ensure_ascii=False));return 0
    if args.mode=='batch':return 0 if batch(load(args.prepared),args.index,args.out)['完成'] else 1
    return 0 if not assemble(args.root,load(args.prepared),args.batches)['errors'] else 1


if __name__=='__main__':raise SystemExit(main())
