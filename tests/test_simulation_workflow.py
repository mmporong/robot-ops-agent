from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.cli import main as cli_main  # noqa: E402
from robot_ops.replay.artifacts import sha256_file  # noqa: E402
from robot_ops.replay.profile import Profile  # noqa: E402
from robot_ops.simulation_workflow import build_report, run_fresh  # noqa: E402


STAGES = ("normal", "fault", "recovery")


class SimulationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.report = self.root / "report"
        self._write_three_runs()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_three_runs(self) -> None:
        for stage in STAGES:
            run_id = f"run-{stage}"
            directory = self.runs / "artifacts" / "runs" / run_id
            isaac = directory / "isaac"
            isaac.mkdir(parents=True)
            has_contact = stage != "normal"
            recovered = stage == "recovery"
            result = {
                "samples": [
                    {
                        "time_s": 0.0,
                        "phase": "APPROACH",
                        "attempt": 0,
                        "contact_force_n": [0.04 if has_contact else 0.0, 0.0],
                        "cup_position_m": [0.0, -0.005 if has_contact else 0.0, 0.4],
                    },
                    {
                        "time_s": 0.1,
                        "phase": "CLOSE",
                        "attempt": 1 if recovered else 0,
                        "contact_force_n": [0.1, 0.0],
                        "cup_position_m": [0.0, -0.005 if has_contact else 0.0, 0.4],
                    },
                ]
            }
            plan = {
                "config": {
                    "cup_center_m": [0.0, 0.0, 0.4],
                    "minimum_contact_force_n": 0.02,
                }
            }
            result_path = isaac / "result.json"
            plan_path = isaac / "plan.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            video = directory / "cup_contact.mp4"
            video.write_bytes(f"video-{stage}".encode())
            response = {
                "result_path": str(result_path),
                "video": {"sha256": sha256_file(video)},
            }
            (directory / "run.response.json").write_text(
                json.dumps(response), encoding="utf-8"
            )
            record = {
                "conditions": {
                    stage: {
                        "cells": [
                            {
                                "status": "ok",
                                "run_id": run_id,
                                "video_path": str(video),
                                "verdict": "pass" if stage != "fault" else "fail",
                            }
                        ]
                    }
                },
                "provenance": {"source_commit": "a" * 40},
                "gpu_check": {"status": "ready"},
            }
            (self.runs / f"{stage}.json").parent.mkdir(parents=True, exist_ok=True)
            (self.runs / f"{stage}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )

    def _analysis(self, stage: str, result_path: Path) -> dict[str, object]:
        has_contact = stage != "normal"
        recovered = stage == "recovery"
        issues = (
            [
                {
                    "time_s": 0.0,
                    "phase": "APPROACH",
                    "attempt": 0,
                    "maximum_force_n": 0.04,
                    "threshold_n": 0.02,
                    "reason_ko": "닫기 전 접촉",
                }
            ]
            if has_contact
            else []
        )
        return {
            "schema": "robot-ops-simulation-inspection/1",
            "status": "review_required" if has_contact else "no_issue_in_checked_scope",
            "source": {
                "result_sha256": sha256_file(result_path),
                "plan_sha256": sha256_file(result_path.with_name("plan.json")),
            },
            "runtime": {
                "sim_time_s": 0.1,
                "recovered": recovered,
                "time_axis_agrees": True,
                "final_attempt": 1 if recovered else 0,
                "final_attempt_premature_contact_count": 0,
            },
            "stored_outcome": {"task_pass": stage != "fault"},
            "premature_contact_check": {
                "issue_count": len(issues),
                "threshold_n": 0.02,
                "issues": issues,
            },
            "consistency": {
                "stored_event_agrees_with_recheck": True,
                "stop_reason_agrees_with_recheck": True,
            },
        }

    async def _inspect(self, path: Path, allowed: Path, audit: Path) -> dict[str, object]:
        self.assertEqual(allowed, self.runs.resolve() / "artifacts/runs")
        audit.parent.mkdir(parents=True, exist_ok=True)
        trace_id = path.parents[1].name
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"layer": "mcp", "phase": "started", "trace_id": trace_id,
                                     "arguments": {"path": str(path)}}) + "\n")
            handle.write(json.dumps({"layer": "mcp", "phase": "completed", "trace_id": trace_id,
                                     "ok": True}) + "\n")
        stage = path.parents[1].name.removeprefix("run-")
        return self._analysis(stage, path)

    def _build(self) -> dict[str, object]:
        with patch(
            "robot_ops.simulation_workflow.inspect_via_mcp", side_effect=self._inspect
        ) as inspect:
            evidence = build_report(self.runs, self.report)
        self.assertEqual(inspect.await_count, 3)
        return evidence

    def test_report_output_does_not_mix_old_audits_or_overwrite_existing_files(self) -> None:
        self.report.mkdir()
        preserved = self.report / "mcp-audit.jsonl"
        preserved.write_text("previous run\n", encoding="utf-8")
        with patch("robot_ops.simulation_workflow.inspect_via_mcp") as inspect:
            with self.assertRaisesRegex(ValueError, "비어 있는 출력"):
                build_report(self.runs, self.report)
            inspect.assert_not_called()
        self.assertEqual(preserved.read_text(), "previous run\n")

    def test_build_report_separates_normal_fault_and_recovery_outcomes(self) -> None:
        evidence = self._build()

        stages = {stage["id"]: stage for stage in evidence["stages"]}
        self.assertTrue(stages["normal"]["analysis"]["stored_outcome"]["task_pass"])
        self.assertEqual(
            stages["normal"]["analysis"]["premature_contact_check"]["issue_count"], 0
        )
        self.assertFalse(stages["fault"]["analysis"]["stored_outcome"]["task_pass"])
        self.assertGreater(
            stages["fault"]["analysis"]["premature_contact_check"]["issue_count"], 0
        )
        self.assertTrue(stages["recovery"]["analysis"]["runtime"]["recovered"])
        self.assertTrue(stages["recovery"]["analysis"]["stored_outcome"]["task_pass"])

    def test_build_report_rejects_result_hash_mismatch(self) -> None:
        original = self._inspect

        async def wrong_hash(path: Path, allowed: Path, audit: Path) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            analysis["source"]["result_sha256"] = "0" * 64
            return analysis

        with patch("robot_ops.simulation_workflow.inspect_via_mcp", side_effect=wrong_hash):
            with self.assertRaisesRegex(ValueError, "원본 파일"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_plan_hash_mismatch(self) -> None:
        original = self._inspect

        async def wrong_hash(path: Path, allowed: Path, audit: Path) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            analysis["source"]["plan_sha256"] = "0" * 64
            return analysis

        with patch("robot_ops.simulation_workflow.inspect_via_mcp", side_effect=wrong_hash):
            with self.assertRaisesRegex(ValueError, "원본 파일"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_video_hash_mismatch(self) -> None:
        response_path = (
            self.runs / "artifacts/runs/run-normal/run.response.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["video"]["sha256"] = "0" * 64
        response_path.write_text(json.dumps(response), encoding="utf-8")

        with patch("robot_ops.simulation_workflow.inspect_via_mcp", side_effect=self._inspect):
            with self.assertRaisesRegex(ValueError, "녹화 파일 해시"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_result_path_mismatch(self) -> None:
        response_path = (
            self.runs / "artifacts/runs/run-normal/run.response.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["result_path"] = str(self.root / "other.json")
        response_path.write_text(json.dumps(response), encoding="utf-8")

        with patch("robot_ops.simulation_workflow.inspect_via_mcp", side_effect=self._inspect):
            with self.assertRaisesRegex(ValueError, "원본·영상 경로"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_run_id_traversal(self) -> None:
        record_path = self.runs / "normal.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["conditions"]["normal"]["cells"][0]["run_id"] = "../fault"
        record_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "실행 ID"):
            build_report(self.runs, self.report)

    def test_build_report_rejects_unexpected_stage_transition(self) -> None:
        original = self._inspect

        async def wrong_transition(
            path: Path, allowed: Path, audit: Path
        ) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            if path.parents[1].name == "run-fault":
                analysis["stored_outcome"]["task_pass"] = True
            return analysis

        with patch(
            "robot_ops.simulation_workflow.inspect_via_mcp", side_effect=wrong_transition
        ):
            with self.assertRaisesRegex(ValueError, "기대한 정상→조기 접촉→복구"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_recovery_with_contact_on_final_attempt(self) -> None:
        original = self._inspect

        async def final_attempt_contact(
            path: Path, allowed: Path, audit: Path
        ) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            if path.parents[1].name == "run-recovery":
                analysis["runtime"]["final_attempt_premature_contact_count"] = 1
            return analysis

        with patch(
            "robot_ops.simulation_workflow.inspect_via_mcp",
            side_effect=final_attempt_contact,
        ):
            with self.assertRaisesRegex(ValueError, "마지막 시도의 조기 접촉"):
                build_report(self.runs, self.report)

    def test_build_report_rejects_failed_physics_time_validation(self) -> None:
        original = self._inspect

        async def invalid_time_axis(
            path: Path, allowed: Path, audit: Path
        ) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            if path.parents[1].name == "run-normal":
                analysis["runtime"]["time_axis_agrees"] = False
            return analysis

        with patch(
            "robot_ops.simulation_workflow.inspect_via_mcp",
            side_effect=invalid_time_axis,
        ):
            with self.assertRaisesRegex(ValueError, "물리 시간 진행"):
                build_report(self.runs, self.report)

    def test_build_report_escapes_script_closing_text_in_embedded_json(self) -> None:
        original = self._inspect

        async def injected_text(
            path: Path, allowed: Path, audit: Path
        ) -> dict[str, object]:
            analysis = await original(path, allowed, audit)
            analysis["probe"] = "</script><script>alert(1)</script>"
            return analysis

        with patch(
            "robot_ops.simulation_workflow.inspect_via_mcp", side_effect=injected_text
        ):
            build_report(self.runs, self.report)

        html = (self.report / "index.html").read_text(encoding="utf-8")
        embedded = html.split('id="sim-proof-data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<script>", embedded)
        self.assertIn(r"\u003c/script>", embedded)

    def test_cli_from_runs_reports_missing_input_as_structured_error(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "sim-check",
                    "--from-runs",
                    str(self.root / "missing"),
                    "--out",
                    str(self.report),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "blocked")

    def test_cli_from_runs_reports_malformed_input_as_structured_error(self) -> None:
        (self.runs / "normal.json").write_text("{broken", encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "sim-check",
                    "--from-runs",
                    str(self.runs),
                    "--out",
                    str(self.report),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "blocked")

    def test_cli_reports_invalid_simulation_result_without_traceback(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(
                [
                    "sim-check",
                    "--from-runs",
                    str(self.runs),
                    "--out",
                    str(self.report),
                ]
            )

        response = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(response["status"], "blocked")
        self.assertIn("MCP 검사", response["error"])
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertTrue((self.report / "mcp-audit.jsonl").is_file())

    def test_run_fresh_rejects_output_inside_source_repository_without_mutation(self) -> None:
        source = self.root / "source-repo"
        source.mkdir()
        sentinel = source / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        profile = Profile(
            path=self.root / "profile.toml",
            base_dir=self.root,
            project={"pinned_commit": "a" * 40},
            adapter_entry=self.root / "adapter.py",
            interpreters={},
            tasks={},
            allowlist_scripts=(),
            allowlist_imports=(),
            limits={},
            artifacts_root=self.root / "artifacts",
            report_dir=self.root / "report-output",
            delete_patterns=(),
            source_repo=source,
        )

        with patch("robot_ops.simulation_workflow.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "원천 저장소 안"):
                run_fresh(profile, source / "generated")

        run.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((source / "generated").exists())


if __name__ == "__main__":
    unittest.main()
