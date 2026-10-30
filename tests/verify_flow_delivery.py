"""真实捕获的独立数值核对、隔离重跑、错主机名负例；不在本机抓取。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import ssl
import subprocess
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from auto_judgment_common import BJ, atomic_text
from flow_indicators import run, dump
from flow_metrics import read_inputs
from flow_source import NEW, START, START31


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-tls',action='store_true');args=parser.parse_args()
    report=json.loads((ROOT/'logs/资金轮动_最新.json').read_text());target=report['data_date'];B,names,C,A=read_inputs(ROOT,target)
    table=pd.read_csv(ROOT/'data/资金轮动/日表.csv',dtype={'date':str,'code':str}).drop_duplicates(['date','code'],keep='last')
    assert table['date'].min()==START
    assert table.loc[table.code.isin(NEW),'占比_28'].isna().all()
    assert table.loc[table.date<START31,'占比_31'].isna().all()
    for scope,start in [('28',START),('31',START31)]:
        grouped=table[table.date>=start].groupby('date')['占比_'+scope]
        sums=grouped.sum(min_count=28 if scope=='28' else 31).dropna();assert np.allclose(sums,1,atol=1e-9)
    checks=[];codes=random.Random(20261030).sample(sorted(set(names)-NEW),3)
    for code in codes:
        for day in (START,START31,target):
            row=table[(table.code==code)&(table.date==day)].iloc[0];i=B.index.get_loc(day)
            for scope,universe in [('28',sorted(set(names)-NEW)),('31',sorted(names))]:
                values=A.loc[day,universe];den=float(values.sum()) if values.notna().all() and (scope=='28' or day>=START31) else None
                expected=float(A.loc[day,code])/den if den and pd.notna(A.loc[day,code]) else None;actual=row['占比_'+scope]
                assert (expected is None and pd.isna(actual)) or np.isclose(expected,actual,atol=1e-11)
                checks.append({'date':day,'code':code,'行业':names[code],'口径':scope,'分子':float(A.loc[day,code]) if pd.notna(A.loc[day,code]) else None,'分母':den,'独立相除':expected,'日表值':float(actual) if pd.notna(actual) else None})
            if i>=20 and pd.notna(C.loc[day,code]) and pd.notna(C.iloc[i-20][code]):
                independent=C.loc[day,code]/C.iloc[i-20][code]-B.iloc[i]/B.iloc[i-20]
                assert np.isclose(independent,row['相对沪深300_20'],atol=1e-10)
    # 真实CSV原字节保护：隔离副本重算，不改正式工作区。
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for path in ('scripts','data/资金轮动'):
            shutil.copytree(ROOT/path,root/path,ignore=shutil.ignore_patterns('__pycache__'))
        for path in ('data/行情库','data/标的清单.csv','data/市场状态/日指标.csv','system/交易所休市.json'):
            src=ROOT/path;dest=root/path;dest.parent.mkdir(parents=True,exist_ok=True)
            if src.is_dir():shutil.copytree(src,dest)
            else:shutil.copyfile(src,dest)
        history={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'data/资金轮动').rglob('*.csv')}
        repeated=run(root,offline=True)
        assert repeated['新增日表行']==0 and repeated['新增ETF行']==0
        assert all(hashlib.sha256((root/p).read_bytes()).hexdigest()==sha for p,sha in history.items())
    proof={'数据日期':target,'独立占比核对':checks,'28/31每日有效占比和为1':True,'新3行业28列全空':True,'31列断点前全空':True,'真实历史CSV隔离重跑原字节不变':True,'单项修订原前缀保留':'另见test_flow合成测试'}
    out=ROOT/'logs/test/T8b';dump(out/'独立核对.json',proof)
    lines=['# T8b 独立核对','', '3行业×3时点×28/31口径，直接按原始bargainsum相除。真实历史CSV隔离重跑不追加且SHA全一致。','',
           '|日期|代码|行业|口径|分子|分母|独立相除|日表值|','|---|---|---|---|---:|---:|---:|---:|']
    def v(x):return '缺' if x is None else str(round(x,10)) if isinstance(x,float) else str(x)
    for r in checks:lines.append('|'+ '|'.join(v(r[k]) for k in ('date','code','行业','口径','分子','分母','独立相除','日表值'))+'|')
    atomic_text(out/'独立核对.md','\n'.join(lines)+'\n')
    if args.negative_tls:
        if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('TLS负例只在Actions，不在本机联网')
        assert report['本次在线TLS验证通过'] and report['资金来源']=='申万官网'
        leaf=out/'执行'/os.environ['GITHUB_RUN_ID']/'leaf.pem'
        failure=subprocess.run(['openssl','verify','-CAfile',ssl.get_default_verify_paths().cafile,'-untrusted',str(ROOT/'system/证书/申万中间CA.pem'),'-purpose','sslserver','-verify_hostname','invalid-flow-test.example',str(leaf)],capture_output=True,text=True,timeout=10)
        assert failure.returncode!=0 and 'hostname mismatch' in (failure.stdout+failure.stderr).lower()
        dump(out/'TLS负例.json',{'错误主机名验证退出码':failure.returncode,'输出':failure.stdout+failure.stderr,'未修改生产证书':True})
        restored=subprocess.run(['openssl','verify','-CAfile',ssl.get_default_verify_paths().cafile,'-untrusted',str(ROOT/'system/证书/申万中间CA.pem'),'-purpose','sslserver','-verify_hostname','www.swsresearch.com',str(leaf)],capture_output=True,text=True,timeout=10)
        assert restored.returncode==0
        dump(out/'TLS恢复.json',{'正确主机名验证退出码':restored.returncode,'输出':restored.stdout+restored.stderr})
        normal=json.loads((ROOT/'logs/资金轮动_告警.json').read_text());normal['checked_at']=dt.datetime.now(BJ).isoformat()
        normal['items'][0]['reasons']=['验收恢复：正确主机名与系统根链验证通过，数据主源正常']
        bad=json.loads(json.dumps(normal));bad['items'][0].update(status='失败',reasons=['验收模拟：当前叶证书用错误主机名验证被openssl拒绝；不是生产故障，未改源数据或CA'])
        dump(out/'模拟故障告警.json',bad);dump(out/'恢复告警.json',normal)
    print('真实CSV保护、28/31断点与18行独立占比核对通过',flush=True)


if __name__=='__main__':main()
