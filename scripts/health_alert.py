#!/usr/bin/env python3
"""健康检查/工作流故障共用 Issue 协议；真实联网仅在 Actions 内执行。"""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BJ = timezone(timedelta(hours=8))
MARKER = re.compile(r"<!-- rsan-health-v1 (\{[^\n]*\}) -->")
BAD = {"延迟", "失败", "部分失败"}


def metadata(issue):
    found = MARKER.search(issue.get("body") or "")
    if not found:
        return None
    try:
        value = json.loads(found[1])
        if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in ("namespace", "item", "date")):
            return None
        date.fromisoformat(value["date"])
        workflow = value.get("workflow")
        if workflow is not None and (not isinstance(workflow, dict)
                or not isinstance(workflow.get("file"), str)
                or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow["file"])
                or type(workflow.get("run_id")) is not int or workflow["run_id"] <= 0
                or type(workflow.get("attempt", 1)) is not int or workflow.get("attempt", 1) <= 0):
            return None
        return value
    except (ValueError, TypeError):
        return None


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("仓库名称须为 owner/repo")
        if not token:
            raise ValueError("缺少 GITHUB_TOKEN")
        self.repository, self.token = repository, token

    def request(self, method, path, payload=None):
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("GitHub 联网告警只能在 Actions 执行")
        url = f"https://api.github.com/repos/{self.repository}/" + path
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rsan-health-check"})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def ensure_label(self):
        try:
            self.request("GET", "labels/" + urllib.parse.quote("告警", safe=""))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            self.request("POST", "labels", {"name": "告警", "color": "B60205", "description": "定时产出或工作流异常，由健康检查自动维护"})

    def issues(self):
        rows, page = [], 1
        while True:
            query = urllib.parse.urlencode({"state": "all", "labels": "告警", "per_page": 100, "page": page})
            batch = self.request("GET", "issues?" + query)
            rows.extend(i for i in batch if "pull_request" not in i)
            if len(batch) < 100:
                return rows
            page += 1


