#!/usr/bin/env python3
"""git_scan.py v2 — 每日低谷扫描 GitHub 提交追踪表并动态刷新（用户要求 2026-09-02）。

追踪表：~/.dsh/github-tracking.json（submissions 状态 + candidates 候选）
流程：读追踪表 → 扫描各项 PR 真实状态（gh）→ 对比旧状态 → 更新追踪表 + 输出变化。
输出：首行稳定签名（状态哈希）——monitor 门控：有变化才唤醒 agent。

用法: python3 git_scan.py [--full]
"""
import datetime
import json
import os
import subprocess
import sys

TRACKING = os.environ.get("GITHUB_TRACKING_PATH") or os.path.expanduser("~/.dsh/github-tracking.json")
UPSTREAM = "awesome-dsh-plugin/awesome-dsh-plugin"


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def load_tracking():
    try:
        return json.load(open(TRACKING, encoding="utf-8"))
    except Exception:
        return {"submissions": [], "candidates": []}


def save_tracking(d):
    tmp = TRACKING + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, TRACKING)


def pr_state(pr):
    out = sh(["gh", "pr", "view", pr, "--repo", UPSTREAM, "--json", "state,title,comments",
              "--jq", "{state, comments: (.comments|length)}"])
    try:
        d = json.loads(out)
        state = d.get("state", "?")
    except Exception:
        return "?", ""
    checks = sh(["gh", "pr", "checks", pr, "--repo", UPSTREAM, "--json", "name,state",
                 "--jq", ".[] | .name + \":\" + .state"])
    gate = "".join(sorted(l.split(":")[-1][:1] for l in checks.splitlines())) if checks else ""
    return state, gate


def main():
    d = load_tracking()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    changed = []
    sigs = []
    for sub in d.get("submissions", []):
        pr = sub.get("pr", "")
        state, gate = pr_state(pr)
        old = sub.get("status", "")
        sub["status"] = state
        sub["gate"] = gate
        sub["lastCheck"] = now
        sigs.append(f"{state}{gate}")
        if state != old:
            changed.append(f"{sub.get('name')} #{pr}: {old} → {state}（gate={gate}）")
    d["updatedAt"] = now
    save_tracking(d)
    print(f"[git-scan] {'|'.join(sigs)}")
    if changed:
        print("变化:")
        for c in changed:
            print(f"  {c}")
    if "--full" in sys.argv:
        print("当前状态:")
        for sub in d.get("submissions", []):
            print(f"  #{sub.get('pr')} {sub.get('name')}: {sub.get('status')} gate={sub.get('gate','')} | {sub.get('issue','')}")
        cands = d.get("candidates", [])
        if cands:
            print(f"候选 {len(cands)} 项: " + ", ".join(c.get("name", "") for c in cands))
    return 0


if __name__ == "__main__":
    sys.exit(main())
