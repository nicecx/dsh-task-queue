#!/usr/bin/env python3
"""
task_queue_consumer.py — Hermes 侧 task-queue 消费端（审核 20260831-017 要求落地）

消费 ~/.dsh/task-queue/queue.json（唯一真相源），租约/认领模型，并发 1。

安全设计（016/017 审核）：
- reset 门禁：~/.dsh/.guardian-last-action 10min 退避 + 多实例检查 + SIGTERM+KeepAlive
- 单写者去重：处理前校验 review-handoff/state.json.lastProcessedId
- 互斥锁：整个 read-pick-claim-write 周期持 busy 锁（O_EXCL 原子）

用法:
  python3 task_queue_consumer.py            # 单次扫描（Hermes cron */1 调用）
  python3 task_queue_consumer.py --dry-run  # 只读不执行

输出约定（monitor 门控）：
  健康恒静默（无输出）；异常输出稳定签名行，供 dsh_heartbeat_monitor 门控。
"""
import datetime
import json
import os
import subprocess
import sys

QUEUE = os.path.expanduser("~/.dsh/task-queue/queue.json")
BUSY = os.path.expanduser("~/.dsh/task-queue/.hermes-busy")
REVIEW_DIR = os.path.expanduser("~/.dsh/review-handoff")
GUARDIAN_LAST = os.path.expanduser("~/.dsh/.guardian-last-action")
LEASE_MS = 5 * 60 * 1000
MAX_ATTEMPTS = 3
RESET_COOLDOWN_SEC = 600  # 10min 退避


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    print(f"[task-queue-consumer] {now_iso()} {msg}", flush=True)


def read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(p, data):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def acquire_busy(owner, ttl_secs=120):
    """O_EXCL 原子获取互斥锁（与 DSH 侧 busy_mutex 同语义）。"""
    try:
        fd = os.open(BUSY, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"owner": owner, "expiresAt": (datetime.datetime.now().timestamp() + ttl_secs) * 1000}).encode())
        os.close(fd)
        return True
    except FileExistsError:
        cur = read_json(BUSY)
        if cur and datetime.datetime.now().timestamp() * 1000 >= cur.get("expiresAt", 0):
            try:
                os.unlink(BUSY)
                return acquire_busy(owner, ttl_secs)
            except FileNotFoundError:
                return acquire_busy(owner, ttl_secs)
        return False
    except OSError:
        return False


def release_busy():
    try:
        os.unlink(BUSY)
    except FileNotFoundError:
        pass


def guardian_cooldown_ok():
    """reset 门禁：10min 退避。"""
    g = read_json(GUARDIAN_LAST)
    if not g:
        return True, ""
    last = g.get("ts")
    if not last:
        return True, ""
    try:
        last_dt = datetime.datetime.fromisoformat(last)
        elapsed = (datetime.datetime.now().astimezone() - last_dt).total_seconds()
        if elapsed < RESET_COOLDOWN_SEC:
            return False, f"guardian 10min 退避中（已过 {int(elapsed)}s）"
    except Exception:
        pass
    return True, ""


def single_instance_ok():
    """reset 门禁：多实例检查。"""
    try:
        r = subprocess.run(["pgrep", "-f", "node.*dsh web"], capture_output=True, text=True, timeout=10)
        pids = [p for p in r.stdout.split() if p.strip()]
        if len(pids) > 1:
            return False, f"多实例 {len(pids)} 个（{','.join(pids[:5])}），先清理保留一个"
        return True, ""
    except Exception as e:
        return False, f"pgrep 失败: {e}"


def last_processed_id():
    s = read_json(os.path.join(REVIEW_DIR, "state.json")) or {}
    return s.get("lastProcessedId")


