from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.diagnostics import (  # noqa: E402
    DiagnosticToolService,
    ProcessResult,
    SubprocessRunner,
    ToolPolicy,
)
from robot_ops.audit import AuditUnavailable  # noqa: E402


class FakeRunner:
    def __init__(self, output: str = "ok") -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.output = output

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        self.calls.append((tuple(argv), timeout_seconds))
        return ProcessResult(0, self.output, "", 1.25)


class DiagnosticToolServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.audit_log = self.root / "audit" / "tools.jsonl"
        self.runner = FakeRunner()
        self.service = DiagnosticToolService(
            ToolPolicy(
                audit_log=self.audit_log,
                allowed_roots=(self.root,),
                allowed_journal_units=frozenset({"robot-base.service"}),
                ros2_binary="/opt/ros/jazzy/bin/ros2",
            ),
            self.runner,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _audit_records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.audit_log.read_text(encoding="utf-8").splitlines()]

    def test_unknown_motion_tool_is_rejected_without_process(self) -> None:
        result = self.service.execute(
            "ros_topic_publish",
            {"topic": "/cmd_vel", "message": "move"},
            trace_id="reject-motion",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")
        self.assertIn("tool_not_allowed", result.error or "")
        self.assertEqual(self.runner.calls, [])
        self.assertIn("tool_not_allowed", self._audit_records()[-1]["error"])

    def test_prompt_injection_in_topic_is_rejected(self) -> None:
        result = self.service.execute(
            "ros_topic_sample",
            {"topic": "/tf; ros2 topic pub /cmd_vel"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.runner.calls, [])

    def test_cmd_vel_can_only_be_sampled_not_published(self) -> None:
        result = self.service.execute("ros_topic_sample", {"topic": "/cmd_vel"})

        self.assertTrue(result.ok)
        argv, _ = self.runner.calls[0]
        self.assertEqual(
            argv,
            ("/opt/ros/jazzy/bin/ros2", "topic", "echo", "/cmd_vel", "--once"),
        )
        self.assertNotIn("pub", argv)

    def test_path_traversal_and_symlink_escape_are_rejected(self) -> None:
        outside = self.root.parent / "outside-test.log"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "escape.log"
        link.symlink_to(outside)
        try:
            direct = self.service.execute("log_tail", {"path": str(outside)})
            symlink = self.service.execute("log_tail", {"path": str(link)})
            self.assertEqual(direct.status, "rejected")
            self.assertEqual(symlink.status, "rejected")
        finally:
            outside.unlink()

    def test_journal_unit_requires_exact_allowlist_match(self) -> None:
        rejected = self.service.execute(
            "journal_tail", {"unit": "ssh.service", "lines": 10}
        )
        accepted = self.service.execute(
            "journal_tail", {"unit": "robot-base.service", "lines": 10}
        )

        self.assertEqual(rejected.status, "rejected")
        self.assertTrue(accepted.ok)
        self.assertIn("robot-base.service", self.runner.calls[0][0])

    def test_result_and_audit_preview_are_redacted(self) -> None:
        fake_openai_token = "sk" + "-" + "fixture-value-abcdefghijklmnopqrstuvwxyz"
        self.runner.output = f"token={fake_openai_token}"
        result = self.service.execute("ros_nodes")
        audit = self._audit_records()[-1]

        self.assertNotIn(fake_openai_token, result.output)
        self.assertEqual(result.output, "[REDACTED]")
        self.assertEqual(audit["output_preview"], "[REDACTED]")

    def test_shared_secret_patterns_redact_aws_and_github_values(self) -> None:
        fake_github_token = "github" + "_pat_" + "fixturevalueabcdefghijklmnopqrstuvwxyz"
        self.runner.output = (
            "AWS_SECRET_ACCESS_KEY=not-a-real-secret-value\n"
            f"{fake_github_token}"
        )

        result = self.service.execute("ros_nodes")

        self.assertNotIn("not-a-real-secret", result.output)
        self.assertNotIn(fake_github_token, result.output)

    def test_truncated_private_key_block_redacts_remaining_body(self) -> None:
        self.runner.output = (
            "-----BEGIN PRIVATE KEY-----\n"
            "ZmFrZS1iYXNlNjQtcHJpdmF0ZS1rZXktYm9keQ=="
        )

        result = self.service.execute("ros_nodes")

        self.assertEqual(result.output, "[REDACTED]")
        self.assertNotIn("ZmFr", result.output)

    def test_audit_log_is_private_and_has_started_then_completed_records(self) -> None:
        result = self.service.execute("ros_nodes", trace_id="audit-sequence")
        records = self._audit_records()

        self.assertTrue(result.ok)
        self.assertEqual(self.audit_log.stat().st_mode & 0o777, 0o600)
        self.assertEqual([record["phase"] for record in records], ["started", "completed"])
        self.assertEqual({record["trace_id"] for record in records}, {"audit-sequence"})

    def test_unwritable_audit_sink_blocks_command_before_runner(self) -> None:
        blocker = self.root / "not-a-directory"
        blocker.write_text("block", encoding="utf-8")
        service = DiagnosticToolService(
            ToolPolicy(
                audit_log=blocker / "audit.jsonl",
                allowed_roots=(self.root,),
                ros2_binary="/opt/ros/jazzy/bin/ros2",
            ),
            self.runner,
        )

        with self.assertRaises(AuditUnavailable):
            service.execute("ros_nodes")

        self.assertEqual(self.runner.calls, [])

    def test_log_tail_reads_only_allowed_text_file(self) -> None:
        log_path = self.root / "robot.log"
        log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

        result = self.service.execute("log_tail", {"path": str(log_path), "lines": 2})

        self.assertTrue(result.ok)
        self.assertEqual(result.output, "two\nthree")

    def test_log_tail_caps_a_single_oversized_line(self) -> None:
        log_path = self.root / "giant.log"
        log_path.write_text("x" * 1_000_000, encoding="utf-8")

        result = self.service.execute("log_tail", {"path": str(log_path), "lines": 1})

        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), self.service.policy.max_output_chars)

    def test_subprocess_runner_caps_captured_output(self) -> None:
        runner = SubprocessRunner(max_output_bytes=128)

        result = runner.run(
            (sys.executable, "-c", "print('x' * 10000)"),
            timeout_seconds=2.0,
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 128)


if __name__ == "__main__":
    unittest.main()
