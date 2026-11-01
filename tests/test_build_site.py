"""T16 看板数据生成：用仓库现有数据跑一遍，检查栏目齐全、可解析、体积小。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BuildSiteTest(unittest.TestCase):
    def test_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            subprocess.run([sys.executable, str(ROOT / 'scripts/build_site.py'), '--out', str(out)], check=True)
            names = {'market', 'mainline', 'flow', 'history', 'macro', 'judgments', 'health', 'meta'}
            self.assertEqual(names, {p.stem for p in out.glob('*.json')})
            meta = json.loads((out / 'meta.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['errors'], [], meta['errors'])
            for name in ('market', 'mainline', 'flow', 'history', 'macro'):
                data = json.loads((out / f'{name}.json').read_text(encoding='utf-8'))
                self.assertIn('数据日期', data)
                self.assertIn('过期', data)
            total = sum(p.stat().st_size for p in out.glob('*.json'))
            self.assertLess(total, 2 * 1024 * 1024)
            self.assertNotIn('NaN', (out / 'flow.json').read_text(encoding='utf-8'))

    def test_page_has_no_external_deps(self):
        html = (ROOT / 'site/index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script src="http', html)
        self.assertIn('不构成投资建议', html)


if __name__ == '__main__':
    unittest.main()
