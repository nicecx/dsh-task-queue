import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  defaultConfig, makeTask, enqueue, pickNext, claim, renew, complete, fail,
  isLeaseExpired, validateTask, queueStats, TIERS, findRequestIdConflict,
} from '../src/core.js'

const NOW = new Date('2026-08-31T10:00:00Z')

test('makeTask 默认字段', () => {
  const t = makeTask({ tier: 'review', payload: { type: 'design' }, now: NOW })
  assert.equal(t.tier, 'review')
  assert.equal(t.status, 'queued')
  assert.equal(t.attempts, 0)
  assert.equal(t.claimedBy, null)
  assert.ok(validateTask(t).ok)
})

test('T1: 入队 5 个不同 tier → 按优先级+FIFO 出队', () => {
  const tasks = []
  enqueue(tasks, makeTask({ tier: 'review', priority: 1, now: NOW }))
  enqueue(tasks, makeTask({ tier: 'approve', priority: 1, now: new Date(NOW.getTime() + 1000) }))
  enqueue(tasks, makeTask({ tier: 'reset', priority: 1, now: new Date(NOW.getTime() + 2000) }))
  enqueue(tasks, makeTask({ tier: 'review', priority: 2, now: new Date(NOW.getTime() + 3000) }))
  enqueue(tasks, makeTask({ tier: 'approve', priority: 0, now: new Date(NOW.getTime() + 4000) }))
  const order = []
  let next = pickNext(tasks, new Date(NOW.getTime() + 5000))
  while (next) {
    order.push(next.tier + ':' + next.priority)
    claim(next, 'hermes-cron', new Date(NOW.getTime() + 5000))
    complete(next, new Date(NOW.getTime() + 5000))
    next = pickNext(tasks, new Date(NOW.getTime() + 5000))
  }
  // 优先级 0 的 approve 最先，然后 FIFO
  assert.deepEqual(order, ['approve:0', 'review:1', 'approve:1', 'reset:1', 'review:2'])
})

test('T2: 同 tier 并发 → 串行（processing 不被重复选）', () => {
  const tasks = []
  enqueue(tasks, makeTask({ tier: 'review', now: NOW }))
  enqueue(tasks, makeTask({ tier: 'review', now: new Date(NOW.getTime() + 1000) }))
  const first = pickNext(tasks, NOW)
  claim(first, 'hermes-cron', NOW)
  // processing 中不再被选出（pickNext 返回 null）
  assert.equal(pickNext(tasks, NOW), null)
  // 完成后再选下一个
  complete(first, NOW)
  const second = pickNext(tasks, NOW)
  assert.ok(second)
})

test('T3: 租约到期 → 可重认领', () => {
  const tasks = []
  const t = makeTask({ tier: 'review', now: NOW })
  enqueue(tasks, t)
  claim(t, 'hermes-cron', NOW)
  // 租约过期后（6min 后），可被重新选出
  const later = new Date(NOW.getTime() + 6 * 60 * 1000)
  const next = pickNext(tasks, later)
  assert.equal(next.id, t.id)
  assert.equal(isLeaseExpired(t, later.getTime()), true)
})

test('T4: 失败重试 ≤3 回 queued，>3 failed', () => {
  const t = makeTask({ tier: 'review', now: NOW })
  for (let i = 1; i <= 3; i++) {
    fail(t, NOW)
    assert.equal(t.status, 'queued')
    assert.equal(t.attempts, i)
  }
  fail(t, NOW)
  assert.equal(t.status, 'failed')
  assert.equal(t.attempts, 4)
})

test('T5: aging 防饿死（等待超时优先级提升）', () => {
  const tasks = []
  const old = makeTask({ tier: 'review', priority: 2, now: new Date(NOW.getTime() - 60 * 60 * 1000) })
  const fresh = makeTask({ tier: 'review', priority: 1, now: NOW })
  enqueue(tasks, old)
  enqueue(tasks, fresh)
  // 60min 后 old 的 aging 已提升，应优先
  const later = new Date(NOW.getTime() + 60 * 60 * 1000)
  const next = pickNext(tasks, later)
  assert.equal(next.id, old.id)
})

test('renew 心跳续租', () => {
  const t = makeTask({ tier: 'approve', now: NOW })
  claim(t, 'hermes-cron', NOW)
  const before = t.leaseExpiry
  renew(t, 'hermes-cron', new Date(NOW.getTime() + 60000))
  assert.ok(new Date(t.leaseExpiry).getTime() > new Date(before).getTime())
})

