#!/usr/bin/env python3
"""
task_queue_consumer.py — Hermes 侧 task-queue 消费端（审核 20260831-017 要求落地）

消费 ~/.dsh/task-queue/queue.json（唯一真相源），租约/认领模型，并发 1。

安全设计（016/017 审核）：
- reset 门禁：~/.dsh/reset-handoff/last-restart.json 10min 退避 + 多实例检查（025/026：执行=调权威 reset_agent.py v2 全流程，含预隔离/健康检查/recovery）
- 单写者去重：处理前校验 review-handoff/state.json.lastProcessedId
- 单槽（026 修正）：他人 pending 且其 result 未出 → defer，防覆盖
- approve tier：cron 永不认领（pickNext 过滤），仅快道同步直调认领
- 互斥锁：整个 read-pick-claim-write 周期持 busy 锁（O_EXCL 原子，与 DSH 侧 busy_mutex 同一文件）

用法:
  python3 task_queue_consumer.py            # 单次扫描（Hermes cron */1 调用）
  python3 task_queue_consumer.py --dry-run  # 只读不执行

输出约定（monitor 门控）：
  健康恒静默（无输出）；异常输出稳定签名行（无时间戳，020 审核），供 dsh_heartbeat_monitor 门控。
"""
import datetime
import json
import os
import shutil
import subprocess
import sys

QUEUE = os.environ.get("TASK_QUEUE_PATH") or os.path.expanduser("~/.dsh/task-queue/queue.json")
BUSY = os.path.join(os.path.dirname(QUEUE), ".hermes-busy")  # 与 DSH 侧 busy_mutex 同一文件
REVIEW_DIR = os.environ.get("REVIEW_HANDOFF_DIR") or os.path.expanduser("~/.dsh/review-handoff")
DOCS = os.path.join(REVIEW_DIR, "docs")  # 024/025：PROTOCOL 定义 docs/ 在此目录下
# 025/026：权威路径（实测存在）；workspace 副本 dsh-reset-handoff/hermes/reset_agent.py 须同步
RESET_AGENT = os.path.expanduser("~/.hermes/profiles/reset-agent/scripts/reset_agent.py")
LEASE_MS = 5 * 60 * 1000
MAX_ATTEMPTS = 3
RESET_COOLDOWN_SEC = 600  # 10min 退避
RESET_TIMEOUT_SEC = 600   # 026：覆盖 health_check(~180s)+recover 多轮重启最坏路径；超时=计 failed 重试


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    # 020 审核意见1：monitor 契约要求输出稳定（no timestamps）——
    # 时间戳会让同一异常每 tick 重触发 agent，去掉时间戳，用固定前缀 + 任务 id + 稳定 note
    print(f"[task-queue-consumer] {msg}", flush=True)


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
    """reset 门禁：10min 退避（读 reset_agent.py 的 last-restart.json，JSON 可靠格式）。"""
    # 意见1（018 审核）：.guardian-last-action 是纯文本，read_json 必失败→恒放行；
    # 改用统一冷却文件 ~/.dsh/reset-handoff/last-restart.json（reset_agent.py 同款）
    lr = read_json(os.path.expanduser("~/.dsh/reset-handoff/last-restart.json"))
    if not lr:
        return True, ""
    last = lr.get("restartedAt")
    if not last:
        return True, ""
    try:
        last_dt = datetime.datetime.fromisoformat(last)
        elapsed = (datetime.datetime.now().astimezone() - last_dt).total_seconds()
        if elapsed < RESET_COOLDOWN_SEC:
            # 021 审核阻塞项：note 必须稳定（无 elapsed 秒数，否则 defer 输出随 tick 变化
            # 在 10min 冷却窗口内每分钟重复唤醒 agent）
            return False, "reset 10min 退避中"
    except Exception:
        pass
    return True, ""


def dsh_pids():
    """过滤出真正的 dsh web 主进程（意见5：pgrep -f 会命中监控/包装进程）。"""
    try:
        r = subprocess.run(["pgrep", "-f", "node.*dsh"], capture_output=True, text=True, timeout=10)
        pids = []
        for p in r.stdout.split():
            p = p.strip()
            if not p:
                continue
            try:
                # 校验进程命令行确实含 dsh web（排除监控/包装）
                r2 = subprocess.run(["ps", "-o", "command=", "-p", p], capture_output=True, text=True, timeout=5)
                cmd = r2.stdout.strip()
                if "dsh" in cmd and "web" in cmd and "watchdog" not in cmd and "monitor" not in cmd:
                    pids.append(p)
            except Exception:
                continue
        return pids
    except Exception:
        return []


def single_instance_ok():
    """reset 门禁：多实例检查（用过滤后的 dsh_pids）。"""
    pids = dsh_pids()
    if len(pids) > 1:
        return False, f"多实例 {len(pids)} 个（{','.join(pids[:5])}），先清理保留一个"
    return True, ""


def last_processed_id():
    s = read_json(os.path.join(REVIEW_DIR, "state.json")) or {}
    return s.get("lastProcessedId")


