"""T10 Reddit 版块抓取：离线解析、去重、精华分组、失败容错、健康检查登记。"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_reddit as fr  # noqa: E402
import health_check as health  # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
FEED = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_a</id><title>Claude &amp; skills tips</title><link href="https://www.reddit.com/r/ClaudeAI/a/"/>
<author><name>/u/x</name></author><published>2026-10-30T10:00:00+00:00</published>
<content type="html">&lt;p&gt;Useful prompt tricks&lt;/p&gt; submitted by /u/x [link] [comments]</content></entry>
<entry><id>t3_b</id><title>Second</title><link href="https://www.reddit.com/r/ClaudeAI/b/"/><updated>2026-10-29T10:00:00+00:00</updated></entry>
</feed>"""


def post(i, rank):
    return {"id": f"t3_{i}", "title": f"帖子{i}", "link": f"https://r/{i}", "author": "u", "published": "", "snippet": "", "rank": rank}


class RedditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        wl = {"平台": {"Reddit": {"条目": [
            {"名称": "ClaudeAI", "地址": "r/ClaudeAI", "目的": [2], "状态": "可抓"},
            {"名称": "wallstreetbets", "地址": "r/wallstreetbets", "目的": [1], "状态": "可抓"},
            {"名称": "已停", "地址": "r/dead", "目的": [4], "状态": "抓不到"}]}}}
        (self.root / "system").mkdir()
        (self.root / "system/信息源白名单.json").write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
        self.now = dt.datetime(2026, 11, 1, 7, 30, tzinfo=BJ)

    def test_parse_atom_and_reject_html(self):
        posts = fr.parse_feed(FEED)
        self.assertEqual([p["id"] for p in posts], ["t3_a", "t3_b"])
        self.assertEqual(posts[0]["title"], "Claude & skills tips")
        self.assertEqual(posts[0]["snippet"], "Useful prompt tricks")
        self.assertEqual(posts[1]["published"], "2026-10-29T10:00:00+00:00")
        with self.assertRaises(ValueError):
            fr.parse_feed(b"<html><body>verify you are human</body></html>")

    def test_whitelist_skips_unfetchable(self):
        self.assertEqual([s["版块"] for s in fr.subreddits(self.root)], ["ClaudeAI", "wallstreetbets"])

    def test_dedupe_digest_and_partial_failure(self):
        calls = []

        def fetcher(sub, wait):
            calls.append(sub)
            if sub == "wallstreetbets":
                raise RuntimeError("HTTP 429")
            return [post("a", 1), post("b", 2)]

        self.assertEqual(fr.run(self.root, self.root, 0, self.now, fetcher), 0)
        base = self.root / "data/信息源/reddit"
        rows = [json.loads(l) for l in (base / "2026-11/2026-11-01.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in rows], ["t3_a", "t3_b"])
        self.assertTrue(all(r["来源级别"] == "三级" for r in rows))
        status = json.loads((base / "状态.json").read_text(encoding="utf-8"))
        self.assertEqual(status["wallstreetbets"]["连续失败"], 1)
        self.assertIn("last_success_at", status)
        md = (self.root / "data/信息源/精华_最新.md").read_text(encoding="utf-8")
        self.assertIn("## 目的2：AI用法", md)
        self.assertIn("三级信息源", md)
        self.assertIn("r/wallstreetbets（HTTP 429）", md)
        # 第二次运行：已见的不再写入，只记新帖
        second = {"ClaudeAI": [post("b", 1), post("c", 2)], "wallstreetbets": [post("w", 1)]}
        fr.run(self.root, self.root, 0, self.now + dt.timedelta(days=1), lambda s, w: second[s])
        rows2 = [json.loads(l) for l in (base / "2026-11/2026-11-02.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["版块"], r["id"]) for r in rows2], [("ClaudeAI", "t3_c"), ("wallstreetbets", "t3_w")])
        self.assertEqual(json.loads((base / "状态.json").read_text(encoding="utf-8"))["wallstreetbets"]["连续失败"], 0)

    def test_all_failed_returns_error_and_keeps_last_success(self):
        fr.run(self.root, self.root, 0, self.now, lambda s, w: [post("a", 1)])
        first = json.loads((self.root / "data/信息源/reddit/状态.json").read_text(encoding="utf-8"))["last_success_at"]

        def boom(sub, wait):
            raise RuntimeError("HTTP 403")
        self.assertEqual(fr.run(self.root, self.root, 0, self.now + dt.timedelta(days=1), boom), 1)
        status = json.loads((self.root / "data/信息源/reddit/状态.json").read_text(encoding="utf-8"))
        self.assertEqual(status["last_success_at"], first)

    def test_per_purpose_cap(self):
        rows = [{"目的": [2], "本周排名": i, "版块": "x", "标题": f"t{i}", "链接": "l", "节选": ""} for i in range(1, 25)]
        md = fr.digest(rows, self.now, {})
        self.assertEqual(md.count("- ["), fr.PER_PURPOSE)

    def test_health_check_registration(self):
        manifest = json.loads((ROOT / "system/运行清单.json").read_text(encoding="utf-8"))
        item = next(i for i in manifest["items"] if i["id"] == "reddit")
        fr.run(self.root, self.root, 0, self.now, lambda s, w: [post("a", 1)])
        cal = health.Calendar(self.root, {"history": "missing", "closures": "missing"})
        ok = health.check_item(self.root, item, health.timestamp("2026-11-02T08:20:00+08:00"), cal)
        self.assertEqual(ok["status"], "正常", ok)
        late = health.check_item(self.root, item, health.timestamp("2026-11-03T22:30:00+08:00"), cal)
        self.assertEqual(late["status"], "延迟", late)

    def test_workflow(self):
        s = (ROOT / ".github/workflows/reddit.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/fetch_reddit.py", s)
        self.assertIn("timeout-minutes: 20", s)


if __name__ == "__main__":
    unittest.main()
