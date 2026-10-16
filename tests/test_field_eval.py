from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from robot_ops.replay.divergence import detect_freezes
from robot_ops.replay.field_eval import (
    build_challenges,
    input_identity,
    repeat_baseline,
    run,
    score_events,
)


def _row(
    frame: int,
    *,
    q_obs: float,
    gripper_obs: float = 0.0,
    q_cmd: float | None = None,
) -> dict[str, object]:
    return {
        "t": frame / 10.0,
        "q_cmd": [float(frame if q_cmd is None else q_cmd)],
        "q_obs": [float(q_obs)],
        "gripper_cmd": float(frame),
        "gripper_obs": float(gripper_obs),
    }


def _write_trajectory(
    path: Path,
    n_frames: int = 6,
    *,
    stationary_observation: bool = False,
) -> tuple[str, str]:
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "so101",
        "arm": "left",
        "joint_names": ["shoulder_pan", "shoulder_lift", "elbow", "wrist", "roll"],
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
    rows = []
    for i in range(n_frames):
        value = 0.0 if stationary_observation else float(i)
        row = _row(i, q_obs=value)
        row["q_cmd"] = [float(i), 0.0, 0.0, 0.0, 0.0]
        row["q_obs"] = [value, 0.0, 0.0, 0.0, 0.0]
        rows.append(row)
    payload = "\n".join(json.dumps(item) for item in [header, *rows]) + "\n"
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest(), input_identity(header, rows)


def _entry(
    trajectory: Path,
    sha256: str,
    identity: str,
    *,
    entry_id: str = "fixture--ep0",
    frame_count: int = 6,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "dataset": entry_id.split("--", 1)[0],
        "episode": 0,
        "frame_count": frame_count,
        "duration_s": (frame_count - 1) / 10,
        "identity_sha256": identity,
        "prior_inventory_match": True,
        "trajectory_path": str(trajectory),
        "trajectory_sha256": sha256,
    }


def _manifest(
    trajectory: Path,
    sha256: str,
    identity: str,
    *,
    duplicate: bool = False,
) -> dict[str, object]:
    entry = _entry(trajectory, sha256, identity)
    episodes = [entry]
    if duplicate:
        episodes.append(entry | {"id": "fixture-copy--ep0", "duplicate_of": entry["id"]})
    return {
        "schema": "robot-ops-field-corpus/1",
        "dataset_count": 2 if duplicate else 1,
        "errors": [],
        "episodes": episodes,
    }


class RepeatBaselineTest(unittest.TestCase):
    def test_reports_three_consecutive_unchanged_observation_transitions(self) -> None:
        rows = [
            _row(0, q_obs=0.0),
            _row(1, q_obs=1.0),
            _row(2, q_obs=1.0),
            _row(3, q_obs=1.0),
            _row(4, q_obs=1.0),
            _row(5, q_obs=2.0),
        ]

        self.assertEqual(
            repeat_baseline(rows),
            [{
                "frame_start": 2,
                "frame_end": 4,
                "n_frames": 3,
                "t_start_s": 0.2,
                "t_end_s": 0.4,
            }],
        )

    def test_ignores_command_changes_when_observation_is_repeated(self) -> None:
        rows = [_row(i, q_obs=7.0, q_cmd=100.0 * i) for i in range(4)]

        events = repeat_baseline(rows)

        self.assertEqual([(event["frame_start"], event["frame_end"]) for event in events], [(1, 3)])

    def test_gripper_motion_breaks_full_observation_repeat(self) -> None:
        rows = [_row(i, q_obs=7.0, gripper_obs=float(i)) for i in range(6)]

        self.assertEqual(repeat_baseline(rows), [])


