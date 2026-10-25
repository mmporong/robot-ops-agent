from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robot_ops.incident_evaluation import build_challenges, evaluate_manifest


def _trajectory(
    path: Path,
    *,
    offset: float = 0.0,
    schedule: list[dict] | None = None,
    start_s: float = 0.0,
) -> str:
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "evaluation-fixture",
        "arm": "left",
        "joint_names": ["joint"],
        "units": "rad",
        "fps": 10,
        "source": {"kind": "synthetic", "path": "fixture", "episode": 0, "sha256": "0" * 64},
        "origin": "fault_injection",
    }
    if schedule is not None:
        header["injection_schedule"] = schedule
    rows = [
        {"t": start_s + i / 10, "q_cmd": [offset + i / 100], "q_obs": [offset + i / 100]}
        for i in range(21)
    ]
    payload = "\n".join(json.dumps(item, sort_keys=True) for item in [header, *rows]) + "\n"
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest()


def _case(path: Path, digest: str, *, case_id: str = "case", split: str = "evaluation",
          group_id: str = "group", onset: float | None = 1.0,
          source: str = "synthetic_injection") -> dict:
    return {
        "id": case_id,
        "group_id": group_id,
        "split": split,
        "trajectory_path": str(path),
        "trajectory_sha256": digest,
        "expected_onset_s": onset,
        "label_source": source,
        "label_reference": "fixture schedule v1",
    }


def _write_manifest(path: Path, cases: list[dict], *, human_trials: list[dict] | None = None) -> None:
    document = {"schema": "robot-ops-incident-evaluation/1", "cases": cases}
    if human_trials is not None:
        document["human_trials"] = human_trials
    path.write_text(json.dumps(document), encoding="utf-8")


def _review(*, windows: list[dict] | None = None, duration_s: float = 2.0) -> dict:
    selected = windows or []
    return {
        "status": "review_required" if selected else "no_issue_in_checked_scope",
        "cause_status": "undetermined",
        "windows": selected,
        "summary": {
            "issue_count": len(selected),
            "window_count": len(selected),
            "selected_count": len(selected),
            "selected_duration_s": sum(item["end_s"] - item["start_s"] for item in selected),
            "duration_s": duration_s,
        },
        "trace": [],
    }


