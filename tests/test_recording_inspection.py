from __future__ import annotations

import contextlib
import io
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robot_ops.cli import main
from robot_ops.recording_inspection import (
    check_rows,
    inspect_corpus,
    inspect_path,
    inspect_source_identity,
)
from robot_ops.replay.artifacts import sha256_file
from robot_ops.replay.field_eval import input_identity
from robot_ops.replay.profile import Profile


def _header(*, source_path: str = "recording", source_sha256: str = "0" * 64) -> dict:
    return {
        "schema": "robot-ops-trajectory/1",
        "robot_id": "so101",
        "arm": "left",
        "joint_names": ["shoulder"],
        "units": "rad",
        "fps": 10,
        "source": {
            "kind": "lerobot_v3",
            "path": source_path,
            "episode": 0,
            "sha256": source_sha256,
        },
        "origin": "real_recording",
    }


def _rows(*, count: int = 8, stationary_observation: bool = False) -> list[dict]:
    rows = []
    for index in range(count):
        angle = math.radians(index)
        rows.append(
            {
                "t": index / 10,
                "q_cmd": [angle],
                "q_obs": [0.0 if stationary_observation else angle],
            }
        )
    return rows


def _write_trajectory(path: Path, header: dict, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *rows]) + "\n",
        encoding="utf-8",
    )


def _profile(root: Path) -> Profile:
    return Profile(
        path=root / "profiles/test.toml",
        base_dir=root,
        project={"id": "test"},
        adapter_entry=root / "adapter.py",
        interpreters={},
        tasks={},
        allowlist_scripts=(),
        allowlist_imports=(),
        limits={},
        artifacts_root=root / "artifacts",
        report_dir=root / "report",
        delete_patterns=(),
        source_repo=None,
    )


class SourceIdentityTest(unittest.TestCase):
    def test_reports_not_assessable_when_source_identity_is_absent(self) -> None:
        result = inspect_source_identity(_rows())

        self.assertEqual(result["status"], "not_assessable")
        self.assertEqual(result["missing_frames"], 8)
        self.assertEqual(result["issues"], [])

    def test_does_not_report_freshness_issue_when_sequence_keeps_advancing(self) -> None:
        rows = _rows(stationary_observation=True)
        for index, row in enumerate(rows):
            row["source_sequence"] = index

        result = inspect_source_identity(rows)

        self.assertEqual(result["status"], "checked_source_identity")
        self.assertEqual(result["issues"], [])

    def test_reports_stagnation_when_both_source_identifiers_repeat(self) -> None:
        rows = _rows()
        for row in rows:
            row["source_sample_timestamp_s"] = 1234.5
            row["source_sequence"] = 42

        result = inspect_source_identity(rows, min_repeat_s=0.3)

        self.assertEqual(len(result["issues"]), 1)
        issue = result["issues"][0]
        self.assertEqual(issue["kind"], "source_identity_stagnation")
        self.assertEqual((issue["reference_frame"], issue["frame_end"]), (0, 7))
        self.assertAlmostEqual(issue["evidence_duration_s"], 0.7)

    def test_treats_source_clock_as_an_identity_instead_of_comparing_clock_epochs(self) -> None:
        rows = _rows()
        epoch = 1_800_000_000.0
        for index, row in enumerate(rows):
            row["source_sample_timestamp_s"] = epoch + index / 10

        result = inspect_source_identity(rows)

        self.assertEqual(result["issues"], [])

    def test_counts_frames_with_partial_source_metadata(self) -> None:
        rows = _rows()
        for index, row in enumerate(rows):
            row["source_sample_timestamp_s"] = 100.0 + index / 10
            row["source_sequence"] = index
        rows[2].pop("source_sequence")
        rows[5].pop("source_sample_timestamp_s")

        result = inspect_source_identity(rows)

        self.assertEqual(result["missing_frames"], 2)
        self.assertEqual(result["issues"], [])

    def test_rejects_invalid_or_non_finite_source_sequence(self) -> None:
        for value in (1.5, -1, float("nan"), True):
            with self.subTest(value=value):
                rows = _rows()
                rows[0]["source_sequence"] = value
                with self.assertRaisesRegex(ValueError, "source_sequence"):
                    inspect_source_identity(rows)

    def test_check_rows_combines_v2_detector_and_source_identity_coverage(self) -> None:
        rows = _rows(stationary_observation=True)
        for index, row in enumerate(rows):
            row["source_sequence"] = index

        result = check_rows(_header(), rows)

        self.assertEqual(result["detector"], "joint_time_v2")
        self.assertEqual(result["coverage"]["observation_freshness"], "checked_source_identity")
        self.assertEqual(result["coverage"]["freshness"]["missing_frames"], 0)
        self.assertTrue(any(issue["kind"] == "joint_observation_stagnation" for issue in result["issues"]))
        self.assertFalse(any(issue["kind"] == "source_identity_stagnation" for issue in result["issues"]))


