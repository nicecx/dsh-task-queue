# PLUGIN-SPEC（插件开发规范 v1 · 20260901-006 approved）

> 所有 dsh-* 插件开发/发布必须符合本节。评审关卡与发布审计均以此核对。
> 参考：Google ADK Agent-Agnostic Safety Plugins、阿里云可治理插件系统实践。

## 1. 能力声明（元数据必填）

- name（dsh- 前缀）、功能一句话、注入依赖（inject 清单）、权限边界、状态（dev/stable）
- 登记：CAPABILITY-INDEX.md 同步更新

## 2. 安全红线（沿用 OPS-GUARDRAILS + 协作契约）

- 最小权限：默认 workspace-write；工作区外写须审批（auto-approver/Hermes）
- 禁越权：不绕过审批链（denyAlways 最高优先；userGranted 保持空数组）
- fail-closed：Hermes 不可用/超时 → 转人工，绝不静默放行
- 审计：关键动作写日志（audit），投递类带 sessionId 可核验
- 禁危险模式：无条件 kickstart -k、pnpm install 于 profile 目录、自杀式重启

## 3. 接口兼容

- 对外工具/协议变更必须声明版本；新增字段向后兼容
- 破坏性变更（协议/流程）→ 契约版本 +1.0 并双端确认
- 协议文件（review-handoff 等）以单一权威为准，禁双路径漂移

## 4. 发布检查清单

- [ ] 能力声明完整（CAPABILITY-INDEX 登记）
- [ ] 测试通过（单测 + 相关 E2E）
- [ ] 复用评估节合规（003 关卡）
- [ ] 模板章节齐全（checkTemplateSections）
- [ ] 收录：awesome-dsh-plugin PR（满 1 天后提）
