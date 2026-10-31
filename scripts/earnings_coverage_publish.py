#!/usr/bin/env python3
"""T13c 严格发布：校验探测与全部分片后，原子追加成分、上市日及缺失单季财报。"""
import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import tempfile

from earnings_coverage import LISTING_VERSION, read_members
from earnings_coverage_batch import probe_universe
from earnings_history import (FIELDS, RAW_FIELDS, SOURCE_CODE, SOURCE_VERSION, URL,
                              deadline, load_v3, rows_digest, validate_amount, visibility)
from earnings_history_transaction import commit_files
from flow_source import append_rows

ROOT = Path(__file__).resolve().parents[1]


def _stamp(value):
    moment = dt.datetime.fromisoformat(value)
    if moment.utcoffset() != dt.timedelta(hours=8):
        raise ValueError('采集时间必须为北京时间')
    return moment


def _probe(result, expected_run, expected_sha):
    universe, digest = probe_universe(result)
    if str(result.get('来源运行')) != str(expected_run) or result.get('来源SHA') != expected_sha:
        raise ValueError('探测运行/SHA 与发布请求不一致')
    _stamp(result['生成时间'])
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha) or not re.fullmatch(r'\d+', str(expected_run)):
        raise ValueError('探测身份格式无效')
    return universe, digest


