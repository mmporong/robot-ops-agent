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

    def _write_simulation(self, directory: str = "run/isaac") -> Path:
        target = self.root / directory
        target.mkdir(parents=True)
        result = {
            "hardware_accessed": False,
            "observation_source": "simulator_ground_truth_not_rgb",
            "task_pass": True,
            "completed": True,
            "stop_reason": "sequence_finished",
            "recovery_enabled": False,
            "retry_count": 0,
            "events": [],
            "samples": [
                {
                    "time_s": 0.0,
                    "physics_time_s": 0.525,
                    "phase": "RESET",
                    "attempt": 0,
                    "contact_force_n": [0.0, 0.0],
                },
                {
                    "time_s": 0.1,
                    "physics_time_s": 0.625,
                    "phase": "CLOSE",
                    "attempt": 0,
                    "contact_force_n": [0.5, 0.5],
                },
            ],
        }
        plan = {
            "hardware_accessed": False,
            "observation_source": "simulator_ground_truth_not_rgb",
            "config": {
                "physics_dt_s": 0.1,
                "minimum_contact_force_n": 0.02,
            },
        }
        result_path = target / "result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        (target / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        return result_path

    def _write_alignment_simulation(self, directory: str = "alignment/isaac") -> Path:
        target = self.root / directory
        target.mkdir(parents=True)
        samples = [
            {
                "time_s": index / 10,
                "physics_time_s": 0.525 + index / 10,
                "phase": "RESET" if index <= 5 else "CLOSE",
                "attempt": 0,
                "cup_position_m": [0.39, 0.165, 0.78],
                "contact_force_n": [0.0, 0.0],
            }
            for index in range(7)
        ]
        result = {
            "hardware_accessed": False,
            "observation_source": "simulator_ground_truth_not_rgb",
            "task_pass": True,
            "completed": True,
            "stop_reason": "sequence_finished",
            "recovery_enabled": False,
            "retry_count": 0,
            "events": [],
            "samples": samples,
            "failure": "must_not_reach_alignment_formula",
            "evaluation_center_m": [99.0, 99.0, 99.0],
        }
        plan = {
            "hardware_accessed": False,
            "observation_source": "simulator_ground_truth_not_rgb",
            "runtime_overrides": {"grid_point": [999.0, 999.0]},
            "config": {
                "physics_dt_s": 0.1,
                "minimum_contact_force_n": 0.02,
                "cup_center_m": [0.39, 0.17, 0.78],
                "cup_spawn_offset_m": [88.0, 88.0, 88.0],
                "recovery": {
                    "observation_duration_s": 0.5,
                    "maximum_observation_motion_m": 0.001,
                },
            },
        }
        path = target / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        (target / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
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

    def test_analyze_simulation_reads_only_fixed_result_and_plan(self) -> None:
        path = self._write_simulation()

        tool_result = self.service.execute("analyze_simulation", {"path": str(path)})

        self.assertTrue(tool_result.ok, tool_result.error)
        self.assertEqual(self.runner.calls, [])
        summary = json.loads(tool_result.output)
        self.assertEqual(summary["status"], "no_issue_in_checked_scope")
        self.assertEqual(summary["runtime"]["sample_count"], 2)
        self.assertFalse(summary["runtime"]["hardware_accessed"])
        self.assertNotIn("samples", summary)
        self.assertEqual(
            summary["source"]["result_sha256"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_analyze_simulation_rejects_wrong_name_symlink_and_plan_symlink(self) -> None:
        path = self._write_simulation()
        wrong_name = path.with_name("copy.json")
        wrong_name.write_bytes(path.read_bytes())
        result_link = path.with_name("result-link.json")
        result_link.symlink_to(path)
        plan_path = path.with_name("plan.json")
        real_plan = path.with_name("real-plan.json")
        plan_path.rename(real_plan)
        plan_path.symlink_to(real_plan)

        wrong = self.service.execute("analyze_simulation", {"path": str(wrong_name)})
        linked_result = self.service.execute(
            "analyze_simulation", {"path": str(result_link)}
        )
        linked_plan = self.service.execute("analyze_simulation", {"path": str(path)})

        self.assertEqual(wrong.status, "rejected")
        self.assertEqual(linked_result.status, "rejected")
        self.assertEqual(linked_plan.status, "rejected")

    def test_analyze_simulation_rejects_path_outside_allowed_root(self) -> None:
        outside_dir = self.root.parent / f"{self.root.name}-outside" / "isaac"
        outside_dir.mkdir(parents=True, exist_ok=True)
        outside_path = outside_dir / "result.json"
        outside_path.write_text("{}", encoding="utf-8")
        try:
            result = self.service.execute(
                "analyze_simulation", {"path": str(outside_path)}
            )
            self.assertEqual(result.status, "rejected")
            self.assertIn("path_not_allowed", result.error or "")
        finally:
            outside_path.unlink()
            outside_dir.rmdir()
            outside_dir.parent.rmdir()

    def test_analyze_simulation_rejects_hardware_and_nonfinite_values(self) -> None:
        path = self._write_simulation("hardware/isaac")
        result = json.loads(path.read_text(encoding="utf-8"))
        result["hardware_accessed"] = True
        path.write_text(json.dumps(result), encoding="utf-8")

        hardware = self.service.execute("analyze_simulation", {"path": str(path)})

        self.assertEqual(hardware.status, "rejected")
        self.assertIn("hardware_accessed", hardware.error or "")

        other_path = self._write_simulation("nonfinite/isaac")
        result = json.loads(other_path.read_text(encoding="utf-8"))
        result["samples"][0]["contact_force_n"][0] = math.inf
        other_path.write_text(json.dumps(result), encoding="utf-8")

        nonfinite = self.service.execute(
            "analyze_simulation", {"path": str(other_path)}
        )

        self.assertEqual(nonfinite.status, "rejected")
        self.assertIn("NaN 또는 무한대", nonfinite.error or "")

    def test_analyze_cup_alignment_returns_bounded_initial_reset_proposal(self) -> None:
        path = self._write_alignment_simulation()

        tool_result = self.service.execute(
            "analyze_cup_alignment", {"path": str(path)}
        )

        self.assertTrue(tool_result.ok, tool_result.error)
        self.assertEqual(self.runner.calls, [])
        summary = json.loads(tool_result.output)
        self.assertEqual(summary["status"], "candidate_available")
        self.assertAlmostEqual(summary["proposal"]["correction_mm"][0], 0.0)
        self.assertAlmostEqual(summary["proposal"]["correction_mm"][1], -5.0)
        self.assertEqual(summary["proposal"]["observation"]["sample_count"], 6)
        self.assertEqual(
            summary["source"]["result_sha256"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        plan_path = path.with_name("plan.json")
        self.assertEqual(
            summary["source"]["plan_sha256"],
            hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        )
        self.assertTrue(summary["scope"]["read_only"])
        self.assertEqual(summary["scope"]["cause_status"], "undetermined")
        for forbidden in (
            "cup_spawn_offset_m",
            "grid_point",
            "failure",
            "task_pass",
            "completed",
            "stop_reason",
            "evaluation_center_m",
        ):
            self.assertNotIn(forbidden, tool_result.output)

    def test_analyze_cup_alignment_metadata_cannot_change_proposal(self) -> None:
        clean_path = self._write_alignment_simulation("clean-alignment/isaac")
        poisoned_path = self._write_alignment_simulation("poisoned-alignment/isaac")
        poisoned_result = json.loads(poisoned_path.read_text(encoding="utf-8"))
        poisoned_result.update(
            {
                "failure": "different_failure",
                "task_pass": False,
                "completed": False,
                "stop_reason": "pretend_stop",
                "evaluation_center_m": [-99.0, -99.0, -99.0],
            }
        )
        poisoned_path.write_text(json.dumps(poisoned_result), encoding="utf-8")
        poisoned_plan_path = poisoned_path.with_name("plan.json")
        poisoned_plan = json.loads(poisoned_plan_path.read_text(encoding="utf-8"))
        poisoned_plan["config"]["cup_spawn_offset_m"] = [-77.0, -77.0, -77.0]
        poisoned_plan["runtime_overrides"] = {"grid_point": [-500.0, 500.0]}
        poisoned_plan_path.write_text(json.dumps(poisoned_plan), encoding="utf-8")

        clean = self.service.execute("analyze_cup_alignment", {"path": str(clean_path)})
        poisoned = self.service.execute(
            "analyze_cup_alignment", {"path": str(poisoned_path)}
        )

        self.assertTrue(clean.ok, clean.error)
        self.assertTrue(poisoned.ok, poisoned.error)
        self.assertEqual(
            json.loads(clean.output)["proposal"],
            json.loads(poisoned.output)["proposal"],
        )

    def test_analyze_cup_alignment_reuses_safe_path_and_size_boundary(self) -> None:
        path = self._write_alignment_simulation("safe-alignment/isaac")
        linked_dir = self.root / "linked-alignment" / "isaac"
        linked_dir.mkdir(parents=True)
        linked = linked_dir / "result.json"
        linked.symlink_to(path)
        linked_result = self.service.execute(
            "analyze_cup_alignment", {"path": str(linked)}
        )
        self.assertEqual(linked_result.status, "rejected")
        self.assertIn("symlink_not_allowed", linked_result.error or "")

        outside_dir = self.root.parent / f"{self.root.name}-alignment-outside"
        outside_dir.mkdir()
        outside = outside_dir / "result.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            outside_result = self.service.execute(
                "analyze_cup_alignment", {"path": str(outside)}
            )
            self.assertEqual(outside_result.status, "rejected")
            self.assertIn("path_not_allowed", outside_result.error or "")
        finally:
            outside.unlink()
            outside_dir.rmdir()

        oversized_dir = self.root / "oversized-alignment" / "isaac"
        oversized_dir.mkdir(parents=True)
        oversized = oversized_dir / "result.json"
        with oversized.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        size_result = self.service.execute(
            "analyze_cup_alignment", {"path": str(oversized)}
        )
        self.assertEqual(size_result.status, "rejected")
        self.assertIn("simulation_file_too_large", size_result.error or "")

    def test_analyze_cup_alignment_requires_simulator_ground_truth_without_hardware(self) -> None:
        hardware_path = self._write_alignment_simulation("alignment-hardware/isaac")
        hardware_doc = json.loads(hardware_path.read_text(encoding="utf-8"))
        hardware_doc["hardware_accessed"] = True
        hardware_path.write_text(json.dumps(hardware_doc), encoding="utf-8")
        hardware = self.service.execute(
            "analyze_cup_alignment", {"path": str(hardware_path)}
        )
        self.assertEqual(hardware.status, "rejected")
        self.assertIn("hardware_accessed", hardware.error or "")

        camera_path = self._write_alignment_simulation("alignment-camera/isaac")
        camera_doc = json.loads(camera_path.read_text(encoding="utf-8"))
        camera_doc["observation_source"] = "rgb_camera"
        camera_path.write_text(json.dumps(camera_doc), encoding="utf-8")
        camera = self.service.execute(
            "analyze_cup_alignment", {"path": str(camera_path)}
        )
        self.assertEqual(camera.status, "rejected")
        self.assertIn("simulator_ground_truth_not_rgb", camera.error or "")

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
                "recording_check",
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
        check = summary["recording_check"]
        self.assertEqual(check["detector"], "joint_time_v2")
        self.assertEqual(check["cause_status"], "undetermined")
        self.assertEqual(check["coverage"]["observation_freshness"], "not_assessable")
        self.assertEqual(check["issue_count"], 1)
        self.assertEqual(check["issues"][0]["kind"], "joint_observation_stagnation")

    def test_analyze_trajectory_caps_v2_candidates_and_preserves_total_count(self) -> None:
        path = self._write_trajectory()
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        values[0]["joint_names"] = [f"joint_{i}" for i in range(12)]
        for row in values[1:]:
            row["q_cmd"] *= 12
            row["q_obs"] *= 12
        path.write_text("\n".join(json.dumps(value) for value in values) + "\n", encoding="utf-8")

        result = self.service.execute("analyze_trajectory", {"path": str(path)})

        self.assertTrue(result.ok)
        self.assertEqual(self.runner.calls, [])
        check = json.loads(result.output)["recording_check"]
        self.assertEqual(check["issue_count"], 12)
        self.assertEqual(len(check["issues"]), 8)
        self.assertTrue(check["issues_truncated"])

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