def execute_task(task):
    """执行任务，返回 (ok, note)。"""
    tier = task.get("tier")
    payload = task.get("payload") or {}

    if tier == "review":
        # 单写者去重：校验 state.json.lastProcessedId
        req_path = os.path.join(REVIEW_DIR, "request.json")
        rid = payload.get("requestId")
        if rid and last_processed_id() == rid:
            return False, "去重：该 requestId 已处理过"
        # 写 review-handoff/request.json（复用现有语义）
        req = {
            "protocol": "review-handoff/v1",
            "requestId": rid or "pending-queue",
            "type": payload.get("type", "design"),
            "ts": now_iso(),
            "title": payload.get("title", ""),
            "docPath": payload.get("docPath", ""),
            "changeFiles": payload.get("changeFiles", []),
            "tests": payload.get("tests", ""),
            "urgency": payload.get("urgency", "normal"),
            "status": "pending",
        }
        write_json(req_path, req)
        return True, f"已写 request.json ({rid})"

    if tier == "reset":
        # reset 门禁（016/017 致命意见）
        ok, note = guardian_cooldown_ok()
        if not ok:
            return False, f"reset 门禁拒绝: {note}"
        ok, note = single_instance_ok()
        if not ok:
            return False, f"reset 门禁拒绝: {note}"
        # SIGTERM+KeepAlive（launchctl kickstart -k 会触发 KeepAlive；此处用 kickstart 触发重启）
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.dsh.web"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False, f"重启失败: {r.stderr.strip()[:200]}"
        # 记录 guardian-last-action（reset 执行后写入方 = 消费者）
        write_json(GUARDIAN_LAST, {"ts": now_iso(), "action": "reset", "by": "task-queue-consumer"})
        return True, "reset 已触发（SIGTERM+KeepAlive）"

    return False, f"未知 tier: {tier}"


def main():
    dry = "--dry-run" in sys.argv
    q = read_json(QUEUE)
    if not q or not isinstance(q, list) or len(q) == 0:
        return 0  # 空队列，健康静默

    if not acquire_busy("hermes-cron"):
        return 0  # 锁被持有（approve 快道或上次 cron），静默跳过

    try:
        # pickNext（并发 1：有未过期 processing 则跳过；租约过期可重认领）
        now_ms = datetime.datetime.now().timestamp() * 1000
        has_active = any(t.get("status") == "processing" and (t.get("leaseExpiry") or "") and now_ms < _parse_ms(t.get("leaseExpiry")) for t in q)
        candidates = []
        for t in q:
            if t.get("status") == "queued" or (t.get("status") == "processing" and now_ms >= _parse_ms(t.get("leaseExpiry"))):
                candidates.append(t)
        if has_active:
            candidates = [t for t in candidates if t.get("status") == "processing" and now_ms >= _parse_ms(t.get("leaseExpiry"))]
        if not candidates:
            return 0
        # 优先级 + FIFO
        candidates.sort(key=lambda t: (t.get("priority", 1), t.get("createdAt", "")))
        task = candidates[0]

        if dry:
            log(f"[dry-run] 将执行 {task.get('id')} tier={task.get('tier')}")
            return 0

        # 认领
        task["status"] = "processing"
        task["claimedBy"] = "hermes-cron"
        task["leaseExpiry"] = datetime.datetime.fromtimestamp((now_ms + LEASE_MS) / 1000).astimezone().isoformat(timespec="seconds")
        task["updatedAt"] = now_iso()
        write_json(QUEUE, q)

        # 执行
        ok, note = execute_task(task)

        # 完成/失败
        if ok:
            task["status"] = "done"
        else:
            task["attempts"] = task.get("attempts", 0) + 1
            task["status"] = "failed" if task["attempts"] > MAX_ATTEMPTS else "queued"
            log(f"任务 {task.get('id')} {note}")
        task["claimedBy"] = None
        task["leaseExpiry"] = None
        task["updatedAt"] = now_iso()
        write_json(QUEUE, q)
        # 更新去重水位
        if ok and task.get("tier") == "review":
            state = read_json(os.path.join(REVIEW_DIR, "state.json")) or {}
            state["lastProcessedId"] = (task.get("payload") or {}).get("requestId")
            write_json(os.path.join(REVIEW_DIR, "state.json"), state)
        return 0
    finally:
        release_busy()


def _parse_ms(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp() * 1000
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