def validate_batches(root, result, paths, expected_run, expected_sha):
    universe, digest = _probe(result, expected_run, expected_sha)
    expected_branch = 'gpt/T13c-earnings'; all_codes = []; listings = {}; financials = {}; rows = {}
    shards = None; seen_shards = set(); batch_run = batch_sha = None; num, _, _ = load_v3(root)
    probe_sha = hashlib.sha256((json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()).hexdigest()
    for path in paths:
        report = json.loads(Path(path).read_text())
        if report['来源分支'] != expected_branch or report['来源运行'] != str(report['来源运行']) or not re.fullmatch(r'\d+', report['来源运行']):
            raise ValueError('批次来源分支/运行身份无效')
        if not re.fullmatch(r'[0-9a-f]{40}', report['来源SHA']):
            raise ValueError('批次来源 SHA 无效')
        batch_run = report['来源运行'] if batch_run is None else batch_run
        batch_sha = report['来源SHA'] if batch_sha is None else batch_sha
        if report['来源运行'] != batch_run or report['来源SHA'] != batch_sha:
            raise ValueError('四个批次不是同一次工作流和同一提交')
        if report['探测运行'] != str(expected_run) or report['探测SHA'] != expected_sha or report['探测结果SHA256'] != probe_sha:
            raise ValueError('批次使用的成分探测身份或字节不一致')
        if report['成分业务SHA256'] != digest or report['成分家数'] != len(universe):
            raise ValueError('批次成分指纹或家数不一致')
        if report['财报实现依据'] != SOURCE_CODE or report['财报来源版本'] != SOURCE_VERSION:
            raise ValueError('批次财报解析依据不一致')
        shards = report['分片总数'] if shards is None else shards
        if report['分片总数'] != shards or report['分片'] in seen_shards or not 0 <= report['分片'] < shards:
            raise ValueError('批次分片集合无效')
        seen_shards.add(report['分片']); _stamp(report['生成时间'])
        codes = report['代码']; all_codes.extend(codes)
        expected_codes = [code for index, code in enumerate(universe) if index % shards == report['分片']]
        if codes != expected_codes:
            raise ValueError('批次代码不符合稳定分片规则')
        if len(codes) != len(set(codes)) or set(codes) != {r['代码'] for r in report['上市日期']} or set(codes) != {r['代码'] for r in report['财报']}:
            raise ValueError('批次代码、上市日和财报集合不一致')
        if report['失败数'] != sum(r['状态'] == '失败' for r in report['财报']) or report['失败数']:
            raise ValueError('存在财报失败，整次拒绝发布')
        if report['上市日未知数'] != sum(r['状态'] != '成功' for r in report['上市日期']):
            raise ValueError('上市日未知数与逐股记录不一致')
        for record in report['上市日期']:
            code = record['代码']
            if code in listings or record['实现版本'] != LISTING_VERSION:
                raise ValueError('上市日期重复或版本不一致')
            if _stamp(record['采集时间']) > _stamp(report['生成时间']):
                raise ValueError('断点上市日期观测晚于本批次')
            expected_source = 'https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/' + code + '.phtml'
            if record['来源'] != expected_source:
                raise ValueError('上市日期来源身份不一致')
            if record['状态'] == '成功':
                date = dt.date.fromisoformat(record['上市日期'])
                if date > _stamp(record['采集时间']).date() or not re.fullmatch(r'[0-9a-f]{64}', record['响应SHA256']):
                    raise ValueError('上市日期成功记录缺证据或日期越界')
                if not record.get('原始日期') or not record.get('来源字段'):
                    raise ValueError('上市日期成功记录缺原始字段')
            elif record['状态'] != '未知，不进入分母' or record.get('上市日期'):
                raise ValueError('上市日期状态不明确')
            listings[code] = record
        for record in report['财报']:
            code = record['代码']
            if code in financials or record['来源版本'] != SOURCE_VERSION:
                raise ValueError('财报记录重复或版本不一致')
            if _stamp(record['采集时间']) > _stamp(report['生成时间']):
                raise ValueError('断点财报观测晚于本批次')
            status = record['状态']
            if status not in ('成功', '无2013起历史', '复用已发布原始财报'):
                raise ValueError('财报批次存在未接受状态')
            payload = report['单季记录'].get(code)
            if status == '复用已发布原始财报':
                if payload is not None or not (Path(root) / 'data/财报/全市场单季' / (code + '.csv')).exists():
                    raise ValueError('复用财报缺正式缓存或携带新载荷')
            else:
                if record.get('来源') != URL.format(code=code) or not re.fullmatch(r'[0-9a-f]{64}', record.get('响应SHA256', '')):
                    raise ValueError('新抓财报来源或响应指纹不一致')
                if not isinstance(payload, list) or (status == '成功') != bool(payload) or record['载荷SHA256'] != rows_digest(payload):
                    raise ValueError('财报状态、载荷或指纹不一致')
                periods = []
                for row in payload:
                    if set(row) != set(RAW_FIELDS) or any(not isinstance(v, str) for v in row.values()):
                        raise ValueError('单季财报字段或原始类型不完整')
                    period = row['报告期']; periods.append(period)
                    if period < '2013-01-01' or row['法定截止'] != deadline(period).isoformat() or row['可见时刻'] != visibility(period) or row['来源'] != URL.format(code=code):
                        raise ValueError('单季财报期、可见时点或来源不一致')
                    for key in ('扣非净利润', '净利润', '营业总收入'):
                        validate_amount(row[key], num)
                if periods != sorted(set(periods)):
                    raise ValueError('单季财报报告期重复或乱序')
                rows[code] = payload
            financials[code] = record
        expected_payloads = {r['代码'] for r in report['财报'] if r['状态'] != '复用已发布原始财报'}
        if set(report['单季记录']) != expected_payloads:
            raise ValueError('批次单季载荷集合与新抓财报不一致')
    if shards is None or seen_shards != set(range(shards)) or sorted(all_codes) != universe or len(all_codes) != len(set(all_codes)):
        raise ValueError('批次未无重无漏覆盖完整成分')
    return universe, digest, listings, financials, rows


def publish(root, probe_path, batch_paths, expected_run, expected_sha):
    root = Path(root).resolve(); probe_path = Path(probe_path)
    probe_bytes = probe_path.read_bytes(); result = json.loads(probe_bytes)
    universe, digest, listings, financials, rows = validate_batches(root, result, batch_paths, expected_run, expected_sha)
    stamp = result['生成时间']; safe = stamp.replace(':', '').replace('+', '_')
    snapshot = root / 'data/财报/成分快照' / (safe + '.json')
    changes = {snapshot: probe_bytes}

    # 上市日期是原始事实账本，只追加本次观测；精确同记录重跑不重复。
    ledger = root / 'data/财报/上市日期.jsonl'; old = ledger.read_bytes() if ledger.exists() else b''
    existing = set(old.decode().splitlines()); pending = []
    keep = ('代码', '状态', '上市日期', '原始日期', '来源字段', '来源', '来源片段',
            '实现版本', '响应SHA256', '响应字节', '采集时间', '错误')
    for code in universe:
        record = {key: listings[code][key] for key in keep if key in listings[code]}
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if encoded not in existing:
            existing.add(encoded); pending.append(encoded)
    changes[ledger] = old + (b'\n' if old and not old.endswith(b'\n') and pending else b'') + ''.join(x + '\n' for x in pending).encode()

    base = root / 'data/财报/全市场单季'; added = {}
    with tempfile.TemporaryDirectory(prefix='.t13c-plan-', dir=root) as directory:
        stage = Path(directory)
        for code, payload in rows.items():
            target = base / (code + '.csv'); staged = stage / (code + '.csv')
            if target.exists(): staged.write_bytes(target.read_bytes())
            added[code] = append_rows(staged, FIELDS, payload, ['报告期'], financials[code]['采集时间'])
            changes[target] = staged.read_bytes()
    state = base / '采集状态.jsonl'; old_state = state.read_bytes() if state.exists() else b''
    known = set(old_state.decode().splitlines()); state_pending = []
    for code in universe:
        record = financials[code]
        if record['状态'] == '复用已发布原始财报':
            continue
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if encoded not in known: known.add(encoded); state_pending.append(encoded)
    changes[state] = old_state + (b'\n' if old_state and not old_state.endswith(b'\n') and state_pending else b'') + ''.join(x + '\n' for x in state_pending).encode()

    evidence = {'生成时间': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
                '探测运行': str(expected_run), '探测SHA': expected_sha,
                '探测产物SHA256': hashlib.sha256(probe_bytes).hexdigest(), '成分业务SHA256': digest,
                '候选行业': 27, '独立股票': len(universe), '上市日成功': sum(r['状态'] == '成功' for r in listings.values()),
                '上市日未知': sum(r['状态'] != '成功' for r in listings.values()),
                '新抓财报股票': len(rows), '复用财报股票': sum(r['状态'] == '复用已发布原始财报' for r in financials.values()),
                '财报追加行': sum(added.values()), '状态': '严格预检通过并原子晋升',
                '说明': '当前成分快照回溯有幸存者偏差；上市日未知不进入分母；未使用指数计入日、首条行情或首条财报推测IPO。'}
    evidence_path = root / 'logs/test/T13c' / ('正式发布_' + str(expected_run) + '.json')
    changes[evidence_path] = (json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    commit_files(root, changes)
    # 晋升后再走生产读取器复核 27 行业和上市账本结构。
    loaded = read_members(root)
    if len(loaded) != 27:
        raise AssertionError('晋升后的成分读取器不是 27 行业')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT); parser.add_argument('--probe-result', type=Path, required=True)
    parser.add_argument('--batch-dir', type=Path, required=True); parser.add_argument('--probe-run', required=True); parser.add_argument('--probe-sha', required=True)
    args = parser.parse_args(); paths = sorted(args.batch_dir.rglob('*.json'))
    print(json.dumps(publish(args.root, args.probe_result, paths, args.probe_run, args.probe_sha), ensure_ascii=False))


if __name__ == '__main__':
    main()
