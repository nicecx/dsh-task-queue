"""task_queue_consumer 测试（017 审核要求 C6/C7 随代码落地）。

C6: reset 门禁（退避期/多实例 → 不重启只告警）
C7: 去重（同 requestId 不二次处理）
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
        """C6: guardian 10min 退避期内 → reset 拒绝。"""
        with tempfile.TemporaryDirectory() as td:
            tqc.GUARDIAN_LAST = os.path.join(td, ".guardian-last-action")
            tqc.write_json(tqc.GUARDIAN_LAST, {"ts": __import__("datetime").datetime.now().astimezone().isoformat()})
            ok, note = tqc.guardian_cooldown_ok()
            self.assertFalse(ok)
            self.assertIn("退避", note)

    def test_c6_no_guardian_allows(self):
        """无 guardian 记录 → 放行。"""
        with tempfile.TemporaryDirectory() as td:
            tqc.GUARDIAN_LAST = os.path.join(td, ".nonexistent")
            ok, _ = tqc.guardian_cooldown_ok()
            self.assertTrue(ok)

    def test_c6_multi_instance_blocks(self):
        """C6: 多实例 → reset 拒绝。"""
        with mock.patch.object(tqc.subprocess, "run") as m:
            m.return_value.stdout = "123 456\n"
            m.return_value.returncode = 0
            ok, note = tqc.single_instance_ok()
            self.assertFalse(ok)
            self.assertIn("多实例", note)

    def test_c6_single_instance_allows(self):
        with mock.patch.object(tqc.subprocess, "run") as m:
            m.return_value.stdout = "123\n"
            m.return_value.returncode = 0
            ok, _ = tqc.single_instance_ok()
            self.assertTrue(ok)

    def test_c7_dedup(self):
        """C7: 同 requestId 已处理 → 拒绝二次处理。"""
        with tempfile.TemporaryDirectory() as td:
            tqc.REVIEW_DIR = td
            tqc.write_json(os.path.join(td, "state.json"), {"lastProcessedId": "20260831-999"})
            task = {"tier": "review", "payload": {"requestId": "20260831-999", "type": "design"}}
            ok, note = tqc.execute_task(task)
            self.assertFalse(ok)
            self.assertIn("去重", note)


if __name__ == "__main__":
    unittest.main()
