/**
 * dsh-task-queue 核心逻辑（纯函数，无副作用，便于单测）。
 *
 * 审核 20260831-015 approved 的机制：
 *  - 3 tier：reset > approve > review（payload.type 区分 design/lesson）
 *  - 并发 1（Hermes cron 串行消费）
 *  - 租约/认领：claimedBy / leaseExpiry / attempts / 心跳续租 / 超时重认领
 *  - 优先级 aging（等待 >30min 优先级 +1，防饿死）
 *  - queue.json 唯一真相源
 */

import fs from 'node:fs'
import path from 'node:path'

export const TIERS = ['reset', 'approve', 'review']
export const STATUS = ['queued', 'processing', 'done', 'failed']

/** 默认配置。 */
export function defaultConfig() {
  return {
    queueDir: undefined,       // 默认 ~/.dsh/task-queue/
    leaseMs: 5 * 60 * 1000,    // 租约 5min
    heartbeatMs: 2 * 60 * 1000, // 心跳 2min
    maxAttempts: 3,
    agingMs: 30 * 60 * 1000,   // aging：等待 30min 优先级 +1
  }
}

/** 队列路径。 */
export function queuePath(dir) {
  return path.join(dir, 'queue.json')
}

/** 读队列（容错）。 */
export function readQueue(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return []
  }
}

/** 写队列（原子）。 */
export function writeQueue(file, tasks) {
  const tmp = file + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(tasks, null, 2))
  fs.renameSync(tmp, file)
}

/** 构造任务。 */
export function makeTask({ tier, payload, priority = 1, id, now = new Date() }) {
  return {
    id: id || `tq-${now.toISOString().replace(/[-:TZ]/g, '').slice(0, 14)}-${Math.floor(Math.random() * 1000)}`,
    tier,
    payload,
    priority,
    status: 'queued',
    attempts: 0,
    claimedBy: null,
    leaseExpiry: null,
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
  }
}

/** 入队（追加，同 tier FIFO）。 */
export function enqueue(tasks, task) {
  tasks.push(task)
  return tasks
}

/** 选择下一个出队任务：并发 1（有未过期 processing 则不再选新）；租约过期可重认领。 */
export function pickNext(tasks, now = new Date(), cfg = defaultConfig()) {
  const ts = now.getTime()
  // 并发 1：若有未过期的 processing 任务，不选新的（除非它租约过期可重认领）
  const hasActive = tasks.some((t) => t.status === 'processing' && !isLeaseExpired(t, ts))
  const candidates = tasks
    .filter((t) => {
      if (hasActive) return t.status === 'processing' && isLeaseExpired(t, ts)
      return t.status === 'queued' || (t.status === 'processing' && isLeaseExpired(t, ts))
    })
    .map((t) => {
      // aging：等待超过 agingMs 提升优先级（防饿死）
      let priority = t.priority
      const waitMs = ts - new Date(t.createdAt).getTime()
      if (t.status === 'queued' && waitMs > cfg.agingMs) {
        const aging = Math.floor(waitMs / cfg.agingMs)
        priority = Math.max(0, t.priority - aging)
      }
      return { t, priority }
    })
  if (candidates.length === 0) return null
  candidates.sort((a, b) => {
    // 优先级（数字小=高）+ 创建时间（FIFO）
    if (a.priority !== b.priority) return a.priority - b.priority
    return new Date(a.t.createdAt).getTime() - new Date(b.t.createdAt).getTime()
  })
  return candidates[0].t
}

/** 租约是否过期。 */
export function isLeaseExpired(task, nowTs) {
  if (task.status !== 'processing' || !task.leaseExpiry) return false
  return nowTs > new Date(task.leaseExpiry).getTime()
}

/** 认领任务（claimedBy + leaseExpiry + status=processing）。 */
export function claim(task, claimedBy, now = new Date(), leaseMs = defaultConfig().leaseMs) {
  task.status = 'processing'
  task.claimedBy = claimedBy
  task.leaseExpiry = new Date(now.getTime() + leaseMs).toISOString()
  task.updatedAt = now.toISOString()
  return task
}

/** 心跳续租（claimedBy 匹配时）。 */
export function renew(task, claimedBy, now = new Date(), leaseMs = defaultConfig().leaseMs) {
  if (task.status === 'processing' && task.claimedBy === claimedBy) {
    task.leaseExpiry = new Date(now.getTime() + leaseMs).toISOString()
    task.updatedAt = now.toISOString()
  }
  return task
}

/** 完成。 */
export function complete(task, now = new Date()) {
  task.status = 'done'
  task.claimedBy = null
  task.leaseExpiry = null
  task.updatedAt = now.toISOString()
  return task
}

/** 失败：attempts+1，≤maxAttempts 回 queued，否则 failed。 */
export function fail(task, now = new Date(), maxAttempts = defaultConfig().maxAttempts) {
  task.attempts += 1
  task.claimedBy = null
  task.leaseExpiry = null
  task.updatedAt = now.toISOString()
  task.status = task.attempts > maxAttempts ? 'failed' : 'queued'
  return task
}

/** 校验任务形状。 */
export function validateTask(task) {
  if (!task || typeof task !== 'object') return { ok: false, error: 'task 必须是对象' }
  if (!TIERS.includes(task.tier)) return { ok: false, error: `tier 应为 ${TIERS.join('/')}` }
  if (!STATUS.includes(task.status)) return { ok: false, error: `status 应为 ${STATUS.join('/')}` }
  if (typeof task.priority !== 'number' || task.priority < 0) return { ok: false, error: 'priority 非法' }
  return { ok: true }
}

/** 统计队列状态。 */
export function queueStats(tasks) {
  const stats = { total: tasks.length, queued: 0, processing: 0, done: 0, failed: 0, byTier: {} }
  for (const t of tasks) {
    stats[t.status] += 1
    stats.byTier[t.tier] = (stats.byTier[t.tier] || 0) + 1
  }
  return stats
}
