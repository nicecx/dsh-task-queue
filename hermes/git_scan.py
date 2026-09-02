#!/usr/bin/env python3
"""git_scan.py — 每日低谷扫描 GitHub PR/issue 反馈（用户要求 2026-09-02）。

用法: python3 git_scan.py            # 输出当前状态摘要（monitor 门控：变化才唤醒 agent）
      python3 git_scan.py --full     # 详细输出

输出约定：首行稳定签名（各 PR 状态哈希），详情行跟进——monitor 哈希变化 = 有状态变化 → 唤醒。
"""
import json
import os
import subprocess
import sys

REPOS = [
    ("awesome-dsh-plugin", ["3919", "4046", "4134"]),   # 收录 PR
]
OWN = "nicecx"


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def pr_status(repo, pr):
    """返回 (state, title, 评论数)。"""
    out = sh(["gh", "pr", "view", pr, "--repo", f"{repo}/{repo}", "--json",
              "state,title,comments,reviews", "--jq",
              "{state, title, comments: (.comments|length), reviews: (.reviews|length)}"])
    try:
        d = json.loads(out)
        return d.get("state", "?"), d.get("title", "")[:40], d.get("comments", 0), d.get("reviews", 0)
    except Exception:
        return "?", "查询失败", 0, 0


def main():
    lines = []
    for repo, prs in REPOS:
        for pr in prs:
            state, title, comments, reviews = pr_status(repo, pr)
            lines.append(f"PR {repo}#{pr}: {state} | {title} | 评论{comments} 审查{reviews}")
    # 签名行 = 状态摘要（变化检测用）
    sig = "|".join(l.split(": ", 1)[1].split(" | ")[0] for l in lines)
    print(f"[git-scan] {sig}")
    if "--full" in sys.argv:
        for l in lines:
            print(f"  {l}")
    # 检查各仓库是否有新 issue（简单计数）
    for repo in ["dsh-auto-approver", "dsh-design-review", "dsh-task-queue", "dsh-reset-handoff"]:
        n = sh(["gh", "issue", "list", "--repo", f"{OWN}/{repo}", "--state", "open", "--limit", "10", "--json", "number", "--jq", "length"])
        if n and n != "0":
            print(f"  {repo}: {n} 个 open issue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
