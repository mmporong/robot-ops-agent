from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from robot_ops.replay.field_eval import input_identity
from robot_ops.replay.recording_check_evaluation import run, union_joint_issues


def _write_trajectory(path: Path) -> tuple[str, str]:
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "so101",
        "arm": "left",
        "joint_names": ["shoulder_pan", "shoulder_lift", "elbow", "wrist", "roll"],
        "units": "rad",
        "fps": 10,
        "source": {"kind": "fixture", "path": "fixture", "episode": 0, "sha256": "0" * 64},
        "origin": "real_recording",
    }
    rows = [{
        "t": i / 10,
        "q_cmd": [float(i), 0.0, 0.0, 0.0, 0.0],
        "q_obs": [0.0, 0.0, 0.0, 0.0, 0.0],
        "gripper_cmd": 0.0,
        "gripper_obs": 0.0,
    } for i in range(6)]
    payload = "\n".join(json.dumps(item) for item in [header, *rows]) + "\n"
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest(), input_identity(header, rows)


def _manifest(path: Path, digest: str, identity: str, *, duplicate: bool = False) -> dict:
    entry = {
        "id": "fixture--ep0",
        "dataset": "fixture",
        "episode": 0,
        "frame_count": 6,
        "duration_s": 0.5,
        "identity_sha256": identity,
        "prior_inventory_match": True,
        "trajectory_path": str(path),
        "trajectory_sha256": digest,
    }
    episodes = [entry]
    if duplicate:
        episodes.append(entry | {"id": "fixture-copy--ep0", "duplicate_of": entry["id"]})
    return {"schema": "robot-ops-field-corpus/1", "dataset_count": 1, "errors": [], "episodes": episodes}


class JointIssueUnionTest(unittest.TestCase):
    def test_merges_overlapping_joints_and_adjacent_ranges_by_frame_union(self) -> None:
        issues = [
            {"kind": "joint_observation_stagnation", "joint": "a", "frame_start": 2, "frame_end": 5},
            {"kind": "joint_observation_stagnation", "joint": "b", "frame_start": 4, "frame_end": 7},
            {"kind": "source_identity_stagnation", "joint": "source", "frame_start": 1, "frame_end": 9},
            {"kind": "joint_observation_stagnation", "joint": "a", "frame_start": 8, "frame_end": 8},
            {"kind": "joint_observation_stagnation", "joint": "a", "frame_start": 11, "frame_end": 12},
        ]

        self.assertEqual(union_joint_issues(issues), [
            {"frame_start": 2, "frame_end": 8},
            {"frame_start": 11, "frame_end": 12},
        ])


class DevelopmentRegressionTest(unittest.TestCase):
    def test_reports_known_comparison_without_accuracy_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))

        self.assertEqual(result["scope"], "known_development_regression")
        self.assertFalse(result["field_validated"])
        self.assertEqual(result["protocol"]["challenge_count"], 17)
        self.assertEqual(result["aggregate"]["v1"]["event_hits"], 4)
        self.assertEqual(result["aggregate"]["v2"]["event_hits"], 10)
        self.assertEqual(result["aggregate"]["v2"]["event_total"], 11)
        self.assertEqual(result["aggregate"]["v2"]["false_positive_frames"], 1)
        self.assertIn("prospective_holdout_performance", result["unmeasured"])
        self.assertEqual(result["cause_status"], "undetermined")

    def test_discloses_unknown_quantization_warning_and_known_resolution_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))

        probes = {probe["id"]: probe for probe in result["quantization_probes"]}
        self.assertTrue(probes["normal_coarse_quantization_unknown"]["warning"])
        self.assertGreater(probes["normal_coarse_quantization_unknown"]["v2_per_joint_issue_count"], 0)
        self.assertFalse(probes["normal_coarse_quantization_known"]["warning"])
        self.assertEqual(probes["normal_coarse_quantization_known"]["v2_per_joint_issue_count"], 0)

    def test_short_hold_is_explicit_intentional_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp))

        short = next(case for case in result["synthetic"] if case["id"] == "fault_two_frame_hold")
        self.assertEqual(short["metrics"]["v2"]["event_missed"], 1)
        self.assertEqual(result["intentional_below_minimum"]["expected_behavior"], "miss")


class CorpusEvaluationTest(unittest.TestCase):
    def test_counts_only_unique_unlabeled_recordings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            digest, identity = _write_trajectory(trajectory)
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps(_manifest(trajectory, digest, identity, duplicate=True)), encoding="utf-8")

            result = run(root / "out", corpus_path=corpus)

        retrospective = result["retrospective"]
        self.assertEqual(retrospective["raw_episodes"], 2)
        self.assertEqual(retrospective["unique_episodes"], 1)
        self.assertEqual(retrospective["duplicate_episodes"], 1)
        self.assertFalse(retrospective["labels_available"])
        self.assertFalse(retrospective["accuracy_computed"])
        self.assertEqual(retrospective["records"][0]["label"], "unknown")
        self.assertEqual(retrospective["alert_counts"]["v2"]["episodes_with_alert"], 1)

    def test_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _, identity = _write_trajectory(trajectory)
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps(_manifest(trajectory, "f" * 64, identity)), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "해시 불일치"):
                run(root / "out", corpus_path=corpus)


if __name__ == "__main__":
    unittest.main()
