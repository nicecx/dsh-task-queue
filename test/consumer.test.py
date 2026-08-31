"""task_queue_consumer 测试（017 审核要求 C6/C7 随代码落地；020 审核意见2/3 补测）。

C6: reset 门禁（退避期/多实例 → 不重启只告警）
C7: 去重（同 requestId 不二次处理）
020-2: 门禁拒绝走 defer（不烧 attempts、保持 queued）
020-3: attempts >= MAX_ATTEMPTS 即 failed（off-by-one 修复）
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hermes"))
spec = importlib.util.spec_from_file_location(
    "tqc", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hermes", "task_queue_consumer.py"))
tqc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tqc)


class TestGuardianCooldown(unittest.TestCase):
    def test_c6_cooldown_blocks_reset(self):
        """C6: last-restart.json 10min 退避期内 → reset 拒绝；note 必须稳定（021 阻塞项：无 elapsed 秒数）。"""
        import datetime as dt
        with tempfile.TemporaryDirectory() as td:
            lr = os.path.join(td, "last-restart.json")
            # 用 monkeypatch 指向临时文件
            tqc.write_json(lr, {"restartedAt": dt.datetime.now().astimezone().isoformat()})
            with mock.patch.object(tqc.os.path, "expanduser", return_value=lr):
                ok, note = tqc.guardian_cooldown_ok()
            self.assertFalse(ok)
            # 精确断言稳定签名行（含秒数即失败）
            self.assertEqual(note, "reset 10min 退避中")

    def test_c6_no_guardian_allows(self):
        """无冷却记录 → 放行。"""
        with tempfile.TemporaryDirectory() as td:
            lr = os.path.join(td, "nonexistent.json")
            with mock.patch.object(tqc.os.path, "expanduser", return_value=lr):
                ok, _ = tqc.guardian_cooldown_ok()
            self.assertTrue(ok)

    def test_c6_multi_instance_blocks(self):
        """C6: 多实例 → reset 拒绝。"""
        with mock.patch.object(tqc, "dsh_pids", return_value=["123", "456"]):
            ok, note = tqc.single_instance_ok()
            self.assertFalse(ok)
            self.assertIn("多实例", note)

    def test_c6_single_instance_allows(self):
        with mock.patch.object(tqc, "dsh_pids", return_value=["123"]):
            ok, _ = tqc.single_instance_ok()
            self.assertTrue(ok)

    def test_c7_dedup(self):
        """C7: 同 requestId 已处理 → 使命完成（done，不二次处理不重试）。"""
        with tempfile.TemporaryDirectory() as td:
            tqc.REVIEW_DIR = td
            tqc.write_json(os.path.join(td, "state.json"), {"lastProcessedId": "20260831-999"})
            task = {"tier": "review", "payload": {"requestId": "20260831-999", "type": "design"}}
            result, note = tqc.execute_task(task)
            self.assertEqual(result, "done")
            self.assertIn("去重", note)

    def test_020_defer_on_gate_deny(self):
        """020-2: 冷却期门禁拒绝 → defer（不烧 attempts，非 failed）。"""
        with mock.patch.object(tqc, "guardian_cooldown_ok", return_value=(False, "reset 10min 退避中")):
            task = {"tier": "reset", "attempts": 2}
            result, note = tqc.execute_task(task)
            self.assertEqual(result, "defer")
            self.assertIn("门禁拒绝", note)
            # attempts 保持原值——由 main() 的 defer 分支保证不入 failed
            self.assertEqual(task.get("attempts"), 2)

    def test_020_defer_on_multi_instance(self):
        """020-2: 多实例门禁拒绝 → defer。"""
        with mock.patch.object(tqc, "guardian_cooldown_ok", return_value=(True, "")), \
             mock.patch.object(tqc, "single_instance_ok", return_value=(False, "多实例 2 个")):
            task = {"tier": "reset", "attempts": 2}
            result, note = tqc.execute_task(task)
            self.assertEqual(result, "defer")
            self.assertIn("多实例", note)

    def test_020_off_by_one(self):
        """020-3: attempts 到 MAX_ATTEMPTS(=3) 即 failed，而不是 4 次。"""
        with tempfile.TemporaryDirectory() as td:
            # 构造队列 + 假执行失败，走 main() 完整分支
            queue = os.path.join(td, "queue.json")
            task = {"id": "t-1", "tier": "reset", "payload": {},
                    "status": "queued", "attempts": 2, "priority": 1, "createdAt": "2026-08-31T00:00:00+08:00"}
            tqc.write_json(queue, [task])
            tqc.QUEUE = queue
            with mock.patch.object(tqc, "acquire_busy", return_value=True), \
                 mock.patch.object(tqc, "execute_task", return_value=("failed", "kickstart 失败")):
                tqc.main()
            q = tqc.read_json(queue)
            self.assertEqual(q[0]["status"], "failed")
            self.assertEqual(q[0]["attempts"], 3)
            # 复原全局
            tqc.QUEUE = os.path.expanduser("~/.dsh/task-queue/queue.json")


if __name__ == "__main__":
    unittest.main()