class ScoreEventsTest(unittest.TestCase):
    def test_counts_frame_confusion_over_transition_frames(self) -> None:
        score = score_events(
            expected=[(2, 4)],
            predicted=[{"frame_start": 3, "frame_end": 5}],
            n_frames=10,
        )

        self.assertEqual(
            {key: score[key] for key in ("frame_tp", "frame_fp", "frame_fn", "frame_tn")},
            {"frame_tp": 2, "frame_fp": 1, "frame_fn": 1, "frame_tn": 5},
        )
        self.assertAlmostEqual(score["frame_precision"], 2 / 3)
        self.assertAlmostEqual(score["frame_recall"], 2 / 3)

    def test_merged_prediction_counts_clean_gap_as_false_positive(self) -> None:
        score = score_events(
            expected=[(2, 3), (6, 7)],
            predicted=[{"frame_start": 2, "frame_end": 7}],
            n_frames=9,
        )

        self.assertEqual(score["frame_tp"], 4)
        self.assertEqual(score["frame_fp"], 2)
        self.assertEqual(score["frame_fn"], 0)

    def test_event_matching_does_not_reuse_one_expected_event(self) -> None:
        score = score_events(
            expected=[(4, 8)],
            predicted=[
                {"frame_start": 4, "frame_end": 8},
                {"frame_start": 5, "frame_end": 7},
            ],
            n_frames=12,
        )

        self.assertEqual(score["event_expected"], 1)
        self.assertEqual(score["event_predicted"], 2)
        self.assertEqual(score["event_hits"], 1)
        self.assertEqual(score["event_missed"], 0)
        self.assertEqual(len(score["event_matching"]), 1)
        self.assertEqual(score["event_matching"][0]["predicted_index"], 0)

    def test_event_matching_maximizes_one_to_one_hits_before_iou(self) -> None:
        score = score_events(
            expected=[(1, 5), (6, 10)],
            predicted=[
                {"frame_start": 3, "frame_end": 8},
                {"frame_start": 1, "frame_end": 1},
            ],
            n_frames=12,
        )

        self.assertEqual(score["event_hits"], 2)
        self.assertEqual(score["event_missed"], 0)
        self.assertEqual(
            {(match["expected_index"], match["predicted_index"]) for match in score["event_matching"]},
            {(0, 1), (1, 0)},
        )

    def test_matching_reports_iou_boundary_errors_and_confirmation_delay(self) -> None:
        score = score_events(
            expected=[(2, 4)],
            predicted=[{"frame_start": 3, "frame_end": 5}],
            n_frames=10,
        )

        self.assertEqual(
            score["event_matching"],
            [{
                "expected_index": 0,
                "predicted_index": 0,
                "iou": 0.5,
                "start_error_frames": 1,
                "end_error_frames": 1,
                "minimum_confirmation_delay_frames": 3,
            }],
        )

    def test_undefined_frame_rates_are_none_when_denominator_is_zero(self) -> None:
        score = score_events(expected=[], predicted=[], n_frames=5)

        self.assertIsNone(score["frame_precision"])
        self.assertIsNone(score["frame_recall"])
        self.assertEqual(score["frame_tn"], 4)


class ChallengeCorpusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.challenges = {challenge["id"]: challenge for challenge in build_challenges()}

    def test_corpus_contains_required_normal_fault_and_ambiguity_cases(self) -> None:
        self.assertLessEqual(
            {
                "normal_stationary",
                "normal_fast_tracking",
                "fault_fast_full_hold",
                "fault_slow_full_hold",
                "fault_arm_hold_gripper_live",
                "ambiguous_stale",
                "ambiguous_physical_block",
            },
            set(self.challenges),
        )

    def test_current_detector_rejects_normal_stationary_and_tracking_cases(self) -> None:
        for challenge_id in ("normal_stationary", "normal_fast_tracking"):
            with self.subTest(challenge=challenge_id):
                challenge = self.challenges[challenge_id]
                self.assertEqual(challenge["expected"], [])
                self.assertEqual(detect_freezes(challenge["rows"]), [])

    def test_repeat_baseline_false_alarms_on_normal_stationary_case(self) -> None:
        challenge = self.challenges["normal_stationary"]

        self.assertNotEqual(repeat_baseline(challenge["rows"]), [])

    def test_fast_full_hold_is_detected(self) -> None:
        challenge = self.challenges["fault_fast_full_hold"]

        predictions = repeat_baseline(challenge["rows"])
        score = score_events(challenge["expected"], predictions, len(challenge["rows"]))

        self.assertNotEqual(challenge["expected"], [])
        self.assertEqual(score["event_hits"], 1)
        self.assertEqual(score["event_missed"], 0)

    def test_slow_hold_is_found_by_baseline_but_missed_by_current_threshold(self) -> None:
        challenge = self.challenges["fault_slow_full_hold"]

        self.assertNotEqual(challenge["expected"], [])
        self.assertNotEqual(repeat_baseline(challenge["rows"]), [])
        self.assertEqual(detect_freezes(challenge["rows"]), [])

    def test_arm_hold_with_live_gripper_truth_exists_even_when_baseline_misses_it(self) -> None:
        challenge = self.challenges["fault_arm_hold_gripper_live"]

        self.assertNotEqual(challenge["expected"], [])
        self.assertEqual(repeat_baseline(challenge["rows"]), [])

    def test_ambiguous_pair_has_identical_input_but_different_causes(self) -> None:
        stale = self.challenges["ambiguous_stale"]
        blocked = self.challenges["ambiguous_physical_block"]

        self.assertEqual(stale["scope"], "cause_ambiguity")
        self.assertEqual(blocked["scope"], "cause_ambiguity")
        self.assertEqual(stale["header"], blocked["header"])
        self.assertEqual(stale["rows"], blocked["rows"])
        self.assertEqual(stale["expected"], blocked["expected"])
        self.assertNotEqual(stale["cause"], blocked["cause"])


