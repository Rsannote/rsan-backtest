#!/usr/bin/env python3
"""T13b 严格预检、逐股成功高水位与可回滚短发布。"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from earnings_history import (BJ, FIELDS, RAW_FIELDS, ROOT, SOURCE_CODE, SOURCE_VERSION,
                              URL, capture_path, deadline, load_v3, members,
                              parse_page, rows_digest, validate_amount, visibility)
from earnings_history_transaction import commit_files
from flow_source import append_rows


def time_value(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+08:00',value):
        raise ValueError('采集时间必须为完整北京时间')
    moment=dt.datetime.fromisoformat(value)
    if moment>dt.datetime.now(BJ)+dt.timedelta(minutes=5):
        raise ValueError('采集时间晚于当前时间')
    return moment


def integer(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>=0


def validate_payload(root,payload,expected_run,expected_sha):
    """验证所有结构、集合、时间、数字与HTML闭环，完全不写文件。"""
    try:
        return _validate_payload(root,payload,expected_run,expected_sha)
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('采集产物缺字段或类型不符：'+str(exc)) from exc


def _validate_payload(root,payload,expected_run,expected_sha):
    if not isinstance(payload,dict) or set(payload)!={'报告','单季记录','探测响应'}:
        raise ValueError('采集产物结构不完整')
    report,rows_by_code,captures=(payload[k] for k in ('报告','单季记录','探测响应'))
    if not isinstance(report,dict) or not isinstance(rows_by_code,dict) or not isinstance(captures,dict):
        raise ValueError('产物报告、记录及响应必须为对象')
    if not re.fullmatch(r'\d+',str(expected_run)) or not re.fullmatch(r'[0-9a-f]{40}',expected_sha):
        raise ValueError('发布运行或SHA无效')
    if report['来源分支']!='gpt/T13b-earnings' or report['来源运行']!=str(expected_run) or report['来源SHA']!=expected_sha:
        raise ValueError('产物分支/运行/SHA与发布请求不一致')
    if report['来源版本']!=SOURCE_VERSION or report['源代码依据']!=SOURCE_CODE or report['解析器SHA256']!=hashlib.sha256((root/'scripts/earnings_history.py').read_bytes()).hexdigest():
        raise ValueError('来源解析器版本已变化，需重新采集')
    snapshot,universe,digest=members(root)
    if report['成分SHA256']!=digest or report['快照行业数']!=len(snapshot) or report['成分家数']!=len(universe):
        raise ValueError('成分快照指纹或家数已变化')
    if report['模式'] not in ('probe','batch') or not isinstance(report['结果'],list):
        raise ValueError('采集模式或结果类型无效')
    stamp=report['生成时间'];time_value(stamp)
    if not integer(report['预算秒']) or not 1<=report['预算秒']<=3000:
        raise ValueError('采集预算无效')
    if not integer(report['断点跳过']) or not integer(report['本批尝试']) or report['本批尝试']!=len(report['结果']) or not integer(report['失败数']):
        raise ValueError('采集数量字段不一致')
    num,_,_=load_v3(root)
    results={};successful=set();response_codes=set()
    for record in report['结果']:
        code=record['代码']
        if not isinstance(code,str) or not re.fullmatch(r'\d{6}',code) or code in results:
            raise ValueError('股票代码无效或重复')
        if report['模式']=='batch' and code not in universe:
            raise ValueError('批量股票不在当前成分中')
        if record['采集时间']!=stamp or record['来源版本']!=SOURCE_VERSION or record['成分SHA256']!=digest or record['来源']!=URL.format(code=code):
            raise ValueError('逐股采集时间、版本或来源不一致')
        time_value(record['采集时间'])
        status=record['状态']
        if status not in ('成功','无2013起历史','失败'):
            raise ValueError('采集状态无效')
        if status=='失败':
            if not isinstance(record['错误'],str) or not record['错误']:
                raise ValueError('失败缺错误信息')
        else:
            successful.add(code)
        if any(k in record for k in ('响应文件','响应SHA256')):
            response_codes.add(code)
            expected=capture_path(stamp,code).as_posix()
            if report['模式']!='probe' or record['响应文件']!=expected or not re.fullmatch(r'[0-9a-f]{64}',record['响应SHA256']):
                raise ValueError('探测证据路径或SHA无效')
        if report['模式']=='probe' and status!='失败' and code not in response_codes:
            raise ValueError('成功探测缺原始响应')
        results[code]=record
    if report['失败数']!=sum(r['状态']=='失败' for r in results.values()):
        raise ValueError('采集失败数不一致')
    if set(rows_by_code)!=successful or set(captures)!=response_codes:
        raise ValueError('成功股票、单季载荷、响应集合不完整或不一致')
    for code,rows in rows_by_code.items():
        record=results[code]
        if not isinstance(rows,list) or not integer(record['行数']) or record['行数']!=len(rows):
            raise ValueError('产物行数不一致')
        if (record['状态']=='成功')!=bool(rows):
            raise ValueError('采集状态与空历史不一致')
        periods=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)!=set(RAW_FIELDS) or any(not isinstance(v,str) for v in row.values()):
                raise ValueError('财报载荷必需字段或原值类型不完整')
            period=row['报告期'];due=deadline(period)
            if period<'2013-01-01' or dt.date.fromisoformat(period)>dt.datetime.now(BJ).date() or row['法定截止']!=due.isoformat() or row['可见时刻']!=visibility(period) or row['来源']!=URL.format(code=code):
                raise ValueError('产物来源或可见边界不符')
            for key in ('扣非净利润','净利润','营业总收入'):
                validate_amount(row[key],num)
            kf,profit=num(row['扣非净利润']),num(row['净利润'])
            basis='扣非净利润' if not math.isnan(kf) else '净利润回退' if not math.isnan(profit) else '缺失'
            if row['利润口径']!=basis:raise ValueError('产物利润口径不符')
            periods.append(period)
        if periods!=sorted(set(periods)):
            raise ValueError('报告期重复或乱序')
        if record['起点']!=(periods[0] if periods else '') or record['终点']!=(periods[-1] if periods else '') or not integer(record['净利润回退行数']) or record['净利润回退行数']!=sum(r['利润口径']=='净利润回退' for r in rows):
            raise ValueError('采集范围或回退统计不一致')
        if record['载荷SHA256']!=rows_digest(rows):raise ValueError('单季载荷与状态指纹不一致')
    for code,text in captures.items():
        record=results[code]
        if not isinstance(text,str) or hashlib.sha256(text.encode()).hexdigest()!=record['响应SHA256']:
            raise ValueError('原始HTML或SHA不符')
        try:
            parsed=parse_page(text,code,num)
        except (ValueError,TypeError,KeyError):
            if record['状态']!='失败':raise ValueError('探测HTML无法重解析')
        else:
            if record['状态']=='失败' or parsed!=rows_by_code[code]:
                raise ValueError('探测HTML重解析与manifest不一致')
    return report,results,rows_by_code,captures


def validate_probe_evidence(root,evidence):
    report=evidence['采集报告']
    if report['模式']!='probe':raise ValueError('不是探测证据')
    captures={}
    for record in report['结果']:
        if '响应文件' in record:
            expected=capture_path(report['生成时间'],record['代码'])
            if record['响应文件']!=expected.as_posix():raise ValueError('探测证据路径不规范')
            captures[record['代码']]=(root/expected).read_text()
    payload={'报告':report,'单季记录':evidence['探测单季记录'],'探测响应':captures}
    _,results,_,_=validate_payload(root,payload,evidence['来源运行'],evidence['来源SHA'])
    if len({code for code,r in results.items() if r['状态']=='成功'})<3:
        raise ValueError('不足3只不同股票成功探测')


def watermark(root):
    """成功观测的高水位与数值修订分离：值未变、空历史也推进。"""
    base=root/'data/财报/全市场单季';output={}
    state=base/'采集状态.jsonl'
    if state.exists():
        for line in state.read_text().splitlines():
            record=json.loads(line)
            moment=time_value(record['采集时间'])
            if record['状态'] in ('成功','无2013起历史'):
                code=record['代码']
                digest=record.get('载荷SHA256')
                if code not in output or moment>output[code][0]:output[code]=(moment,digest)
                elif moment==output[code][0] and digest and output[code][1] and digest!=output[code][1]:raise ValueError('已有高水位同时间载荷冲突')
    for path in base.glob('*.csv'):
        with path.open(encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream)
            if reader.fieldnames!=FIELDS:raise ValueError('正式财报表头不符')
            for row in reader:
                moment=time_value(row['采集时间'])
                if path.stem not in output or moment>output[path.stem][0]:output[path.stem]=(moment,None)
    return output


def publish(root,artifact,expected_run,expected_sha,*,check_ancestry=True):
    root,artifact=Path(root).resolve(),Path(artifact)
    original=artifact.read_bytes();payload=json.loads(original)
    report,results,rows_by_code,captures=validate_payload(root,payload,expected_run,expected_sha)
    if check_ancestry:
        subprocess.run(['git','merge-base','--is-ancestor',expected_sha,'HEAD'],cwd=root,check=True)
    stamp=report['生成时间'];moment=time_value(stamp);high=watermark(root)
    if report['模式']=='batch':
        for code,record in results.items():
            if code not in high:continue
            latest,signature=high[code]
            if moment<latest:raise ValueError('旧采集产物低于最近成功观测高水位，整份拒绝')
            if moment==latest and record['状态']!='失败' and signature and signature!=record['载荷SHA256']:
                raise ValueError('同高水位时点的采集载荷冲突，整份拒绝')
    evidence=root/'logs/test/T13b'/('发布_'+str(expected_run)+'.json')
    artifact_hash=hashlib.sha256(original).hexdigest()
    if evidence.exists():
        old=json.loads(evidence.read_text())
        if old.get('产物SHA256')!=artifact_hash:raise ValueError('同运行产物与已发布证据不一致')
        return dict(old,追加行={code:0 for code in rows_by_code} if report['模式']=='batch' else {})
    changes={};added={};base=root/'data/财报/全市场单季'
    # 全量预检通过后在临时目录生成所有正式字节，任何失败均未晋升。
    with tempfile.TemporaryDirectory(prefix='.t13b-plan-',dir=root) as directory:
        stage=Path(directory)
        if report['模式']=='batch':
            for code,rows in rows_by_code.items():
                target=base/(code+'.csv');staged=stage/(code+'.csv')
                if target.exists():staged.write_bytes(target.read_bytes())
                # 相同时点不同值也拒绝，不能用同一采集时刻覆盖先前修订。
                old={}
                if target.exists():
                    with target.open(encoding='utf-8',newline='') as stream:old={r['报告期']:r for r in csv.DictReader(stream)}
                for row in rows:
                    prior=old.get(row['报告期'])
                    if prior and time_value(prior['采集时间'])==moment and any(prior[k]!=row[k] for k in RAW_FIELDS):
                        raise ValueError('同采集时点出现不同修订')
                added[code]=append_rows(staged,FIELDS,rows,['报告期'],stamp)
                changes[target]=staged.read_bytes()
            state=base/'采集状态.jsonl';old=state.read_bytes() if state.exists() else b''
            lines=old.decode().splitlines();seen=set(lines)
            pending=[]
            for record in report['结果']:
                encoded=json.dumps(record,ensure_ascii=False,allow_nan=False)
                if encoded not in seen:pending.append(encoded);seen.add(encoded)
            changes[state]=old+(b'\n' if old and not old.endswith(b'\n') and pending else b'')+''.join(x+'\n' for x in pending).encode()
    for code,text in captures.items():
        target=root/capture_path(stamp,code)
        if target.exists() and target.read_text()!=text:raise ValueError('同名探测HTML证据冲突')
        changes[target]=text.encode()
    summary={'来源运行':str(expected_run),'来源SHA':expected_sha,'产物SHA256':artifact_hash,
             '追加行':added,'采集报告':report,'探测单季记录':rows_by_code if report['模式']=='probe' else {}}
    changes[evidence]=(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    commit_files(root,changes)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--sha',required=True)
    args=parser.parse_args()
    print(json.dumps(publish(args.root,args.artifact,args.run_id,args.sha),ensure_ascii=False))


if __name__=='__main__':main()
