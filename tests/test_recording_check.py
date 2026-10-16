from __future__ import annotations

import math
import unittest

from robot_ops.replay.recording_check import inspect_recording
from robot_ops.replay.schema import SchemaError


def _header(*, fps: int = 10, gripper_units: str | None = None) -> dict:
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "test",
        "arm": "left",
        "joint_names": ["shoulder", "elbow"],
        "units": "rad",
        "fps": fps,
        "source": {"kind": "synthetic", "path": "test", "episode": 0, "sha256": "0" * 64},
        "origin": "fault_injection",
    }
    if gripper_units is not None:
        header["gripper_units"] = gripper_units
    return header


def _rows(*, fps: int = 10, seconds: float = 2.0, speed_deg_s: float = 5.0) -> list[dict]:
    result = []
    for index in range(int(fps * seconds) + 1):
        angle = speed_deg_s * index / fps
        result.append(
            {
                "t": index / fps,
                "q_cmd": [math.radians(angle), math.radians(angle / 2)],
                "q_obs": [math.radians(angle), math.radians(angle / 2)],
            }
        )
    return result


def _hold(rows: list[dict], start: int, end: int, joint: int = 0) -> None:
    value = rows[start - 1]["q_obs"][joint]
    for index in range(start, end + 1):
        rows[index]["q_obs"][joint] = value


