"""仅Actions：重取已定位的一手发布PDF，记录可达性/哈希，不把可达性当文本核验。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from auto_judgment_common import BJ
from mainline_cases_data import dump

ALLOWED = {'static.cninfo.com.cn', 'www.orient-fund.com', 'www.cmfchina.com', 'www.sse.com.cn'}


def verify(root=ROOT):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('财经公开文件重取只能在GitHub Actions，禁止本机联网')
    root = Path(root)
    metadata = json.loads((root / 'logs/test/T14/公开记录出处.json').read_text())
    output = root / 'logs/test/T14/公开文件'
    output.mkdir(parents=True, exist_ok=True)
    receipts = []
    for group, entry in metadata.items():
        if not isinstance(entry, dict):
            continue
        for source in entry['来源']:
            receipt = {'组': group, **source, '重取时间': dt.datetime.now(BJ).isoformat()}
            try:
                url = source['URL']
                if urlparse(url).scheme != 'https' or urlparse(url).hostname not in ALLOWED:
                    raise ValueError('非本次已核验一级发布域名')
                response = requests.get(url, timeout=(15, 60), headers={'User-Agent': 'Mozilla/5.0'})
                response.raise_for_status()
                if urlparse(response.url).hostname not in ALLOWED:
                    raise ValueError('重定向至未核验域名')
                if not response.content.lstrip().startswith(b'%PDF-'):
                    raise ValueError('响应不是PDF，可能被验证码或拒绝页面替代')
                filename = f'{len(receipts) + 1:02d}.pdf'
                (output / filename).write_bytes(response.content)
                receipt.update({'状态': '已重取', '字节数': len(response.content),
                                'SHA256': hashlib.sha256(response.content).hexdigest(),
                                '最终URL': response.url, '本地文件': str((output / filename).relative_to(root)),
                                '边界': '本步骤只证实发布文件可达；页码/叙事核验见独立记录，不把PDF下载成功冒充全文支持草案'})
            except (requests.RequestException, ValueError) as exc:
                receipt.update({'状态': '重取失败', '原因': str(exc)})
            receipts.append(receipt)
    dump(root / 'logs/test/T14/公开文件重取回执.json', receipts)
    failed = [r for r in receipts if r['状态'] != '已重取']
    print(f'一手公开文件重取：{len(receipts) - len(failed)}/{len(receipts)}，可达性不替代文本/日度触发核验')
    if failed:
        raise RuntimeError('部分一手文件未能重取，详见回执；不改参数或删除反证')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    verify(parser.parse_args().root)
