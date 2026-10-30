#!/usr/bin/env python3
"""离线核验同实现、同规则的连续五个真实交易日；不抓取、不补数。"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

from auto_judgment_common import TradingCalendar, atomic_text, read_csv
from consensus_common import BJ, RULE_SHA
from update_consensus import fingerprint

ROOT=Path(__file__).resolve().parents[1]


def check(root=ROOT,now=None):
    root=Path(root);now=(now or dt.datetime.now(BJ)).astimezone(BJ);cal=TradingCalendar(root);implementation=fingerprint(root)
    base=root/'data/一致预期';accepted={};excluded=[]
    for p in sorted((base/'验收').glob('*.json')):
        proof=json.loads(p.read_text());date=proof['日期'];day=dt.date.fromisoformat(date)
        reason=[];snapshot=base/date[:7]/f'{date}.csv'
        if not proof.get('计入五交易日验收'):reason.append('小样本或未获完整资格')
        if proof.get('实现指纹')!=implementation or proof.get('规则SHA256')!=RULE_SHA:reason.append('非当前同一实现或规则')
        if day>now.date() or not cal.is_open(day):reason.append('未来日或非交易日')
        if not snapshot.exists():reason.append('缺快照')
        elif hashlib.sha256(snapshot.read_bytes()).hexdigest()!=proof['快照SHA256']:reason.append('快照SHA不符')
        else:
            rows=read_csv(snapshot)
            if len(rows)!=proof['完整范围'] or len({r['code'] for r in rows})!=len(rows):reason.append('范围数量/唯一性不符')
            for row in rows:
                if row['来源']!=proof['主来源'] or dt.datetime.fromisoformat(row['抓取时间']).astimezone(BJ).date()!=day:reason.append('来源或实际抓取日不符');break
                if (int(row['预测本年']),int(row['预测次年']))!=(day.year,day.year+1):reason.append('预测年度错配');break
                if any(row[f] and not math.isfinite(float(row[f])) for f in ('eps_本年','eps_次年')):reason.append('EPS非有限数值');break
        if reason:excluded.append({'日期':date,'原因':reason})
        else:accepted[date]=proof
    continuous=[]
    if accepted:
        day=dt.date.fromisoformat(max(accepted))
        while day.isoformat() in accepted:
            continuous.append(accepted[day.isoformat()]);day=cal.previous(day)
    continuous.reverse()
    return {'检查北京时间':now.isoformat(),'当前实现指纹':implementation,'规则SHA256':RULE_SHA,
            '同实现完整日期':sorted(accepted),'最近连续合格交易日数':len(continuous),'达到五日':len(continuous)>=5,
            '最近五日':continuous[-5:],'排除':excluded,'说明':'按官方日历连续；周末/休市跳过；合成/小样本/旧实现/坏SHA不计。'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--out',type=Path,default=ROOT/'logs/test/T12b');args=parser.parse_args()
    report=check(args.root);args.out.mkdir(parents=True,exist_ok=True)
    atomic_text(args.out/'五日验收.json',json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# 一致预期五日验收','',f"检查时间：{report['检查北京时间']}；连续合格真实交易日：{report['最近连续合格交易日数']}/5。",'',
           '|日期|完整范围|主来源|Actions|','|---|---:|---|---|']
    for r in report['最近五日']:lines.append(f"|{r['日期']}|{r['完整范围']}|{r['主来源']}|{r['Actions']}|")
    lines+=['','同实现和规则指纹：`'+report['当前实现指纹']+'` / `'+RULE_SHA+'`。','',json.dumps(report['排除'],ensure_ascii=False),'']
    atomic_text(args.out/'五日验收.md','\n'.join(lines));print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