class RecordingCheckTest(unittest.TestCase):
    def test_stationary_recording_is_not_flagged(self) -> None:
        result = inspect_recording(_header(), _rows(speed_deg_s=0.0))
        self.assertEqual(result["status"], "no_issue_in_checked_scope")
        self.assertEqual(result["issues"], [])

    def test_slow_normal_tracking_is_not_flagged(self) -> None:
        result = inspect_recording(_header(), _rows(speed_deg_s=2.0))
        self.assertEqual(result["issues"], [])

    def test_quantized_normal_tracking_is_not_flagged(self) -> None:
        rows = _rows(fps=100, speed_deg_s=2.0)
        for row in rows:
            degrees = math.degrees(row["q_obs"][0])
            row["q_obs"][0] = math.radians(round(degrees / 0.1) * 0.1)
        result = inspect_recording(_header(fps=100), rows)
        self.assertEqual(result["issues"], [])

    def test_known_one_degree_quantization_raises_command_threshold(self) -> None:
        rows = _rows(fps=10, speed_deg_s=1.0)
        for row in rows:
            degrees = math.degrees(row["q_obs"][0])
            row["q_obs"][0] = math.radians(round(degrees))
        header = _header()
        header["recording_check"] = {"observation_resolution_deg": {"shoulder": 1.0}}
        result = inspect_recording(header, rows)
        self.assertFalse(any(issue["joint"] == "shoulder" for issue in result["issues"]))
        self.assertEqual(result["parameters"]["effective_command_span_deg"]["shoulder"], 2.0)
        without_resolution = inspect_recording(_header(), rows)
        self.assertTrue(
            any(issue["joint"] == "shoulder" for issue in without_resolution["issues"])
        )
        self.assertFalse(without_resolution["parameters"]["field_validated"])

    def test_unknown_resolution_records_quantization_limit(self) -> None:
        result = inspect_recording(_header(), _rows())
        resolution = result["coverage"]["observation_resolution"]
        self.assertIn("shoulder", resolution["unknown_joints"])
        self.assertIn("구분할 수 없습니다", resolution["limitation_ko"])

    def test_short_stagnation_is_not_flagged(self) -> None:
        rows = _rows(speed_deg_s=20.0)
        _hold(rows, 5, 6)
        self.assertEqual(inspect_recording(_header(), rows)["issues"], [])

    def test_cumulative_observation_drift_breaks_run(self) -> None:
        rows = _rows(fps=100, speed_deg_s=3.0)
        for index, row in enumerate(rows):
            row["q_obs"][0] = math.radians(index * 0.02)
        issues = inspect_recording(_header(fps=100), rows)["issues"]
        self.assertFalse(any(issue["joint"] == "shoulder" for issue in issues))

    def test_partial_joint_stagnation_is_found_with_earliest_confirmation(self) -> None:
        rows = _rows(speed_deg_s=8.0)
        _hold(rows, 5, 12, joint=0)
        result = inspect_recording(_header(), rows)
        issue = next(issue for issue in result["issues"] if issue["joint"] == "shoulder")
        self.assertEqual(issue["kind"], "joint_observation_stagnation")
        self.assertEqual((issue["frame_start"], issue["frame_end"]), (5, 12))
        self.assertEqual(issue["n_frames"], 8)
        self.assertAlmostEqual(issue["duration_s"], 0.7)
        self.assertEqual(issue["confirmed_at_frame"], 7)
        self.assertEqual(issue["reference_frame"], 4)
        self.assertAlmostEqual(issue["reference_t_s"], rows[4]["t"])
        self.assertAlmostEqual(issue["t_start_s"], rows[5]["t"])
        self.assertAlmostEqual(issue["confirmation_t_s"], rows[7]["t"])
        self.assertAlmostEqual(issue["evidence_duration_s"], 0.8)
        self.assertTrue(issue["symptom_only"])
        self.assertGreater(issue["command_span"], 0.5)
        self.assertLessEqual(issue["observation_span"], 0.1)
        self.assertEqual(issue["unit"], "deg")
        self.assertEqual(result["cause_status"], "undetermined")

    def test_live_gripper_does_not_hide_arm_stagnation(self) -> None:
        rows = _rows(speed_deg_s=8.0)
        _hold(rows, 5, 12, joint=0)
        for index, row in enumerate(rows):
            row["gripper_cmd"] = row["gripper_obs"] = float(index)
        result = inspect_recording(_header(gripper_units="lerobot_raw"), rows)
        self.assertTrue(result["coverage"]["gripper_checked"])
        self.assertTrue(any(issue["joint"] == "shoulder" for issue in result["issues"]))
        self.assertFalse(any(issue["joint"] == "gripper" for issue in result["issues"]))

    def test_gripper_stagnation_is_checked_only_for_known_complete_units(self) -> None:
        rows = _rows(speed_deg_s=0.0)
        for index, row in enumerate(rows):
            row["gripper_cmd"] = float(index)
            row["gripper_obs"] = 7.0
        checked = inspect_recording(_header(gripper_units="lerobot_raw"), rows)
        grip = next(issue for issue in checked["issues"] if issue["joint"] == "gripper")
        self.assertEqual(grip["unit"], "lerobot_raw")
        unknown = inspect_recording(_header(gripper_units="degrees"), rows)
        self.assertFalse(unknown["coverage"]["gripper_checked"])
        self.assertFalse(any(issue["joint"] == "gripper" for issue in unknown["issues"]))
        rows[3]["gripper_obs"] = None
        missing = inspect_recording(_header(gripper_units="lerobot_raw"), rows)
        self.assertTrue(missing["coverage"]["gripper_checked"])
        self.assertEqual(missing["coverage"]["gripper_missing_frames"], [3])
        self.assertEqual(missing["coverage"]["gripper_checked_ranges"], [[0, 2], [4, 20]])

    def test_high_rate_stagnation_uses_elapsed_time(self) -> None:
        rows = _rows(fps=100, speed_deg_s=20.0)
        _hold(rows, 20, 60, joint=0)
        issues = inspect_recording(_header(fps=100), rows)["issues"]
        issue = next(issue for issue in issues if issue["joint"] == "shoulder")
        self.assertEqual(issue["confirmed_at_frame"], 49)
        self.assertAlmostEqual(issue["duration_s"], 0.4)
        self.assertAlmostEqual(issue["evidence_duration_s"], 0.41)

    def test_low_rate_recording_uses_adaptive_gap_limit(self) -> None:
        rows = _rows(fps=2, seconds=3.0, speed_deg_s=2.0)
        result = inspect_recording(_header(fps=2), rows)
        self.assertFalse(any(issue["kind"] == "sampling_gap" for issue in result["issues"]))
        self.assertEqual(result["coverage"]["sampling"]["effective_max_gap_s"], 1.25)

    def test_irregular_sampling_gap_is_reported_and_splits_holds(self) -> None:
        rows = _rows(speed_deg_s=8.0)
        rows[8]["t"] = 1.1
        for index in range(9, len(rows)):
            rows[index]["t"] = 1.1 + (index - 8) / 10
        result = inspect_recording(_header(), rows)
        gap = next(issue for issue in result["issues"] if issue["kind"] == "sampling_gap")
        self.assertEqual((gap["frame_start"], gap["frame_end"]), (8, 8))
        self.assertAlmostEqual(gap["duration_s"], 0.0)
        self.assertAlmostEqual(gap["evidence_duration_s"], 0.4)

    def test_output_records_checked_scope_and_freshness_limit(self) -> None:
        result = inspect_recording(_header(), _rows())
        self.assertEqual(result["schema"], "robot-ops-recording-check/1")
        self.assertEqual(result["detector"], "joint_time_v2")
        self.assertEqual(result["coverage"]["checked_joints"], ["shoulder", "elbow"])
        self.assertEqual(result["coverage"]["observation_freshness"], "not_assessable")
        self.assertEqual(result["coverage"]["freshness"]["status"], "not_assessable")
        self.assertEqual(result["cause_status"], "undetermined")

    def test_rejects_bad_schema_timestamps_and_parameters(self) -> None:
        with self.assertRaises(SchemaError):
            inspect_recording(dict(_header(), units="deg"), _rows())
        rows = _rows()
        rows[2]["t"] = rows[1]["t"]
        with self.assertRaisesRegex(ValueError, "엄격히 증가"):
            inspect_recording(_header(), rows)
        rows = _rows()
        rows[2]["t"] = float("nan")
        with self.assertRaises((SchemaError, ValueError)):
            inspect_recording(_header(), rows)
        with self.assertRaisesRegex(ValueError, "min_hold_s"):
            inspect_recording(_header(), _rows(), min_hold_s=0.0)
        with self.assertRaisesRegex(ValueError, "max_gap_s"):
            inspect_recording(_header(), _rows(), max_gap_s=float("inf"))
        header = _header()
        header["recording_check"] = {"observation_resolution_deg": {"unknown": 1.0}}
        with self.assertRaisesRegex(ValueError, "알 수 없는 관절"):
            inspect_recording(header, _rows())
        rows = _rows()
        rows[0]["gripper_cmd"] = float("nan")
        rows[0]["gripper_obs"] = None
        with self.assertRaisesRegex(ValueError, "gripper_cmd"):
            inspect_recording(_header(gripper_units="lerobot_raw"), rows)


if __name__ == "__main__":
    unittest.main()