class InspectionReportTest(unittest.TestCase):
    def test_cli_rejects_missing_corpus_hash_with_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _write_trajectory(trajectory, _header(), _rows())
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema": "robot-ops-field-corpus/1", "episodes": [
                {"id": "x", "trajectory_path": str(trajectory)}]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["inspect", "--corpus", str(manifest), "--out", str(root / "out")])

            self.assertEqual(code, 2)
            error = json.loads(output.getvalue())
            self.assertEqual(error["type"], "ValueError")
            self.assertIn("SHA-256", error["error"])

    def test_rejects_empty_or_malformed_corpus_instead_of_showing_empty_success(self) -> None:
        for episodes in ([], None, [{}], [{"id": "x", "trajectory_path": 3}]):
            with self.subTest(episodes=episodes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = root / "manifest.json"
                manifest.write_text(json.dumps({"schema": "robot-ops-field-corpus/1", "episodes": episodes}))
                with self.assertRaisesRegex(ValueError, "기록 목록"):
                    inspect_corpus(manifest, root / "out")

    def test_unavailable_saved_media_does_not_invalidate_valid_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _write_trajectory(trajectory, _header(), _rows(stationary_observation=True))
            with mock.patch("robot_ops.recording_inspection._matching_media", side_effect=OSError("missing archive")):
                result = inspect_path(trajectory, root / "out", profile=_profile(root))

            self.assertEqual(result["status"], "review_required")
            self.assertTrue(result["check"]["issues"])
            self.assertIsNone(result["media"]["real"])
            self.assertEqual(result["media"]["lookup_error"], "OSError")

    def test_inspect_path_writes_valid_json_and_html_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            out = root / "out"
            _write_trajectory(trajectory, _header(), _rows())

            result = inspect_path(trajectory, out)

            self.assertEqual(result["schema"], "robot-ops-recording-inspection/1")
            self.assertEqual(result["status"], "no_issue_in_checked_scope")
            self.assertEqual(json.loads((out / "inspection.json").read_text(encoding="utf-8")), result)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "inspection.js").is_file())
            self.assertTrue((out / "inspection.css").is_file())

    def test_inspect_path_writes_invalid_input_report_for_bad_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "bad.jsonl"
            out = root / "out"
            trajectory.write_text("not-json\n", encoding="utf-8")

            result = inspect_path(trajectory, out)

            self.assertEqual(result["status"], "invalid_input")
            self.assertIn("검사할 수 없습니다", (out / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((out / "inspection.json").read_text(encoding="utf-8"))["status"], "invalid_input")

    def test_embedded_json_escapes_script_closing_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            out = root / "out"
            header = _header()
            header["joint_names"] = ["</script><script>alert(1)</script>"]
            _write_trajectory(trajectory, header, _rows())

            inspect_path(trajectory, out)
            page = (out / "index.html").read_text(encoding="utf-8")

            self.assertNotIn("</script><script>alert(1)</script>", page)
            self.assertIn(r"\u003c/script>\u003cscript>alert(1)\u003c/script>", page)

    def test_copies_media_only_when_source_and_recorded_poses_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "dataset"
            source_path.mkdir()
            source_sha = "a" * 64
            header = _header(source_path=str(source_path), source_sha256=source_sha)
            rows = _rows()
            trajectory = root / "trajectory.jsonl"
            replay_trajectory = root / "recorded.jsonl"
            real = root / "real.mp4"
            ghost = root / "ghost.mp4"
            _write_trajectory(trajectory, header, rows)
            _write_trajectory(replay_trajectory, header, rows)
            real.write_bytes(b"real video")
            ghost.write_bytes(b"ghost video")
            replay = {
                "source": {"sha256": source_sha},
                "trajectory_path": str(replay_trajectory),
                "convert_video": {"path": str(real)},
                "video_path": str(ghost),
            }
            key = (str(source_path), 0)

            with mock.patch("robot_ops.replay.report.latest_replay_results", return_value={key: replay}):
                result = inspect_path(trajectory, root / "out", profile=_profile(root))

            self.assertEqual(result["media"]["real"], "media/real.mp4")
            self.assertEqual(result["media"]["ghost"], "media/ghost.mp4")
            self.assertEqual(result["media"]["real_sha256"], sha256_file(real))
            self.assertEqual(result["media"]["ghost_sha256"], sha256_file(ghost))

    def test_does_not_copy_media_when_recorded_poses_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "dataset"
            source_path.mkdir()
            source_sha = "b" * 64
            header = _header(source_path=str(source_path), source_sha256=source_sha)
            rows = _rows()
            trajectory = root / "trajectory.jsonl"
            replay_trajectory = root / "recorded.jsonl"
            real = root / "real.mp4"
            _write_trajectory(trajectory, header, rows)
            different = _rows()
            different[3]["q_obs"] = [math.radians(99)]
            _write_trajectory(replay_trajectory, header, different)
            real.write_bytes(b"real video")
            replay = {
                "source": {"sha256": source_sha},
                "trajectory_path": str(replay_trajectory),
                "convert_video": {"path": str(real)},
            }

            with mock.patch(
                "robot_ops.replay.report.latest_replay_results",
                return_value={(str(source_path), 0): replay},
            ):
                result = inspect_path(trajectory, root / "out", profile=_profile(root))

            self.assertIsNone(result["media"]["real"])
            self.assertFalse((root / "out/media/real.mp4").exists())


class CorpusAndCliTest(unittest.TestCase):
    def test_corpus_rejects_changed_trajectory_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _write_trajectory(trajectory, _header(), _rows())
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "robot-ops-field-corpus/1",
                        "errors": [],
                        "episodes": [
                            {
                                "id": "record-1",
                                "trajectory_path": str(trajectory),
                                "trajectory_sha256": "f" * 64,
                                "identity_sha256": input_identity(_header(), _rows()),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "입력 해시"):
                inspect_corpus(manifest, root / "out")

    def test_corpus_rejects_duplicate_of_unknown_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            _write_trajectory(trajectory, _header(), _rows())
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "robot-ops-field-corpus/1",
                        "errors": [],
                        "episodes": [
                            {
                                "id": "forged-duplicate",
                                "trajectory_path": str(trajectory),
                                "trajectory_sha256": sha256_file(trajectory),
                                "identity_sha256": input_identity(_header(), _rows()),
                                "duplicate_of": "record-that-does-not-exist",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "중복 대상"):
                inspect_corpus(manifest, root / "out")

    def test_corpus_rejects_duplicate_with_different_input_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical.jsonl"
            impostor = root / "impostor.jsonl"
            canonical_rows = _rows()
            impostor_rows = _rows(stationary_observation=True)
            header = _header()
            _write_trajectory(canonical, header, canonical_rows)
            _write_trajectory(impostor, header, impostor_rows)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "robot-ops-field-corpus/1",
                        "errors": [],
                        "episodes": [
                            {
                                "id": "canonical",
                                "trajectory_path": str(canonical),
                                "trajectory_sha256": sha256_file(canonical),
                                "identity_sha256": input_identity(header, canonical_rows),
                            },
                            {
                                "id": "impostor",
                                "trajectory_path": str(impostor),
                                "trajectory_sha256": sha256_file(impostor),
                                "identity_sha256": input_identity(header, impostor_rows),
                                "duplicate_of": "canonical",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "검사 입력이 다릅니다"):
                inspect_corpus(manifest, root / "out")

    def test_corpus_rejects_record_id_that_can_escape_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            rows = _rows()
            header = _header()
            _write_trajectory(trajectory, header, rows)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "robot-ops-field-corpus/1",
                        "errors": [],
                        "episodes": [
                            {
                                "id": "../outside",
                                "trajectory_path": str(trajectory),
                                "trajectory_sha256": sha256_file(trajectory),
                                "identity_sha256": input_identity(header, rows),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "기록 ID"):
                inspect_corpus(manifest, root / "out")

    def test_cli_inspects_one_trajectory_without_starting_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trajectory = root / "trajectory.jsonl"
            out = root / "out"
            _write_trajectory(trajectory, _header(), _rows())
            stdout = io.StringIO()

            with mock.patch("subprocess.run", side_effect=AssertionError("external process called")) as run:
                with contextlib.redirect_stdout(stdout):
                    code = main(["inspect", "--trajectory", str(trajectory), "--out", str(out)])

            self.assertEqual(code, 0)
            run.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue())["status"], "no_issue_in_checked_scope")
            self.assertTrue((out / "index.html").is_file())

    def test_cli_rejects_dataset_root_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with mock.patch("subprocess.run", side_effect=AssertionError("external process called")) as run:
                with contextlib.redirect_stdout(stdout):
                    code = main(["inspect", "--dataset-root", tmp, "--out", str(Path(tmp) / "out")])

            self.assertEqual(code, 2)
            run.assert_not_called()
            self.assertIn("--profile", json.loads(stdout.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
