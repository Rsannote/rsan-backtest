"""T16 v2 看板数据：用仓库现有数据跑一遍，检查各个问题的数据齐全、可解析、体积小，页面不依赖外部脚本。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_site  # noqa: E402


class BuildSiteTest(unittest.TestCase):
    def test_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "market.json").write_text("{}", encoding="utf-8")       # v1 旧文件应被清掉
            subprocess.run([sys.executable, str(ROOT / "scripts/build_site.py"), "--out", str(out)], check=True)
            self.assertEqual({p.name for p in out.glob("*.json")}, {"dashboard.json"})
            text = (out / "dashboard.json").read_text(encoding="utf-8")
            self.assertNotIn("NaN", text)
            self.assertLess(len(text.encode()), 200 * 1024)
            d = json.loads(text)
            self.assertEqual(d["错误"], [], d["错误"])
            for key in ("大盘", "资金", "主线", "历史", "海外"):
                self.assertIn("过期", d[key], key)
            self.assertIn(d["大盘"]["结论"], ("攻", "守", "中性"))
            self.assertEqual(len(d["资金"]["在追"]), 5)
            self.assertLessEqual(len(d["历史"]["最像"]), 3)
            self.assertIn("近两周到期", d["盯"])

    def test_panorama_readings_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "panorama").mkdir()
            (root / "panorama/2026-10-30.md").write_text(
                "# 每周市场全景 · 2026-10-30\n\n**Claude 小结（11-01）**：大盘守。\n\n## 3. 两层对照\n\n像在哪、不像在哪（Claude 判读，11-01）：程序找出的。\n\n"
                "- **像的地方**：A。\n\n小结：像下跌后段。\n\n要盯：温度跌破 20。\n\n来源：x\n\n## 6. 主线雷达\n\n和历史哪次最像、要盯什么：【待判读】\n\n来源：y\n",
                encoding="utf-8")
            j = build_site.panorama(root)
            self.assertEqual(j["小结"], "大盘守。")
            self.assertIn("像的地方", j["历史判读"])
            self.assertNotIn("要盯", j["历史判读"])          # 要盯 挪到"这周盯什么"
            self.assertEqual(j["要盯"], ["要盯：温度跌破 20。"])
            self.assertIsNone(j["主线判读"])                 # 还没写判读就是空
            self.assertTrue(j["链接"].endswith("panorama/2026-10-30.md"))

    def test_page(self):
        html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertNotIn('<script src="http', html)
        self.assertIn("不构成投资建议", html)
        self.assertIn("data/dashboard.json", html)
        for q in ("钱在往哪走？", "有没有主线？", "现在像历史上哪一次？", "海外怎么样？", "这周盯什么？"):
            self.assertIn(q, html)


if __name__ == "__main__":
    unittest.main()
