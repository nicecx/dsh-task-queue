/**
 * dsh-task-queue — 缓存梯队任务队列插件（宿主级，跨会话共享）。
 *
 * 审核 20260831-015 approved 架构：
 *  - queue.json 唯一真相源（~/.dsh/task-queue/queue.json）
 *  - 3 tier：reset > approve > review（payload.type 区分 design/lesson）
 *  - 并发 1（Hermes cron 串行消费，租约/认领模型）
 *  - DSH 侧只入队；Hermes 侧消费（cron 独立进程，不在 DSH 进程树内）
 *  - approve 关键路径：保留同步直调 Hermes + busy 互斥锁（不排队，避免 90s 超时）
 *
 * 工具：
 *  - task_enqueue(tier, payload, priority?)  入队任务
 *  - task_status()                           队列全景（只读）
 *  - busy_mutex_acquire/release              审批串行互斥锁（approve 快道用）
 */

import { mkdirSync, readFileSync, writeFileSync, existsSync, openSync, closeSync, unlinkSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {
  defaultConfig, makeTask, enqueue, readQueue, writeQueue, queueStats,
  pickNext, claim, complete, fail, renew, validateTask,
} from './core.js'

export const name = 'dsh-task-queue'

export function apply(ctx, rawConfig = {}) {
  const config = { ...defaultConfig(), ...(rawConfig || {}) }
  const queueDir = config.queueDir || path.join(os.homedir(), '.dsh', 'task-queue')
  const qPath = path.join(queueDir, 'queue.json')
  const busyFile = path.join(queueDir, '.hermes-busy')
  const ensureDir = () => mkdirSync(queueDir, { recursive: true })

  const load = () => {
    ensureDir()
    return readQueue(qPath)
  }
  const save = (tasks) => {
    ensureDir()
    writeQueue(qPath, tasks)
  }

  const toolDefs = [
    {
      name: 'task_enqueue',
      description: 'Enqueue a task into the tiered DSH↔Hermes task queue. Args: tier (required, reset|approve|review), payload (required object, e.g. {type:"design", docPath:...} or {type:"lesson", ...}), priority (optional 0=urgent 1=normal 2=low). The Hermes-side cron consumes the queue with a lease model; approve-tier should normally use the sync fast path instead (busy mutex).',
      parameters: {
        type: 'object',
        properties: {
          tier: { type: 'string', enum: ['reset', 'approve', 'review'], description: 'Task tier' },
          payload: { type: 'object', description: 'Task payload (type, docPath, requestId, etc.)' },
          priority: { type: 'integer', description: '0=urgent, 1=normal, 2=low (default 1)' },
        },
        required: ['tier', 'payload'],
        additionalProperties: false,
      },
      output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
      async execute(args) {
        const task = makeTask({ tier: args.tier, payload: args.payload, priority: args.priority ?? 1 })
        const v = validateTask(task)
        if (!v.ok) return `❌ ${v.error}`
        const tasks = load()
        enqueue(tasks, task)
        save(tasks)
        return `✅ 已入队 ${task.id}（tier=${task.tier}, priority=${task.priority}）\n队列: ${queueStats(tasks).queued} 个排队中`
      },
    },
    {
      name: 'task_status',
      description: 'Query the tiered task queue: total, queued/processing/done/failed counts, by-tier breakdown, and the next task that would be picked. Read-only.',
      parameters: { type: 'object', properties: {}, required: [], additionalProperties: false },
      output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
      async execute() {
        const tasks = load()
        const s = queueStats(tasks)
        const next = pickNext(tasks)
        const lines = [
          `任务队列: 共 ${s.total}（queued=${s.queued}, processing=${s.processing}, done=${s.done}, failed=${s.failed}）`,
          `按梯队: ${Object.entries(s.byTier).map(([k, n]) => `${k}=${n}`).join(', ')}`,
        ]
        if (next) {
          lines.push(`下一个: ${next.id}（tier=${next.tier}, priority=${next.priority}, 等待${Math.round((Date.now() - new Date(next.createdAt).getTime()) / 60000)}min）`)
        }
        return lines.join('\n')
      },
    },
    {
      name: 'busy_mutex_acquire',
      description: 'Acquire the Hermes busy mutex (serializes Hermes calls with the Hermes-side cron). Returns ok=true if acquired; ok=false with owner if held. Used by the approve fast path before calling hermes chat directly. Args: owner (required, e.g. auto-approver:session-xxx), ttlSecs (optional, default 120).',
      parameters: {
        type: 'object',
        properties: {
          owner: { type: 'string', description: 'Mutex owner id' },
          ttlSecs: { type: 'integer', description: 'TTL seconds (default 120)' },
        },
        required: ['owner'],
        additionalProperties: false,
      },
      output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
      async execute(args) {
        // 原子获取（O_EXCL 独占创建，修复 016 审核的 TOCTOU：读改写并发双双判未持有）
        ensureDir()
        const now = Date.now()
        const ttl = (args.ttlSecs ?? 120) * 1000
        try {
          const fd = openSync(busyFile, 'wx')
          writeFileSync(fd, JSON.stringify({ owner: args.owner, expiresAt: now + ttl }))
          closeSync(fd)
          return '✅ 已获取互斥锁'
        } catch (err) {
          if (err.code !== 'EEXIST') return `❌ 获取失败: ${err.message}`
          // 已存在：检查过期
          try {
            const cur = JSON.parse(readFileSync(busyFile, 'utf8'))
            if (now >= cur.expiresAt) {
              // 过期抢占（原子：unlink 后重试一次）
              try { unlinkSync(busyFile) } catch { /* 竞争无妨 */ }
              try {
                const fd = openSync(busyFile, 'wx')
                writeFileSync(fd, JSON.stringify({ owner: args.owner, expiresAt: now + ttl }))
                closeSync(fd)
                return '✅ 已抢占过期互斥锁'
              } catch (e2) { return `⏳ 抢占竞争失败（他人刚持有）` }
            }
            return `⏳ 互斥锁被持有（owner=${cur.owner}，剩余 ${Math.round((cur.expiresAt - now) / 1000)}s）`
          } catch { return `❌ 锁文件损坏，请人工清理 ${busyFile}` }
        }
      },
    },
    {
      name: 'busy_mutex_release',
      description: 'Release the Hermes busy mutex. Args: owner (must match the current holder to release).',
      parameters: {
        type: 'object',
        properties: { owner: { type: 'string', description: 'Owner id that acquired it' } },
        required: ['owner'],
        additionalProperties: false,
      },
      output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
      async execute(args) {
        ensureDir()
        let cur = null
        try { cur = existsSync(busyFile) ? JSON.parse(readFileSync(busyFile, 'utf8')) : null } catch { /* 忽略 */ }
        if (!cur) return 'ℹ️ 互斥锁未被持有'
        if (cur.owner !== args.owner) return `⚠️ owner 不匹配（当前 ${cur.owner}），未释放`
        try { unlinkSync(busyFile) } catch { /* 已不存在 */ }
        return '✅ 已释放互斥锁（已删除锁文件）'
      },
    },
  ]

  ctx.effect(() => {
    const disposers = toolDefs.map((def) => ctx.tools.register(def))
    return () => {
      for (const d of disposers) d()
    }
  }, 'dsh-task-queue: tools')

  ctx.logger?.info?.(
    `[dsh-task-queue] loaded (queue=${qPath}, lease=${config.leaseMs / 60000}min, maxAttempts=${config.maxAttempts})`,
  )
}

// Cordis 4 inject 声明。
export const inject = ['tools']
apply.inject = ['tools']

// 纯函数导出（单测与外部实现复用）
export {
  defaultConfig, makeTask, enqueue, readQueue, writeQueue, queueStats,
  pickNext, claim, complete, fail, renew, validateTask,
} from './core.js'
