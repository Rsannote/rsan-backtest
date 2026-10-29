#!/usr/bin/env python3
"""按固定交易日窗口离线复核；无check只列人工清单，缺行情不改判断。"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path
from auto_judgment_common import (BJ, TradingCalendar, evaluate, isolated, load_documents,
                                  now_bj, persist, save_report)

ROOT=Path(__file__).resolve().parents[1]


def run(root=ROOT, day=None, directory=None, out_dir=None, now=None):
    root=Path(root).resolve(); now=now or now_bj()
    if now.tzinfo is None:
        raise ValueError("运行时间必须带时区")
    now=now.astimezone(BJ)
    calendar=TradingCalendar(root)
    as_of=dt.date.fromisoformat(day) if day else min(calendar.days[-1],now.date())
    if as_of>now.date():
        raise ValueError("复核日不能晚于北京时间今天")
    if directory is None:
        directory=root/"judgments"
        if out_dir:
            raise ValueError("正式复核日志目录固定为logs")
        output=root/"logs"
    else:
        directory=isolated(root,directory)
        output=isolated(root,out_dir or directory.parent)
    report={"title":"程序判断自动复核", "generated_at":now.isoformat(), "data_date":as_of.isoformat(),
            "status":"正常", "summary":[], "errors":[], "manual":[], "pending":[], "skipped":[]}
    _,ids,_=load_documents(directory)
    reviews={}
    if not calendar.is_open(as_of):
        report["skipped"].append("复核日期非交易日，不更新档案")
    else:
        for _,_,record in ids.values():
            if record["status"]!="待验证":
                continue
            if record["review_date"]>as_of.isoformat():
                continue
            if "result" in record:
                report["manual"].append(record["id"]+"：待验证但已有result，需人工确认，不覆盖已有结果 【待判读】")
                continue
            if "check" not in record:
                report["manual"].append(record["id"]+" "+record["claim"]+" 【待判读】")
                continue
            try:
                outcome=evaluate(root,record,as_of,calendar)
                if "pending" in outcome:
                    report["pending"].append(record["id"]+"："+outcome["pending"])
                else:
                    outcome["result"]["date"]=now.date().isoformat()
                    reviews[record["id"]]=outcome["result"]
            except (ValueError,KeyError,OSError,TypeError) as exc:
                report["errors"].append(record["id"]+"："+str(exc))
        # 格式/窗口错误时整批不写；缺行情只是待验证，其他完整条目可复核。
        if report["errors"]:
            report["status"]="失败"
        else:
            persist(directory,reviews=reviews)
    report["summary"].append(f"核对{len(ids)}条；复核写入{0 if report['errors'] else len(reviews)}条；待人工{len(report['manual'])}条；待数据{len(report['pending'])}条。")
    report["reviewed"]=[{"id":identifier,"status":result["status"],"note":result["note"]} for identifier,result in reviews.items()] if not report["errors"] else []
    save_report(output,"自动复核_最新",report)
    print("自动复核："+report["summary"][0]+" 状态："+report["status"])
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=ROOT)
    parser.add_argument("--date")
    parser.add_argument("--directory",type=Path,help="隔离JSONL目录，只能在logs/test")
    parser.add_argument("--out-dir",type=Path)
    args=parser.parse_args()
    try:
        report=run(args.root,args.date,args.directory,args.out_dir)
    except (ValueError,KeyError,OSError,TypeError) as exc:
        parser.exit(1,f"自动复核失败：{exc}\n")
    return 1 if report["errors"] else 0


if __name__=="__main__":
    raise SystemExit(main())