class ManifestValidationTest(unittest.TestCase):
    def test_rejects_expected_onset_outside_trajectory_time_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory, start_s=10.0)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest, onset=9.9)])

            with self.assertRaisesRegex(ValueError, "시간 범위 밖"):
                evaluate_manifest(manifest, root / "out")

    def test_rejects_trajectory_without_rows_with_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            _trajectory(trajectory)
            header = trajectory.read_text(encoding="utf-8").splitlines()[0] + "\n"
            trajectory.write_text(header, encoding="utf-8")
            digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest)])

            with self.assertRaisesRegex(ValueError, "행이 비어 있습니다"):
                evaluate_manifest(manifest, root / "out")

    def test_rejects_trajectory_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, "f" * 64)])

            with self.assertRaisesRegex(ValueError, "해시 불일치"):
                evaluate_manifest(manifest, root / "out")

    def test_rejects_unknown_case_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            entry = _case(trajectory, digest) | {"detector_result": "copied-label"}
            manifest = root / "manifest.json"
            _write_manifest(manifest, [entry])

            with self.assertRaisesRegex(ValueError, "허용되지 않은 키"):
                evaluate_manifest(manifest, root / "out")

    def test_rejects_same_group_across_development_and_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "dev.jsonl", root / "eval.jsonl"
            first_sha = _trajectory(first)
            second_sha = _trajectory(second, offset=1.0)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [
                _case(first, first_sha, case_id="dev", split="development", group_id="shared"),
                _case(second, second_sha, case_id="eval", split="evaluation", group_id="shared"),
            ])

            with self.assertRaisesRegex(ValueError, "group.*누수"):
                evaluate_manifest(manifest, root / "out")

    def test_rejects_identical_trajectory_identity_even_with_different_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "a.jsonl", root / "b.jsonl"
            first_sha = _trajectory(first)
            text = first.read_text(encoding="utf-8")
            second.write_text(text.replace("\n", " \n"), encoding="utf-8")
            second_sha = hashlib.sha256(second.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            _write_manifest(manifest, [
                _case(first, first_sha, case_id="a", split="development", group_id="a"),
                _case(second, second_sha, case_id="b", split="evaluation", group_id="b"),
            ])

            with self.assertRaisesRegex(ValueError, "동일.*trajectory identity"):
                evaluate_manifest(manifest, root / "out")


class EvaluationSemanticsTest(unittest.TestCase):
    def test_records_reproducibility_hashes_policy_and_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest)])

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review()):
                result = evaluate_manifest(manifest, root / "out")

            protocol = result["reproducibility"]
            self.assertEqual(protocol["manifest_sha256"], hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertEqual(
                set(protocol["source_sha256"]),
                {"incident_review", "recording_check", "recording_inspection", "incident_evaluation"},
            )
            self.assertEqual(protocol["policy"], "incident_review_v1")
            self.assertEqual(protocol["parameters"]["onset_tolerance_s"], 0.2)
            self.assertEqual(
                protocol["protocol_sha256"],
                hashlib.sha256((root / "out" / "protocol.json").read_bytes()).hexdigest(),
            )

    def test_writes_protocol_before_running_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest)])
            out = root / "out"

            with patch("robot_ops.incident_evaluation._run_review", side_effect=RuntimeError("stop")):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    evaluate_manifest(manifest, out)

            self.assertTrue((out / "protocol.json").is_file())

    def test_selected_duration_uses_nonzero_trajectory_time_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory, start_s=10.0)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest, onset=10.5)])
            windows = [{"id": "1", "start_s": 10.25, "end_s": 10.75, "focus_s": 10.25}]

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review(windows=windows)):
                result = evaluate_manifest(manifest, root / "out")

        self.assertEqual(result["cases"][0]["selected_duration_s"], 0.5)
        self.assertEqual(result["cases"][0]["selected_time_ratio"], 0.25)

    def test_rejects_review_window_beyond_last_trajectory_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory, start_s=10.0)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest, onset=11.0)])
            windows = [{"id": "1", "start_s": 11.9, "end_s": 12.1, "focus_s": 11.9}]

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review(windows=windows)):
                with self.assertRaisesRegex(ValueError, "원시 시간이 올바르지 않습니다"):
                    evaluate_manifest(manifest, root / "out")

    def test_scores_onset_only_against_first_three_raw_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest, onset=1.0)])
            windows = [
                {"id": "1", "start_s": 0.0, "end_s": 0.1, "focus_s": 0.0},
                {"id": "2", "start_s": 0.2, "end_s": 0.3, "focus_s": 0.2},
                {"id": "3", "start_s": 0.4, "end_s": 0.5, "focus_s": 0.4,
                 "context_start_s": 0.0, "context_end_s": 1.2},
                {"id": "4", "start_s": 0.9, "end_s": 1.1, "focus_s": 0.9},
            ]

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review(windows=windows)):
                result = evaluate_manifest(manifest, root / "out")

        self.assertFalse(result["cases"][0]["onset_hit"])
        self.assertEqual(result["cases"][0]["evaluated_window_count"], 3)

    def test_reports_normal_case_flagging_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "normal.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest, onset=None)])
            windows = [{"id": "1", "start_s": 0.2, "end_s": 0.4, "focus_s": 0.2}]

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review(windows=windows)):
                result = evaluate_manifest(manifest, root / "out")

        self.assertTrue(result["cases"][0]["normal_case_flagged"])
        self.assertEqual(result["aggregate"]["evaluation"]["normal_flagged"], 1)
        self.assertNotIn("onset_hit", result["cases"][0])

    def test_synthetic_evaluation_is_not_field_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest)])

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review()):
                result = evaluate_manifest(manifest, root / "out")

        self.assertFalse(result["field_validated"])
        self.assertEqual(result["field_validation"]["status"], "not_measured")
        self.assertEqual(result["scope"]["evaluation_case_ids"], ["case"])

    def test_human_effect_uses_only_paired_reviewer_case_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            trials = [
                {"case_id": "case", "reviewer": "r1", "method": "manual", "duration_s": 30.0, "correct": True},
                {"case_id": "case", "reviewer": "r1", "method": "assisted", "duration_s": 20.0, "correct": True},
                {"case_id": "case", "reviewer": "r2", "method": "manual", "duration_s": 5.0, "correct": False},
            ]
            _write_manifest(manifest, [_case(trajectory, digest)], human_trials=trials)

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review()):
                result = evaluate_manifest(manifest, root / "out")

        effect = result["human_effect"]
        self.assertEqual(effect["paired_count"], 1)
        self.assertEqual(effect["median_duration_delta_s"], 10.0)
        self.assertEqual(effect["claim"], "association_only")
        self.assertEqual(effect["effect_status"], "not_measured")
        self.assertEqual(result["claims"]["human_time_savings"], "not_measured")

    def test_independent_labels_alone_do_not_claim_field_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [
                _case(trajectory, digest, source="independent_review")
            ])

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review()):
                result = evaluate_manifest(manifest, root / "out")

        self.assertFalse(result["field_validated"])
        self.assertEqual(result["field_validation"]["independent_evaluation_count"], 1)
        self.assertEqual(result["claims"]["field_holdout_performance"], "not_measured")

    def test_without_human_trials_time_effect_is_not_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "case.jsonl"
            digest = _trajectory(trajectory)
            manifest = root / "manifest.json"
            _write_manifest(manifest, [_case(trajectory, digest)])

            with patch("robot_ops.incident_evaluation._run_review", return_value=_review()):
                result = evaluate_manifest(manifest, root / "out")

        self.assertEqual(result["human_effect"]["status"], "not_measured")
        self.assertEqual(result["claims"]["human_time_savings"], "not_measured")


class ChallengeGenerationTest(unittest.TestCase):
    def test_builds_at_least_eight_deterministic_cases_with_schedule_based_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = build_challenges(root / "first")
            second = build_challenges(root / "second")
            first_doc = json.loads(first.read_text(encoding="utf-8"))
            second_doc = json.loads(second.read_text(encoding="utf-8"))

            self.assertGreaterEqual(len(first_doc["cases"]), 8)
            self.assertEqual(
                [(case["id"], case["expected_onset_s"]) for case in first_doc["cases"]],
                [(case["id"], case["expected_onset_s"]) for case in second_doc["cases"]],
            )
            for case in first_doc["cases"]:
                trajectory = Path(case["trajectory_path"])
                header = json.loads(trajectory.read_text(encoding="utf-8").splitlines()[0])
                schedule = header.get("injection_schedule", [])
                expected = schedule[0]["start_s"] if schedule else None
                self.assertEqual(case["expected_onset_s"], expected)

    def test_generated_challenges_run_through_actual_review_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = build_challenges(root / "challenge")

            result = evaluate_manifest(manifest, root / "result")

        self.assertEqual(result["scope"]["valid_case_count"], 9)
        self.assertEqual(result["aggregate"]["evaluation"]["status"], "measured")
        self.assertFalse(result["field_validated"])
        self.assertEqual(result["human_effect"]["status"], "not_measured")


if __name__ == "__main__":
    unittest.main()
