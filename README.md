# dsh-task-queue

DSH↔Hermes 协作的缓存梯队任务队列（queue.json 唯一真相源）：reset > approve > review 三级，租约/认领模型、优先级 aging 防饿死、原子忙锁；Hermes 侧消费端（cron */1 + monitor 门控）出队执行。

## 联动（20260902-003 approved，独立部署·协议联动）

本插件是 DSH 治理体系的**队列中枢**，与应用层插件协议联动：

```
dsh-auto-approver（审批）──┐
dsh-design-review（评审）──┼──入队──▶ dsh-task-queue ──▶ 消费端 ──▶ Hermes
dsh-reset-handoff（重启）──┘
```

- **审批×队列**：approval 请求入队 approve tier（快道同步直调 + busy 锁共用）
- **评审×队列**：提审入队 review tier → 消费端出队 → 单槽写 request.json → Hermes 审核 → 结论按 sessionId 路由
- **重启×队列**：reset 入队（最高优先级）→ 门禁 → 调 reset_agent.py
- **共享机制**：.hermes-busy 互斥锁、requestId 单一编号源（docs/ 扫描）、fail-closed

详见协作契约「二·五 架构总览」（~/.dsh/DSH-HERMES-CONTRACT.md）。
