from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence
from unittest import mock


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

    def _write_trajectory(self, name: str = "trajectory.jsonl") -> Path:
        path = self.root / name
        header = {
            "schema": "robot-ops-trajectory/1",
            "robot_id": "so101",
            "arm": "left",
            "joint_names": ["shoulder"],
            "units": "rad",
            "fps": 10,
            "source": {
                "kind": "fixture",
                "path": "fixture",
                "episode": 0,
                "sha256": "0" * 64,
            },
            "origin": "real_recording",
        }
        rows = [
            {
                "t": index / 10,
                "q_cmd": [math.radians(index * 1.2)],
                "q_obs": [0.0],
                "tcp_cmd_m": [index * 0.01, 0.0, 0.0],
                "tcp_obs_m": [0.0, 0.0, 0.0],
            }
            for index in range(6)
        ]
        path.write_text(
            "\n".join(json.dumps(item) for item in [header, *rows]) + "\n",
            encoding="utf-8",
        )
        return path

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

    def test_analyze_trajectory_returns_bounded_summary_without_process(self) -> None:
        path = self._write_trajectory()

        result = self.service.execute("analyze_trajectory", {"path": str(path)})

        self.assertTrue(result.ok)
        self.assertEqual(self.runner.calls, [])
        summary = json.loads(result.output)
        self.assertEqual(
            set(summary),
            {
                "source_path",
                "source_sha256",
                "frame_count",
                "duration_s",
                "joint_names",
                "thresholds",
                "freeze_rule",
                "incidents",
                "tcp",
                "max_joint_err_deg",
            },
        )
        self.assertEqual(
            summary["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(summary["frame_count"], 6)
        self.assertEqual(summary["thresholds"], {"joint_deg": 3.0, "tcp_mm": 20.0})
        self.assertEqual(
            summary["freeze_rule"],
            {"eps_deg": 0.1, "delta_deg": 0.5, "min_frames": 3},
        )
        self.assertNotIn("series", result.output)
        self.assertTrue(summary["incidents"])

    def test_analyze_trajectory_rejects_invalid_inputs_and_audits_completion(self) -> None:
        outside = self.root.parent / f"outside-{self.root.name}-trajectory.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        symlink = self.root / "linked.jsonl"
        valid = self._write_trajectory()
        symlink.symlink_to(valid)
        no_rows = self.root / "no-rows.jsonl"
        no_rows.write_text(
            valid.read_text(encoding="utf-8").splitlines()[0] + "\n",
            encoding="utf-8",
        )
        nonfinite = self.root / "nonfinite.jsonl"
        nonfinite.write_text(
            valid.read_text(encoding="utf-8").replace("0.0]", "NaN]", 1),
            encoding="utf-8",
        )
        invalid_schema = self.root / "invalid-schema.jsonl"
        invalid_schema.write_text(
            valid.read_text(encoding="utf-8").replace(
                "robot-ops-trajectory/1", "robot-ops-trajectory/999", 1
            ),
            encoding="utf-8",
        )
        wrong_extension = self.root / "trajectory.txt"
        wrong_extension.write_bytes(valid.read_bytes())
        try:
            cases = (
                (outside, "path_not_allowed"),
                (symlink, "symlink_not_allowed"),
                (no_rows, "궤적 행이 없습니다"),
                (nonfinite, "invalid_trajectory"),
                (invalid_schema, "invalid_trajectory"),
                (wrong_extension, "invalid_trajectory_path"),
            )
            for candidate, reason in cases:
                with self.subTest(candidate=candidate.name):
                    result = self.service.execute(
                        "analyze_trajectory", {"path": str(candidate)}
                    )
                    self.assertEqual(result.status, "rejected")
                    self.assertIn(reason, result.error or "")
                    self.assertEqual(self._audit_records()[-1]["phase"], "completed")
            self.assertEqual(self.runner.calls, [])
        finally:
            outside.unlink()

    def test_analyze_trajectory_enforces_size_and_row_limits(self) -> None:
        oversized = self.root / "oversized.jsonl"
        with oversized.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        size_result = self.service.execute(
            "analyze_trajectory", {"path": str(oversized)}
        )
        self.assertEqual(size_result.status, "rejected")
        self.assertIn("trajectory_too_large", size_result.error or "")

        path = self._write_trajectory()
        with mock.patch("robot_ops.diagnostics.MAX_TRAJECTORY_ROWS", 2):
            rows_result = self.service.execute(
                "analyze_trajectory", {"path": str(path)}
            )
        self.assertEqual(rows_result.status, "rejected")
        self.assertIn("trajectory_too_many_rows", rows_result.error or "")
        self.assertEqual(self.runner.calls, [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO는 이 플랫폼에서 지원되지 않습니다")
    def test_analyze_trajectory_rejects_fifo_without_blocking(self) -> None:
        fifo = self.root / "trajectory.jsonl"
        os.mkfifo(fifo)

        result = self.service.execute("analyze_trajectory", {"path": str(fifo)})

        self.assertEqual(result.status, "rejected")
        self.assertIn("일반 파일", result.error or "")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self._audit_records()[-1]["phase"], "completed")

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
