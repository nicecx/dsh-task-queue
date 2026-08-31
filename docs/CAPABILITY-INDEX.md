# DSH 能力资产索引（CAPABILITY-INDEX v0.1）

> 用途：开发新功能前的**第一站检索**——先查这里，再决定"复用/扩展/新造"。
> 维护：随插件开发/收录更新；对应规范见协作契约"开发前复用检索"条款。

## A. 审批与安全
| 能力 | 仓库/位置 | 功能 | 状态 |
|---|---|---|---|
| 审批自动代理 | dsh-auto-approver | 权限请求规则层 + Hermes Pro 语义裁决 + fail-closed 转人工 + 拒绝反馈回会话 | ✅ 已部署 |
| 远程审批通道 | dsh-remote-approval | 审批双轨推送（iMessage/web） | ✅ 已部署 |

## B. 协作协议与队列
| 能力 | 仓库/位置 | 功能 | 状态 |
|---|---|---|---|
| 重置托管 | dsh-reset-handoff | reset_handoff 协议：预检→门禁→重启→健康检查→恢复 | ✅ 已部署 |
| 缓存梯队队列 | dsh-task-queue | 3 tier 任务队列（reset>approve>review）+ 租约/认领 + 消费端 cron | ✅ 已部署 |
| 设计交叉评审 | dsh-design-review | 设计文档自动识别 → 入队提审 → 结论回会话 + 守则追加 | ✅ 已部署 |
| 审核协议 | review-handoff（PROTOCOL v1.1→v1.2） | DSH↔Hermes 审核协议（单槽/取号/路由） | ✅ 运行中 |

## C. 输入与媒体
| 能力 | 仓库/位置 | 功能 | 状态 |
|---|---|---|---|
| 语音输入 | dsh-voice-input / dsh-stt-input | 语音转文字输入通道 | ✅ 可用 |
| 视觉路由 | dsh-vision-router | 图像/视觉任务路由（描述/OCR/检测等） | ✅ 可用 |
| iMessage 收发 | dsh-relay | chat.db 注入 + osascript 收发 | ✅ 已部署 |

## D. UI 与外观
| 能力 | 仓库/位置 | 功能 | 状态 |
|---|---|---|---|
| Web UI 皮肤 | dsh-web-ui / dsh-matrix-skin | Matrix 暗色皮肤（已合入上游） | ✅ 已合并 |
| 远程 Web UI | dsh-remote-web-ui | 远程访问（认证反代 tailnet/funnel） | ⚠️ 已禁用 |

## E. 系统与工具链
| 能力 | 仓库/位置 | 功能 | 状态 |
|---|---|---|---|
| 日历集成 | dsh-macos-calendar | macOS Calendar 增删查 | ✅ 已部署 |
| 邮件 | dsh-email | IMAP 收发 | ✅ 已部署 |
| 看门狗 | dsh-task-watchdog | 任务心跳/自愈 | ✅ 已部署 |
| 干跑测试生成 | dsh-dryrun-gen | 插件 dry-run 测试设计 | ✅ 技能 |
| 智能体流程 | dsh-agent-flow | agent 编排 | ✅ 已部署 |

## F. 外部生态检索（第二站）
- awesome-dsh-plugin：**2712 个收录插件**（`~/Documents/Workspace/awesome-dsh-plugin/data/plugins/`）——开发新功能前必查
- GitHub 搜索：按关键词找成熟方案（web_search 工具）

## G. 历史设计（第三站）
- `~/.dsh/review-handoff/docs/*.md`（35 份）——同类问题历史方案/教训，提审前必查
- 各仓库 docs/review-*.md——审核结论与修订历史
