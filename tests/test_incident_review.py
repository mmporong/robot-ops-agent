from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

from robot_ops.cli import main
from robot_ops.incident_review import compare_rows, load_events, read_input, review_path, review_rows
from robot_ops.incident_report import write_report


def header():
    return {"schema": "robot-ops-trajectory/1", "robot_id": "test", "arm": "left", "joint_names": ["shoulder"],
            "units": "rad", "fps": 10, "source": {"kind": "fixture", "path": "fixture", "episode": 0, "sha256": "0" * 64},
            "origin": "fault_injection"}


def samples(frozen=True, identity=None):
    rows = [{"t": i / 10, "q_cmd": [math.radians(i)], "q_obs": [0.0 if frozen else math.radians(i)]} for i in range(20)]
    if identity is not None:
        for i, row in enumerate(rows):
            row["source_sequence"] = i if identity == "changing" else 0
    return rows


def save(path, rows, h=None):
    path.write_text("\n".join(json.dumps(x) for x in [h or header(), *rows]) + "\n")


class IncidentReviewTests(unittest.TestCase):
    def test_missing_identity_requests_evidence_not_a_cause(self):
        result = review_rows(header(), samples())
        self.assertEqual(result["cause_status"], "undetermined")
        self.assertEqual(result["next_checks"][0]["id"], "collect_source_identity")
        self.assertIn("inspect_source_identity", [step["tool"] for step in result["trace"]])

    def test_stale_identity_selects_collector_inspection(self):
        result = review_rows(header(), samples(identity="stale"))
        self.assertEqual(result["next_checks"][0]["id"], "inspect_collector")
        self.assertIn("source_identity_stagnation", result["windows"][0]["kinds"])

    def test_changing_identity_selects_actuator_inspection(self):
        result = review_rows(header(), samples(identity="changing"))
        self.assertEqual(result["next_checks"][0]["id"], "inspect_actuator")
        self.assertEqual(result["cause_status"], "undetermined")

    def test_partial_metadata_does_not_rule_out_collector(self):
        rows = samples(identity="changing")
        del rows[5]["source_sequence"]
        result = review_rows(header(), rows)
        self.assertEqual(result["next_checks"][0]["id"], "collect_source_identity")

    def test_no_candidate_abstains_without_running_unneeded_inspection(self):
        rows = samples(False)
        result = review_rows(header(), rows)
        self.assertEqual(result["status"], "no_candidate")
        self.assertNotIn("inspect_source_identity", [step["tool"] for step in result["trace"]])
        self.assertEqual(result["next_checks"][0]["id"], "broaden_evidence")

    def test_normal_stop_is_not_observation_stagnation(self):
        rows = samples()
        for row in rows:
            row["q_cmd"] = [0.0]
        self.assertEqual(review_rows(header(), rows)["windows"], [])

    def test_gap_triggers_capture_check(self):
        rows = samples(False)
        for row in rows[10:]:
            row["t"] += 3
        result = review_rows(header(), rows)
        self.assertEqual(result["next_checks"][0]["id"], "inspect_capture_gap")
        self.assertEqual(result["windows"][0]["start_s"], 0.9)
        self.assertEqual(result["windows"][0]["end_s"], 4)

    def test_overlapping_joint_flags_become_one_window(self):
        h, rows = header(), samples()
        h["joint_names"].append("elbow")
        for row in rows:
            row["q_cmd"] *= 2
            row["q_obs"] *= 2
        result = review_rows(h, rows)
        self.assertEqual(result["summary"]["issue_count"], 2)
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(len(result["windows"][0]["joints"]), 2)

    def test_long_normal_prefix_is_removed(self):
        rows = samples()
        for i, row in enumerate(rows):
            row["q_cmd"] = [math.radians(max(0, i - 10))]
        self.assertGreaterEqual(review_rows(header(), rows)["windows"][0]["start_s"], 1)

    def test_persistent_warning_shows_first_evidence_and_preserves_full_extent(self):
        rows = [{"t": i / 10, "q_cmd": [math.radians(i)], "q_obs": [0.0]} for i in range(100)]
        result = review_rows(header(), rows)
        window = result["windows"][0]
        self.assertEqual(window["end_s"], 2.0)
        self.assertEqual(window["full_end_s"], 9.9)
        self.assertEqual(result["summary"]["selected_duration_s"], 2.0)

    def test_at_most_three_separate_windows_preserve_first(self):
        rows = []
        for block in range(5):
            for j in range(10):
                i = block * 10 + j
                rows.append({"t": i / 10, "q_cmd": [math.radians(i)], "q_obs": [math.radians(block * 10)]})
        result = review_rows(header(), rows)
        self.assertEqual(result["summary"]["window_count"], 5)
        self.assertEqual(len(result["windows"]), 3)
        self.assertEqual(result["windows"][0]["start_s"], 0)

    def test_error_log_changes_next_check_but_does_not_confirm_cause(self):
        event = {"t": 0.5, "level": "error", "message": "controller timeout"}
        result = review_rows(header(), samples(), events=[event])
        self.assertEqual(result["next_checks"][0]["id"], "inspect_reported_error")
        self.assertEqual(result["windows"][0]["logs"], [event])
        self.assertEqual(result["cause_status"], "undetermined")

    def test_misaligned_and_invalid_logs_rejected(self):
        for t in (-1, 100, float("nan"), True):
            with self.subTest(t=t), self.assertRaises(ValueError):
                review_rows(header(), samples(), events=[{"t": t, "level": "error", "message": "test"}])

    def test_comparison_uses_same_commands_and_records_limited_claim(self):
        result = compare_rows(header(), samples(), header(), samples(False))
        self.assertEqual(result["status"], "warnings_removed_in_scope")
        self.assertEqual(result["cause_status"], "undetermined")

    def test_comparison_rejects_changed_commands_configuration_and_coverage(self):
        for change in ("command", "resolution", "metadata", "time"):
            h, rows = header(), samples(False)
            if change == "command": rows[1]["q_cmd"] = [0.3]
            if change == "resolution": h["recording_check"] = {"observation_resolution_deg": {"shoulder": 5}}
            if change == "metadata": rows[1]["source_sequence"] = 1
            if change == "time": rows[-1]["t"] += 0.1
            with self.subTest(change=change):
                self.assertEqual(compare_rows(header(), samples(), h, rows)["status"], "incomparable")

    def test_comparison_allows_same_metadata_coverage_when_warning_disappears(self):
        self.assertEqual(compare_rows(header(), samples(True, "changing"), header(), samples(False, "changing"))["status"],
                         "warnings_removed_in_scope")

    def test_unknown_physical_cause_does_not_change_with_unread_header_hint(self):
        h1, h2 = header(), header()
        h1["cause_hint"] = "sensor freeze"
        h2["cause_hint"] = "physical obstruction"
        self.assertEqual(review_rows(h1, samples()), review_rows(h2, samples()))

    def test_report_and_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, out = root / "input.jsonl", root / "out"
            save(path, samples())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["investigate", "--trajectory", str(path), "--out", str(out)]), 0)
            result = json.loads((out / "incident.json").read_text())
            self.assertEqual(result["source"]["sha256"], read_input(path)[2])
            self.assertIsNone(result["media"]["real"])
            self.assertTrue((out / "index.html").is_file())
            self.assertFalse(result["validation"]["field_validated"])

    def test_report_escapes_script_and_rejects_external_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = review_rows(header(), samples(), events=[{"t": 0.5, "level": "error", "message": "</script><script>alert(1)</script>"}])
            write_report(data, root)
            self.assertNotIn("</script><script>alert", (root / "index.html").read_text())
            data["media"] = {"real": "https://example.com/video.mp4"}
            with self.assertRaises(ValueError): write_report(data, root)

    def test_events_hash_mismatch_and_bad_input_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = root / "events.json"
            events.write_text(json.dumps({"schema": "robot-ops-incident-events/1", "time_basis": "trajectory", "trajectory_sha256": "0" * 64, "events": []}))
            with self.assertRaises(ValueError): load_events(events, "1" * 64)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["investigate", "--trajectory", str(root / "absent.jsonl"), "--out", str(root / "out")]), 2)
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
