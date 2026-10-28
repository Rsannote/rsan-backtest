#!/usr/bin/env python3
"""从上交所已验证的年度休市公告生成日历补充；网络刷新只在 Actions 执行。"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
BJ = timezone(timedelta(hours=8))
HOLIDAYS = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_notice(raw, year):
    parser = VisibleText()
    parser.feed(raw.decode("utf-8"))
    text = " ".join(" ".join(parser.parts).split())
    annual = re.search(r"(20\d{2})年休市安排(.*?)(?:相关公告|相关链接|$)", text)
    if not annual or int(annual[1]) != year:
        raise ValueError(f"未取得 {year} 年完整年度休市公告，不推测交易日")
    sections = list(re.finditer("(" + "|".join(HOLIDAYS) + r")\s*[：:]", annual[2]))
    if [m[1] for m in sections] != list(HOLIDAYS):
        raise ValueError("年度公告未包含全部七类假日，拒绝写入")
    ranges, closed = [], set()
    for i, section in enumerate(sections):
        body = annual[2][section.end():sections[i + 1].start() if i + 1 < len(sections) else None]
        span = re.search(r"(\d{1,2})月(\d{1,2})日\s*[（(][^）)]*[）)]\s*至\s*(?:(\d{1,2})月)?(\d{1,2})日\s*[（(][^）)]*[）)]\s*休市", body)
        if span:
            start = date(year, int(span[1]), int(span[2]))
            end = date(year, int(span[3] or span[1]), int(span[4]))
        else:
            one = re.search(r"(\d{1,2})月(\d{1,2})日\s*[（(][^）)]*[）)]\s*休市", body)
            if not one or "至" in body[:body.find("休市")]:
                raise ValueError(f"无法解析 {section[1]} 的完整休市区间")
            start = end = date(year, int(one[1]), int(one[2]))
        if end < start:
            raise ValueError(f"{section[1]} 的日期区间倒置")
        ranges.append({"名称": section[1], "开始": start.isoformat(), "结束": end.isoformat()})
        closed.update((start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1))
    return {"schema_version": 1, "coverage_start": f"{year}-01-01", "coverage_end": f"{year}-12-31",
            "closed_dates": sorted(closed), "holiday_ranges": ranges,
            "说明": "历史交易日先用沪深300日线；覆盖区间内工作日按本官方全年休市表判定，周末始终休市。"}


def valid_cache(path, year):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data["coverage_start"] == f"{year}-01-01" and data["coverage_end"] == f"{year}-12-31"
                and bool(data["sources"]) and len(data["holiday_ranges"]) == len(HOLIDAYS)
                and all(date.fromisoformat(d).year == year for d in data["closed_dates"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--year", type=int, default=datetime.now(BJ).year)
    ap.add_argument("--source", type=Path, help="离线重放已保存的 Actions 官方响应")
    ap.add_argument("--captured-at", help="离线响应原始抓取时刻")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out = args.root / "system/交易所休市.json"
    if not args.source and not args.force and valid_cache(out, args.year):
        print(f"沿用已核验的 {args.year} 年上交所全年休市表")
        return 0
    try:
        captured = args.captured_at
        if args.source:
            raw = args.source.read_bytes()
        else:
            if os.environ.get("GITHUB_ACTIONS") != "true":
                raise RuntimeError("网络刷新只能在 GitHub Actions 执行；本机请用 --source 离线重放")
            for attempt in range(3):
                try:
                    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"})
                    with urllib.request.urlopen(req, timeout=25) as response:
                        raw = response.read()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 1))
            captured = datetime.now(BJ).isoformat()
        data = parse_notice(raw, args.year)
        data["sources"] = [{"url": URL, "captured_at": captured, "sha256": hashlib.sha256(raw).hexdigest(),
                            "snapshot": str(args.source) if args.source else "运行时官网响应"}]
        data["generated_at"] = datetime.now(BJ).isoformat()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"已生成 {args.year} 年官方休市表，{len(data['closed_dates'])} 个公告休市日期；来源 {URL}")
        return 0
    except Exception as exc:
        if not args.source and valid_cache(out, args.year):
            print(f"日历刷新失败，保留已验证全年缓存：{type(exc).__name__}: {exc}")
            return 0
        print(f"日历失败，未写入：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
