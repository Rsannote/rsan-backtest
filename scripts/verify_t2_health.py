#!/usr/bin/env python3
"""仅在 Actions 用隔离模拟产出演练真实 Issue 开启、去重与恢复关闭。"""
import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile

import health_alert
import health_check

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs/test/T2"


def fixture(root, item):
    for name in ("system", "data/行情库/指数", "data/市场状态"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "system/交易所休市.json", root / "system/交易所休市.json")
    shutil.copyfile(ROOT / "data/行情库/指数/sh000300.csv", root / "data/行情库/指数/sh000300.csv")
    source = json.loads((ROOT / "system/运行清单.json").read_text(encoding="utf-8"))
    source["items"] = [item]
    (root / "system/运行清单.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")


def output(root, date, generated):
    data = {"数据日期": date, "生成时间": generated, "数据告警": []}
    (root / "data/市场状态/最新.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (root / "data/市场状态/最新.md").write_text("模拟产出，仅供 T2 告警演练，不是行情或判断。\n", encoding="utf-8")


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "Issue 实测只能在 Actions 执行"
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(health_check.BJ)
    api = health_alert.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    # 首轮验收发现标签列表存在读写延迟，留下两条同日测试告警。
    # 只清理本演练命名空间：真实健康恢复关闭全部，再将多余旧标记归档；保留正文和评论审计。
    prior = sorted((i for i in api.issues() if (health_alert.metadata(i) or {}).get("namespace") == "test"
                    and health_alert.metadata(i).get("item") == "t2-alert-drill"
                    and health_alert.metadata(i).get("date") == now.date().isoformat()), key=lambda i: i["number"])
    cleanup = []
    if len(prior) > 1:
        cleanup = health_alert.sync_report(api, {"checked_at": now.isoformat(), "items": [
            {"id": "t2-alert-drill", "name": "T2 健康检查演练（模拟）", "status": "正常"}]}, "test")
        for old in prior[1:]:
            meta = health_alert.metadata(old)
            meta["namespace"] = "test-failed-validation"
            marker = "<!-- rsan-health-v1 " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + " -->"
            api.request("POST", f"issues/{old['number']}/comments", {"body":
                f"首轮验收发现标签列表读写延迟导致重复，现保留此失败记录并归档测试标记；后续演练复用 #{prior[0]['number']}。未触及生产告警。"})
            api.request("PATCH", f"issues/{old['number']}", {"body": health_alert.MARKER.sub(lambda _: marker, old["body"], count=1)})
            archived = api.request("GET", f"issues/{old['number']}")
            assert archived["state"] == "closed" and health_alert.metadata(archived)["namespace"] == "test-failed-validation", archived
    original = json.loads((ROOT / "system/运行清单.json").read_text(encoding="utf-8"))
    item = copy.deepcopy(next(i for i in original["items"] if i["id"] == "market_state"))
    item.update(id="t2-alert-drill", name="T2 健康检查演练（模拟）", times=["00:00"], grace_minutes=0, trading_days_only=False, check_mode=None)
    with tempfile.TemporaryDirectory(prefix="T2-drill-") as temp:
        root = Path(temp)
        fixture(root, item)
        output(root, (now.date() - timedelta(days=1)).isoformat(), (now - timedelta(days=1)).isoformat())
        failed = health_check.check(root, now)
        assert failed["items"][0]["status"] == "延迟", failed
        opened = health_alert.sync_report(api, failed, "test")
        number = opened[0]["number"]
        assert api.request("GET", f"issues/{number}")["state"] == "open"
        # 新建客户端模拟下次独立定时进程，不能依赖本进程写入缓存才去重成功。
        api = health_alert.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        repeated = health_alert.sync_report(api, failed, "test")
        assert repeated[0]["number"] == number and repeated[0]["action"] == "追加评论", repeated
        output(root, now.date().isoformat(), now.isoformat())
        healthy = health_check.check(root, now)
        assert healthy["items"][0]["status"] == "正常", healthy
        recovered = health_alert.sync_report(api, healthy, "test")
        final = api.request("GET", f"issues/{number}")
        assert final["state"] == "closed" and final["closed_at"], final
        proof = {"说明": "隔离目录模拟旧产出→恢复；只操作 test 命名空间 Issue，未修改正式数据。",
                 "检查时间": now.isoformat(), "代码": os.environ["GITHUB_SHA"], "Issue": final["html_url"],
                 "首轮失败清理": cleanup, "异常报告": failed, "开启": opened, "同日重复": repeated, "恢复报告": healthy,
                 "关闭": recovered, "最终状态": final["state"], "关闭时间": health_check.timestamp(final["closed_at"]).isoformat()}
        (OUT / "告警开关实测.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT / "告警开关实测.md").write_text(f"# T2 告警生命周期实测\n\n时间：{now.isoformat()}（北京时间）。\n\n[测试 Issue #{number}]({final['html_url']})：旧产出为延迟后开启（首轮创建，本轮复用重开），同日重复复用 #{number} 并追加评论；恢复正常后自动关闭。\n\n最终状态：{final['state']}；关闭时间：{proof['关闭时间']}（北京时间）。\n\n首轮验收的重复 Issue #4 已由健康恢复关闭，保留失败记录并归档测试标记；本轮复用 #3 核验修复。正式数据未写入；测试命名空间与生产隔离。详细请求结果见同名 JSON。\n", encoding="utf-8")
        calendar_item = copy.deepcopy(next(i for i in original["items"] if i["id"] == "market_state"))
        calendar_item.update(check_mode=None, grace_minutes=110)  # 隔离的旧时段规则夹具
        fixture(root, calendar_item)
        cases = []
        for checked, produced, expected in (("2026-10-01T22:30:00+08:00", "2026-09-30", "正常"),
                                             ("2026-10-31T22:30:00+08:00", "2026-10-30", "正常"),
                                             ("2026-10-28T22:30:00+08:00", "2026-10-27", "延迟")):
            output(root, produced, produced + "T18:31:00+08:00")
            result = health_check.check(root, health_check.timestamp(checked))["items"][0]
            assert result["status"] == expected, result
            cases.append({"模拟检查时刻": checked, "模拟产出日期": produced, "期望状态": expected, "实际": result})
        (OUT / "周末节假日核验.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"告警演练通过：{final['html_url']}；同日重复复用；恢复后 closed；周末/国庆/缺今日覆盖三种检查通过")


if __name__ == "__main__":
    main()
