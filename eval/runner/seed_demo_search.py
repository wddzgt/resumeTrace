"""把 e2e 跑出的报告灌进服务库,供 /ui/ 直接演示(免再跑一次慢搜索)。

用法: python3 seed_demo_search.py --arm-dir e2e_b1
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.db import store  # noqa: E402

RES = Path(__file__).resolve().parents[1] / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm-dir", default="e2e_b1")
    ap.add_argument("--query-text", default="(演示)中信期货 应用架构师(风控平台方向) JD 全文见 fixtures/jd")
    args = ap.parse_args()
    arm = RES / args.arm_dir
    audit = json.loads((arm / "audit.json").read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in (arm / "summary.jsonl").read_text(encoding="utf-8").splitlines()]
    sid = store.create_search("6101b004ba4011f1b8ee3f9c7e9efa67", args.query_text,
                              audit.get("query_sha", "seed"), "jd", None)
    store.save_criteria(sid, 1, audit.get("criteria", []), audit.get("warnings", []))
    n = 0
    for r in rows:
        rep = json.loads((arm / f"report_{r['candidate']}.json").read_text(encoding="utf-8"))
        store.save_report(sid, r["candidate"], rep)
        n += 1
    store.finish_search(sid, "done")
    print(f"seeded search {sid} with {n} reports")


if __name__ == "__main__":
    main()
