# KNOWN-ISSUES（已知缺陷登记 · 20260901-006 approved）

> 各插件/脚本已知缺陷登记（lesson 跨 agent 传播的"已修复/待修复"状态跟踪）。
> 与 OPS-GUARDRAILS.md 关系：守则=append-only 防复发措施（Hermes lesson 机制）；
> 本清单=缺陷实例状态跟踪（open/fixed/verified），两者独立不混用。

## 登记格式

```
| 缺陷模式 | 文件 | 严重度 | 状态 | 来源(lesson/审核) | 复核 | 修复 |
| launchctl 无条件 -k | a.py | S1 | fixed | 20260830-xxx | agent-x | commit-y |
```

- 严重度：S1 安全/数据破坏 / S2 功能错误 / S3 健壮性噪声
- 状态：open → fixed → verified
- 来源：lesson requestId 或审核 requestId（可追溯）

## 当前条目

（空——首个 lesson 传播扫描后填充）