class FieldEvaluationBoundaryTest(unittest.TestCase):
    def test_rejects_corpus_with_normalization_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps({
                "schema": "robot-ops-field-corpus/1",
                "errors": [{"stage": "normalize", "error": "broken"}],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "corpus"):
                run(corpus, root / "out")

    def test_rejects_trajectory_when_manifest_hash_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _write_trajectory(trajectory)
            corpus = root / "manifest.json"
            corpus.write_text(
                json.dumps(_manifest(trajectory, "f" * 64, "0" * 64)),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "해시 불일치"):
                run(corpus, root / "out")

    def test_unlabeled_real_recording_and_duplicate_do_not_gain_accuracy_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            digest, identity = _write_trajectory(trajectory)
            corpus = root / "manifest.json"
            corpus.write_text(
                json.dumps(_manifest(trajectory, digest, identity, duplicate=True)),
                encoding="utf-8",
            )

            result = run(corpus, root / "out")

            self.assertEqual(result["deployment_verdict"], "not_validated")
            self.assertEqual(result["corpus"]["raw_episodes"], 2)
            self.assertEqual(result["corpus"]["unique_episodes"], 1)
            self.assertEqual(result["corpus"]["duplicate_episodes"], 1)
            self.assertEqual(result["retrospective"][0]["label"], "unknown")
            self.assertNotIn("metrics", result["retrospective"][0])
            self.assertIn("real_world_precision_recall", result["unmeasured"])

    def test_cause_ambiguity_is_reported_separately_from_detection_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            digest, identity = _write_trajectory(trajectory)
            corpus = root / "manifest.json"
            corpus.write_text(
                json.dumps(_manifest(trajectory, digest, identity)),
                encoding="utf-8",
            )

            result = run(corpus, root / "out")

            synthetic_ids = {case["id"] for case in result["synthetic"]}
            self.assertNotIn("ambiguous_stale", synthetic_ids)
            self.assertNotIn("ambiguous_physical_block", synthetic_ids)
            self.assertEqual(result["cause_ambiguity"]["rows_equal"], True)
            self.assertEqual(result["cause_ambiguity"]["predictions_equal"], True)
            self.assertEqual(result["cause_ambiguity"]["cause_labels_differ"], True)
            self.assertEqual(result["cause_ambiguity"]["root_cause_identifiable"], False)

    def test_rejects_duplicate_marker_when_target_has_different_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first_sha, first_identity = _write_trajectory(first)
            second_sha, second_identity = _write_trajectory(second, stationary_observation=True)
            manifest = {
                "schema": "robot-ops-field-corpus/1",
                "dataset_count": 2,
                "errors": [],
                "episodes": [
                    _entry(first, first_sha, first_identity, entry_id="first--ep0"),
                    _entry(second, second_sha, second_identity, entry_id="second--ep0")
                    | {"duplicate_of": "first--ep0"},
                ],
            }
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "중복 대상 identity 불일치"):
                run(corpus, root / "out")

    def test_rejects_identical_input_without_duplicate_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            digest, identity = _write_trajectory(trajectory)
            manifest = {
                "schema": "robot-ops-field-corpus/1",
                "dataset_count": 2,
                "errors": [],
                "episodes": [
                    _entry(trajectory, digest, identity, entry_id="first--ep0"),
                    _entry(trajectory, digest, identity, entry_id="second--ep0"),
                ],
            }
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "중복 표시가 없는 동일 입력"):
                run(corpus, root / "out")

    def test_injection_summary_distinguishes_effective_and_noop_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracking = root / "tracking.jsonl"
            stationary = root / "stationary.jsonl"
            tracking_sha, tracking_identity = _write_trajectory(tracking, n_frames=90)
            stationary_sha, stationary_identity = _write_trajectory(
                stationary,
                n_frames=90,
                stationary_observation=True,
            )
            manifest = {
                "schema": "robot-ops-field-corpus/1",
                "dataset_count": 2,
                "errors": [],
                "episodes": [
                    _entry(
                        tracking,
                        tracking_sha,
                        tracking_identity,
                        entry_id="tracking--ep0",
                        frame_count=90,
                    ),
                    _entry(
                        stationary,
                        stationary_sha,
                        stationary_identity,
                        entry_id="stationary--ep0",
                        frame_count=90,
                    ),
                ],
            }
            corpus = root / "manifest.json"
            corpus.write_text(json.dumps(manifest), encoding="utf-8")

            result = run(corpus, root / "out")
            summary = json.loads((root / "out" / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(
                [item["observation_changed_frames"] for item in result["real_injection_delta"]],
                [30, 0],
            )
            for method in ("current", "repeat_baseline"):
                self.assertEqual(summary["real_injection_summary"][method]["episodes"], 2)
                self.assertEqual(summary["real_injection_summary"][method]["effective_mutations"], 1)
                self.assertEqual(summary["real_injection_summary"][method]["noop_mutations"], 1)


if __name__ == "__main__":
    unittest.main()