def recovered_workflow(api, meta, cache, checked):
    workflow = meta.get("workflow")
    if not workflow:
        return True
    filename = workflow["file"].rsplit("/", 1)[-1]
    if filename not in cache:
        path = "actions/workflows/" + urllib.parse.quote(filename, safe="") + "/runs?" + urllib.parse.urlencode({"branch": "main", "per_page": 30})
        runs = api.request("GET", path).get("workflow_runs", [])
        completed = []
        for run in runs:
            if run.get("status") != "completed":
                continue
            try:
                updated = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                if updated.tzinfo is not None and updated <= checked:
                    completed.append((updated, run))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        cache[filename] = max(completed, key=lambda entry: (entry[0], entry[1].get("conclusion") != "success")) if completed else None
    last = cache[filename]
    failure = (int(workflow["run_id"]), int(workflow.get("attempt", 1)))
    try:
        failed_at = datetime.fromisoformat(workflow["failed_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    # 旧运行重跑后 ID 不会增加；必须按实际已完成运行的更新时间比较。
    return bool(last and failed_at.tzinfo is not None and last[0] > failed_at
                and last[1].get("conclusion") == "success"
                and (int(last[1]["id"]), int(last[1].get("run_attempt", 1))) != failure)


def sync_report(api, report, namespace="production", workflow_event=False):
    checked = datetime.fromisoformat(report["checked_at"].replace("Z", "+00:00"))
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=BJ)
    stamp = checked.astimezone(BJ).isoformat()
    today = checked.astimezone(BJ).date().isoformat()
    rows = report["items"]
    if not isinstance(rows, list) or any(i.get("status") not in BAD | {"正常"} for i in rows):
        raise ValueError("告警输入 items/status 无效")
    if len({i["id"] for i in rows}) != len(rows):
        raise ValueError("告警输入 item id 重复")
    api.ensure_label()
    issues, results, recovery_cache = api.issues(), [], {}
    present = {i["id"] for i in rows}

    def close(issue, why):
        api.request("POST", f"issues/{issue['number']}/comments", {"body": f"{stamp}（北京时间）恢复正常：{why}"})
        closed = api.request("PATCH", f"issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        issue["state"] = "closed"
        results.append({"item": metadata(issue)["item"], "action": "关闭", "number": issue["number"], "url": issue["html_url"], "state": closed.get("state"), "closed_at": closed.get("closed_at")})

    for item in rows:
        matches = [i for i in issues if (metadata(i) or {}).get("namespace") == namespace and metadata(i).get("item") == item["id"]]
        if item["status"] == "正常":
            for issue in matches:
                if issue["state"] == "open" and recovered_workflow(api, metadata(issue), recovery_cache, checked):
                    close(issue, "产出已符合预期；工作流故障另核对到后续成功运行" if metadata(issue).get("workflow") else "产出已符合预期")
            continue
        same_day = [i for i in matches if metadata(i)["date"] == today]
        if len(same_day) > 1:
            raise RuntimeError(f"{item['id']} 同日已有多个告警，拒绝再创建")
        issue = same_day[0] if same_day else None
        meta = {"namespace": namespace, "item": item["id"], "date": today}
        if issue and metadata(issue).get("workflow"):
            meta["workflow"] = metadata(issue)["workflow"]
        if item.get("workflow"):
            meta["workflow"] = dict(item["workflow"], failed_at=item["workflow"].get("failed_at", stamp))
        marker = "<!-- rsan-health-v1 " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + " -->"
        details = json.dumps({"预期": item.get("expected", {}), "实际": item.get("actual", {}), "原因": item.get("reasons", [])}, ensure_ascii=False, indent=2)
        title = f"[告警] {today} {item['name']}：{item['status']}"
        body = f"{marker}\n\n检查时间：{stamp}（北京时间）\n\n项目：{item['name']}｜状态：{item['status']}\n\n```json\n{details}\n```\n\n规则：同日同项复用本 Issue，恢复后由健康检查关闭。"
        if issue:
            api.request("POST", f"issues/{issue['number']}/comments", {"body": f"{stamp}（北京时间）更新：{item['status']}\n\n```json\n{details}\n```"})
            action = "重新打开" if issue["state"] == "closed" else "追加评论"
            issue.update(api.request("PATCH", f"issues/{issue['number']}", {"title": title, "body": body, "state": "open"}))
        else:
            issue = api.request("POST", "issues", {"title": title, "body": body, "labels": ["告警"]})
            issues.append(issue)
            action = "创建"
        results.append({"item": item["id"], "action": action, "number": issue["number"], "url": issue["html_url"], "state": issue["state"]})
    # 手动脚本等无定时产出的工作流告警，也可由后续成功运行自动恢复。
    if not workflow_event:
        for issue in issues:
            meta = metadata(issue)
            if meta and meta["namespace"] == namespace and meta["item"].startswith("workflow:") and meta["item"] not in present and meta.get("workflow") and issue["state"] == "open" and recovered_workflow(api, meta, recovery_cache, checked):
                close(issue, "已核对到该工作流后续成功运行")
    return results


def failure_report():
    run_id = os.environ["GITHUB_RUN_ID"]
    repository = os.environ["GITHUB_REPOSITORY"]
    name = os.environ.get("HEALTH_ITEM_NAME", os.environ.get("GITHUB_WORKFLOW", "工作流"))
    filename = os.environ["HEALTH_WORKFLOW_FILE"]
    return {"checked_at": datetime.now(BJ).isoformat(), "items": [{
        "id": os.environ["HEALTH_ITEM_ID"], "name": name, "status": "失败", "expected": {"工作流": "成功"},
        "actual": {"run_url": f"https://github.com/{repository}/actions/runs/{run_id}"},
        "reasons": [f"工作流 {os.environ.get('GITHUB_WORKFLOW', filename)} 执行失败"],
        "workflow": {"file": filename, "run_id": int(run_id), "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))}}]}


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", type=Path)
    mode.add_argument("--workflow-failure", action="store_true")
    ap.add_argument("--namespace", default="production")
    ap.add_argument("--result-file", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    report = failure_report() if args.workflow_failure else json.loads(args.report.read_text(encoding="utf-8"))
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_TOKEN"))
    result = sync_report(api, report, args.namespace, workflow_event=args.workflow_failure)
    output = json.dumps({"checked_at": report["checked_at"], "events": result}, ensure_ascii=False, indent=2) + "\n"
    if args.result_file:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        args.result_file.write_text(output, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