test('queueStats', () => {
  const tasks = []
  const t1 = makeTask({ tier: 'review', now: NOW })
  const t2 = makeTask({ tier: 'reset', now: NOW })
  enqueue(tasks, t1)
  enqueue(tasks, t2)
  claim(t1, 'hermes-cron', NOW)
  const s = queueStats(tasks)
  assert.equal(s.total, 2)
  assert.equal(s.queued, 1)
  assert.equal(s.processing, 1)
})

test('validateTask 拒绝非法', () => {
  assert.equal(validateTask({ tier: 'bogus' }).ok, false)
  assert.equal(validateTask({ tier: 'review', status: 'bad' }).ok, false)
  assert.equal(validateTask(null).ok, false)
})

// ── busy_mutex 原子锁测试（017 审核要求）──
import { mkdtempSync, writeFileSync, readFileSync, unlinkSync, openSync, closeSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

test('busy_mutex: O_EXCL 原子获取 + 持有中拒绝 + 过期抢占', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'busy-'))
  const busyFile = path.join(dir, '.hermes-busy')
  // 模拟 acquire 逻辑（与 src/index.js 一致的原子路径）
  const acquire = (owner, ttlSecs = 120) => {
    const now = Date.now()
    const ttl = ttlSecs * 1000
    try {
      const fd = openSync(busyFile, 'wx')
      writeFileSync(fd, JSON.stringify({ owner, expiresAt: now + ttl }))
      closeSync(fd)
      return { ok: true }
    } catch (err) {
      if (err.code !== 'EEXIST') return { ok: false, error: err.message }
      try {
        const cur = JSON.parse(readFileSync(busyFile, 'utf8'))
        if (now >= cur.expiresAt) {
          try { unlinkSync(busyFile) } catch { /* 竞争 */ }
          try {
            const fd = openSync(busyFile, 'wx')
            writeFileSync(fd, JSON.stringify({ owner, expiresAt: now + ttl }))
            closeSync(fd)
            return { ok: true, preempted: true }
          } catch { return { ok: false, error: '竞争失败' } }
        }
        return { ok: false, heldBy: cur.owner }
      } catch { return { ok: false, error: '损坏' } }
    }
  }
  // 首次获取成功
  assert.deepEqual(acquire('a', 120), { ok: true })
  // 他人持有中 → 拒绝
  const r2 = acquire('b', 120)
  assert.equal(r2.ok, false)
  assert.equal(r2.heldBy, 'a')
  // 过期后抢占（把锁文件改成已过期）
  writeFileSync(busyFile, JSON.stringify({ owner: 'a', expiresAt: Date.now() - 1000 }))
  const r3 = acquire('c', 120)
  assert.equal(r3.ok, true)
  assert.equal(r3.preempted, true)
  unlinkSync(busyFile)
})

// ---------- 20260903-010 approved：requestId 撞号冲突检测 ----------

test('010: findRequestIdConflict — queue 已有同号（含终态）→ 冲突', () => {
  const tasks = [
    makeTask({ tier: 'review', payload: { type: 'design', requestId: '20260903-005' }, now: NOW }),
    { ...makeTask({ tier: 'review', payload: { type: 'arbitration', requestId: '20260903-006' }, now: NOW }), status: 'done' },
  ]
  assert.equal(findRequestIdConflict(tasks, { rid: '20260903-005' }).ok, false, 'queued 同号冲突')
  assert.equal(findRequestIdConflict(tasks, { rid: '20260903-006' }).ok, false, 'done 终态同号也冲突（防归档回收复撞）')
  assert.equal(findRequestIdConflict(tasks, { rid: '20260903-007' }).ok, true, '未用号放行')
})

test('010: findRequestIdConflict — docs 快照已存在 → 冲突', () => {
  const tasks = []
  const c = findRequestIdConflict(tasks, { rid: '20260903-008', docsExists: (r) => r === '20260903-008' })
  assert.equal(c.ok, false)
  assert.match(c.reason, /docs 快照/)
  const ok = findRequestIdConflict(tasks, { rid: '20260903-009', docsExists: () => false })
  assert.equal(ok.ok, true)
})

test('010: findRequestIdConflict — 空 rid 放行（自动取号路径）', () => {
  assert.equal(findRequestIdConflict([], { rid: '' }).ok, true)
  assert.equal(findRequestIdConflict([], { rid: undefined }).ok, true)
})
