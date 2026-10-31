#!/usr/bin/env python3
"""按PR17预先固定规则随机50只比对，两年EPS/年度映射自动决定来源。"""
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import random
import sys
import time

from auto_judgment_common import atomic_text
from consensus_common import BJ, HTTP, RULE, RULE_SHA, compare_row, decide, fetch_em, fetch_ths

ROOT=Path(__file__).resolve().parents[1]


def run(root=ROOT,out=None):
    root=Path(root);now=dt.datetime.now(BJ);client=HTTP(time.monotonic()+900);out=Path(out) if out else root/'logs/test/T12b';out.mkdir(parents=True,exist_ok=True)
    # 完整取东财后，固定随机种子一次取50只；任何失败不重抽或剔除。
    full=fetch_em(client,now.year);chosen=random.Random(RULE['随机种子']).sample(full['rows'],50)
    report={'北京时间':now.isoformat(),'year':now.year,'规则SHA256':RULE_SHA,'规则':RULE,
            'Actions':f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}",
            '源提交':os.environ.get('GITHUB_SHA'),'东财全量':{'总数':full['总数'],'页数':full['页数']},
            '抽样总体代码':[r['code'] for r in full['rows']],
            '固定抽样代码':[r['code'] for r in chosen],'比对':[],'请求':client.trace,'程序完成':False}
    atomic_text(out/'东财全量.json',json.dumps(full,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    def save():atomic_text(out/'来源比对_50.json',json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    save()
    for em in chosen:
        try:ths=fetch_ths(client,em['code'],now.year)
        except Exception as exc:ths={'失败':type(exc).__name__+': '+str(exc),'eps':{},'机构数':{}}
        row=compare_row(em,ths,now.year);report['比对'].append(row);save()
        print(em['code']+' 两年通过='+str(row['两年EPS均通过'])+' 年度一致='+str(row['年度映射一致']),flush=True)
    report['来源决策']=decide(report['比对']);report['程序完成']=True;report['结束北京时间']=dt.datetime.now(BJ).isoformat();save()
    fields=['code','name','东财本年','同花顺本年','本年相对差','东财次年','同花顺次年','次年相对差','东财计数','同花顺本年机构','同花顺次年机构','年度一致','两年EPS通过','说明']
    rows=[]
    for r in report['比对']:
        a,b=str(now.year),str(now.year+1);ths=r['同花顺'];em=r['东财'];eps=ths.get('eps',{});n=ths.get('机构数',{})
        rows.append([r['code'],r['name'],em['eps'][a],eps.get(a),r['相对差'][a],em['eps'][b],eps.get(b),r['相对差'][b],em['机构数_东财'],n.get(a),n.get(b),r['年度映射一致'],r['两年EPS均通过'],ths.get('失败',ths.get('状态',''))])
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(fields);writer.writerows(rows);atomic_text(out/'来源比对_50.csv',stream.getvalue())
    lines=['# 东财/同花顺50只固定随机比对','',f"Actions：{report['Actions']}；北京时间{now.isoformat()}。",'',
           '规则在取数前固定：分母50只不删缺失，不重抽；同花顺为相对差分母，零对零通过、零对非零不通过；YEAR+YEAR_MARK=E定位预测年度。机构数差异不改变EPS门槛。','',
           json.dumps(report['来源决策'],ensure_ascii=False),'', '|'+ '|'.join(fields)+'|','|'+'|'.join('---' for _ in fields)+'|']
    def cell(v):return '缺' if v is None else str(round(v,8)) if isinstance(v,float) else str(v).replace('|','/')
    lines.extend('|'+ '|'.join(cell(v) for v in row)+'|' for row in rows)
    lines+=['','原年度/原EPS字段、同花顺EPS原表行与HTTP响应指纹见JSON；东财RATING_ORG_NUM不合并为同花顺年度机构数。','']
    atomic_text(out/'来源比对_50.md','\n'.join(lines));print(json.dumps(report['来源决策'],ensure_ascii=False),flush=True)
    return report


if __name__=='__main__':
    if os.environ.get('GITHUB_ACTIONS')!='true':sys.exit('只允许Actions联网比对')
    run()