def review_slot_ok(rid):
    """单槽检查（026 修正：比较对象 = 当前 pending 的 request.json.requestId，勿用新 rid）。

    仅当"存在他人 pending 且该 pending 尚未出 result"才 defer；
    与 PROTOCOL drain 校验（state.json.lastProcessedId != q.requestId 判占用）语义一致。
    """
    q = read_json(os.path.join(REVIEW_DIR, "request.json"))
    if q and q.get("status") == "pending" and q.get("requestId") != rid:
        r = read_json(os.path.join(REVIEW_DIR, "result.json")) or {}
        if r.get("requestId") != q.get("requestId"):
            return False, "上一请求未出 result，单槽 defer"
    return True, ""


def execute_task(task):
    """执行任务，返回 (result, note)；result ∈ {done, defer, failed}。

    - done:   任务完成（含去重——使命已达成）
    - defer:  门禁拒绝（冷却/多实例），不烧 attempts，保持 queued 等下次 cron
    - failed: 真实执行失败（kickstart 失败等），才计 attempts
    """
    tier = task.get("tier")
    payload = task.get("payload") or {}

    if tier == "review":
        # 单写者去重：校验 state.json.lastProcessedId
        req_path = os.path.join(REVIEW_DIR, "request.json")
        rid = payload.get("requestId")
        if rid and last_processed_id() == rid:
            return "done", f"去重：requestId {rid} 已处理过（使命完成）"
        # 单槽检查（026 修正版，防覆盖他人 pending）
        ok, note = review_slot_ok(rid)
        if not ok:
            return "defer", note
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
        # doc 落盘（024/025/026：消费端同步快照 docs/<requestId>.md，与入队端幂等双保险；
        # 入队端已复制到 docs/ 时 src==dst，须跳过避免 SameFileError）
        doc = payload.get("docPath")
        if doc and os.path.exists(doc):
            dst = os.path.join(DOCS, f"{rid or 'pending-queue'}.md")
            if os.path.realpath(doc) != os.path.realpath(dst):
                os.makedirs(DOCS, exist_ok=True)
                shutil.copy2(doc, dst)
        return "done", f"已写 request.json ({rid})"

    if tier == "reset":
        # reset 门禁（016/017 致命意见；020 意见2：门禁拒绝走 defer，不烧 attempts）
        ok, note = guardian_cooldown_ok()
        if not ok:
            return "defer", f"reset 门禁拒绝: {note}"
        ok, note = single_instance_ok()
        if not ok:
            return "defer", f"reset 门禁拒绝: {note}"
        # 写全字段 request.json（025/026：reset_agent.py read_request 校验 schema/version，
        # 缺 id/reason → main() KeyError 崩溃；先落盘再调用，保证审计链）
        write_json(os.path.join(REVIEW_DIR, "request.json"), {
            "schema": "dsh-reset-handoff/request",
            "version": 1,
            "id": payload.get("id", ""),
            "reason": payload.get("reason", ""),
            "scope": payload.get("scope", {}),
            "ts": now_iso(),
            "status": "pending",
        })
        # 调权威 reset_agent.py v2 全流程（025/026：预检→预隔离→三段式→健康检查→recovery；
        # timeout=600 覆盖最坏路径；超时计 failed 重试，10min 退避门禁防重复 reset 风暴）
        try:
            r = subprocess.run(["python3", RESET_AGENT], capture_output=True, text=True, timeout=RESET_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return "failed", "reset_agent.py 超时(600s)"
        if r.returncode != 0:
            return "failed", f"reset_agent.py 失败: {r.stderr.strip()[:200]}"
        return "done", f"reset_agent.py 完成 (id={payload.get('id', '')})"

    return "failed", f"未知 tier: {tier}"


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
            if t.get("tier") == "approve":
                continue  # 024/025/026：approve 仅由快道同步直调认领，cron 永不抢单（防烧 attempts）
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

        # 执行（三态：done / defer / failed；异常兜底 → 计 failed，避免任务卡 processing）
        try:
            result, note = execute_task(task)
        except Exception as e:
            result, note = "failed", f"execute_task 异常: {type(e).__name__}: {str(e)[:120]}"

        # 完成/推迟/失败
        if result == "done":
            task["status"] = "done"
        elif result == "defer":
            # 020 意见2：门禁拒绝不烧 attempts，保持 queued 等下次 cron 重试
            task["status"] = "queued"
            log(f"任务 {task.get('id')} 推迟: {note}")
        else:
            task["attempts"] = task.get("attempts", 0) + 1
            # 020 意见3：>= MAX_ATTEMPTS 即 failed（attempts 到 3 就判失败，语义与 MAX_ATTEMPTS=3 一致）
            task["status"] = "failed" if task["attempts"] >= MAX_ATTEMPTS else "queued"
            log(f"任务 {task.get('id')} 失败: {note}")
        task["claimedBy"] = None
        task["leaseExpiry"] = None
        task["updatedAt"] = now_iso()
        write_json(QUEUE, q)

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
