#!/usr/bin/env python3
"""T5 验收：用真实仓库数据生成指定周的全景（隔离到 logs/test/T5/），再从源文件独立取数逐项核对。

用法：python3 tests/verify_t5_panorama.py 2026-10-23
结果写 logs/test/T5/核对_<截止日>.md；有不一致时退出码 1。
"""
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import panorama  # noqa: E402


def main(day: str) -> int:
    w = panorama.Week(ROOT, dt.date.fromisoformat(day))
    last = w.last.isoformat()
    subprocess.run([sys.executable, str(ROOT / "scripts/panorama.py"), "--week", day, "--out-dir", "logs/test/T5"], check=True)
    text = (ROOT / f"logs/test/T5/{last}.md").read_text(encoding="utf-8")
    checks = []

    def expect(name, snippet):
        checks.append((name, snippet, snippet in text))

    # 1 温度
    temps = {r["date"]: r for r in csv.DictReader((ROOT / "data/社区情绪/温度计.csv").open(encoding="utf-8-sig"))}
    t = float(temps[last]["温度"])
    expect(f"温度 {last}", f"情绪温度 {t:.0f}（{panorama.zone(t)}）")
    # 2 大盘每日结论与沪深300涨跌
    for d in w.days:
        p = ROOT / "data/市场状态" / d.strftime("%Y-%m") / f"{d.isoformat()}.json"
        if p.exists():
            m = json.loads(p.read_text(encoding="utf-8"))["市场"]
            expect(f"大盘 {d}", f"|{d.isoformat()}|{m['结论']}|{m.get('冷热', '—')}|{m.get('总分', '—')}|{m['沪深300']['chg']:.2f}%|")
        else:
            expect(f"大盘 {d}", f"|{d.isoformat()}|无存档|")
    # 4 资金：每行业该日最后一条修订
    rows = {}
    for r in csv.DictReader((ROOT / "data/资金轮动/日表.csv").open(encoding="utf-8-sig")):
        if r["date"] == last and r["占比5日变化_31"]:
            rows[r["code"]] = r
    ranked = sorted(rows.values(), key=lambda r: float(r["占比5日变化_31"]), reverse=True)
    for r in ranked[:5] + ranked[-5:]:
        expect(f"资金 {r['name']}", f"|{r['name']}|{float(r['占比_31']) * 100:.2f}%|{float(r['占比5日变化_31']) * 100:+.2f}个百分点|")
    # 6 主线阶段
    stages = {}
    for r in csv.DictReader((ROOT / "data/主线/阶段日表_31.csv").open(encoding="utf-8")):
        if r["date"] == last:
            stages[r["code"]] = r
    for s in panorama.STAGES:
        n = sum(1 for r in stages.values() if r["阶段"] == s)
        expect(f"阶段 {s}", f"- **{s}**（{n}）")
    # 9 到期复核条数
    js = panorama.load_judgments(ROOT, w.sunday)
    due = [r for r in js if w.monday.isoformat() <= r["review_date"] <= w.sunday.isoformat()]
    expect("本周到期条数", f"本周到期 {len(due)} 条")
    expect("判断总数", f"判断共 {len(js)} 条")

    bad = [c for c in checks if not c[2]]
    lines = [f"# T5 验收核对 · {last}", "", f"全景：`logs/test/T5/{last}.md`；核对 {len(checks)} 项，不一致 {len(bad)} 项。", "",
             "|项|期望片段（由源文件独立取数）|结果|", "|---|---|---|"]
    lines += [f"|{n}|`{s}`|{'一致' if ok else '不一致'}|" for n, s, ok in checks]
    out = ROOT / f"logs/test/T5/核对_{last}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"核对 {len(checks)} 项，不一致 {len(bad)} 项 → {out.relative_to(ROOT)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "2026-10-23"))
