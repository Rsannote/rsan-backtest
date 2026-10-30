"""离线抽查已存派生值：独立从原始可见观测重算起止值与20日bp变化。"""
import csv
import datetime as dt
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from auto_judgment_common import BJ, TradingCalendar, atomic_text


def read(path):
    with path.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))


def main():
    calendar=TradingCalendar(ROOT)
    derived=read(ROOT/'data/宏观/派生.csv')
    proof=[]
    for name in ('中国10年国债','美国10年国债'):
        original=read(ROOT/'data/宏观'/f'{name}.csv')
        entries=[r for r in derived if r['指标']==name]
        assert len(entries)>1000, '不得用小样本冒充2015以来派生值'
        for index in sorted({0,len(entries)//2,len(entries)-1}):
            item=entries[index]
            assert calendar.window(item['起点A股日'],20)[-1].isoformat()==item['date']
            endpoints=[]
            for date in (item['起点A股日'],item['date']):
                cutoff=dt.datetime.fromisoformat(date+'T18:00:00').replace(tzinfo=BJ)
                eligible=[r for r in original if dt.datetime.fromisoformat(r['可见时刻'])<=cutoff]
                # 首先取最新观测期，再取当时可见的最新修订记录。
                row=max(eligible,key=lambda r:(r['date'],r['记录时间']))
                endpoints.append(row)
            assert [r['date'] for r in endpoints]==[item['起点观测日'],item['终点观测日']]
            value=(float(endpoints[1]['value'])-float(endpoints[0]['value']))*100
            assert abs(value-float(item['value']))<1e-7
            proof.append({'指标':name,'起点A股日':item['起点A股日'],'终点A股日':item['date'],
                          '起点观测日':endpoints[0]['date'],'终点观测日':endpoints[1]['date'],
                          '起点收益率':endpoints[0]['value'],'终点收益率':endpoints[1]['value'],
                          '独立重算bp':round(value,8),'CSV中bp':item['value']})
    lines=['# 中美10年20日变化离线核对','','两端均按A股日18:00已可见的原始收益率独立重算；窗口固定20个A股交易日，不调用宏观派生函数。','',
           '|指标|起点A股日|终点A股日|起点观测日|终点观测日|起点收益率%|终点收益率%|重算bp|CSV中bp|',
           '|---|---|---|---|---|---:|---:|---:|---:|']
    for r in proof:lines.append('|'+ '|'.join(str(v) for v in r.values())+'|')
    atomic_text(ROOT/'logs/test/T6b/派生核对.md','\n'.join(lines)+'\n')
    atomic_text(ROOT/'logs/test/T6b/派生核对.json',json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    print(f'派生独立核对{len(proof)}行通过')


if __name__=='__main__':main()
