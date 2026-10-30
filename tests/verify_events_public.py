"""仅Actions：用公开腾讯历史K线重新核对2008/2015/2018/2022区间端点。"""
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from auto_judgment_common import atomic_text
from update_hangqing import fetch_segment


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('公开行情核对只能在Actions联网')
    with (ROOT/'data/事件库/大调整.csv').open(newline='') as f:
        records={r['事件id']:r for r in csv.DictReader(f)}
    with (ROOT/'data/行情库/指数/sh000300.csv').open(newline='') as f:
        local={r['date']:float(r['close'])*float(r['factor']) for r in csv.DictReader(f)}
    proof=[]
    for year in (2008,2015,2018,2022):
        chosen=[r for r in records.values() if int(r['开始'][:4])<=year<=int(r['结束'][:4])]
        item=min(chosen,key=lambda r:float(r['幅度']))
        start,end=map(dt.date.fromisoformat,(item['开始'],item['结束']))
        values,note=fetch_segment('sh000300',start,end)
        assert values and not note,(year,note)
        for date in (item['开始'],item['结束']):
            assert date in values,(year,date,'公开源缺日')
            assert math.isclose(values[date][3],local[date],abs_tol=1e-6),date
        computed=values[item['结束']][3]/values[item['开始']][3]-1
        assert abs(computed-float(item['幅度']))<1e-9
        source=f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,{start},{end},900,'
        proof.append({'核对年份':year,'事件id':item['事件id'],'开始':item['开始'],'结束':item['结束'],'公开起点收盘':values[item['开始']][3],
                      '公开终点收盘':values[item['结束']][3],'公开重算幅度':computed,'事件表幅度':float(item['幅度']),'来源':source,
                      '说明':'公开行情同源重采核对，不冒称独立数据源；局部调整段不是全年跌幅'})
    lines=['# 大调整公开行情核对','','局部方向变化区间，与年度/整轮熊市的统计口径分别注明；未填写触发事件或归因。','',
           '|年份|事件id|开始|结束|公开起点|公开终点|重算幅度|表中幅度|公开来源|','|---|---|---|---|---:|---:|---:|---:|---|']
    for r in proof:lines.append(f"|{r['核对年份']}|{r['事件id']}|{r['开始']}|{r['结束']}|{r['公开起点收盘']}|{r['公开终点收盘']}|{r['公开重算幅度']:.6%}|{r['事件表幅度']:.6%}|[腾讯历史K线]({r['来源']})|")
    output=ROOT/'logs/test/T9';atomic_text(output/'公开行情核对.md','\n'.join(lines)+'\n')
    atomic_text(output/'公开行情核对.json',json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    print('2008/2015/2018/2022公开端点与幅度核对4行通过')


if __name__=='__main__':main()
