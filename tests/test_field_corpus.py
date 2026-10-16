from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ModuleNotFoundError:  # 프로젝트 기본 venv에는 선택적 데이터 판독 의존성이 없다.
    pa = None
    pq = None


evaluate_recordings = None
if pa is not None:
    MODULE_PATH = Path(__file__).resolve().parents[1] / "adapters" / "bimanual" / "evaluate_recordings.py"
    SPEC = importlib.util.spec_from_file_location("evaluate_recordings", MODULE_PATH)
    assert SPEC and SPEC.loader
    evaluate_recordings = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(evaluate_recordings)


JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def make_dataset(
    root: Path,
    name: str,
    *,
    bad_value: float | None = None,
    fps: int = 10,
    joint_names: list[str] | None = None,
    timestamps: list[float] | None = None,
) -> Path:
    assert pa is not None and pq is not None
    names = joint_names or JOINTS
    dataset = root / name
    data_path = dataset / "data" / "chunk-000" / "file-000.parquet"
    episode_path = dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    data_path.parent.mkdir(parents=True)
    episode_path.parent.mkdir(parents=True)
    info = {
        "codebase_version": "v3.0",
        "fps": fps,
        "robot_type": "so101_follower",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [6], "names": names},
            "action": {"dtype": "float32", "shape": [6], "names": names},
        },
        "total_episodes": 1,
        "total_frames": 2,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
    }
    (dataset / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    observed = [[0, 10, 20, 30, 40, 50], [1, 11, 21, 31, 41, 51]]
    if bad_value is not None:
        observed[1][0] = bad_value
    pq.write_table(
        pa.table({
            "observation.state": observed,
            "action": [[2, 12, 22, 32, 42, 52], [3, 13, 23, 33, 43, 53]],
            "timestamp": timestamps or [7.2, 7.3],
            "episode_index": [0, 0],
            "frame_index": [0, 1],
        }),
        data_path,
    )
    pq.write_table(
        pa.table({
            "episode_index": [0],
            "length": [2],
            "data/chunk_index": [0],
            "data/file_index": [0],
        }),
        episode_path,
    )
    return dataset


@unittest.skipUnless(pa is not None, "pyarrow가 있는 LeRobot 평가 환경에서 실행")
class FieldCorpusTest(unittest.TestCase):
    def test_builds_normalized_corpus_and_marks_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = make_dataset(base / "input", "first")
            second = make_dataset(base / "input", "second")
            first_rel = "data/chunk-000/file-000.parquet"
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps({
                "so101": {"datasets": {
                    "first": {"data_parquet_sha256": {
                        first_rel: evaluate_recordings.sha256_file(first / first_rel),
                    }},
                    "second": {"data_parquet_sha256": {
                        first_rel: evaluate_recordings.sha256_file(second / first_rel),
                    }},
                }},
            }), encoding="utf-8")

            manifest = evaluate_recordings.build_corpus(base / "input", base / "out", inventory)

            self.assertEqual(manifest["schema"], "robot-ops-field-corpus/1")
            self.assertEqual(manifest["dataset_count"], 2)
            self.assertEqual(manifest["episode_count"], 2)
            self.assertEqual(manifest["unique_episode_count"], 1)
            self.assertEqual(manifest["duplicate_episode_count"], 1)
            self.assertEqual(manifest["episodes"][1]["duplicate_of"], "first--ep0")
            self.assertTrue(all(row["prior_inventory_match"] for row in manifest["episodes"]))
            trajectory = Path(manifest["episodes"][0]["trajectory_path"])
            payload = [json.loads(line) for line in trajectory.read_text().splitlines()]
            self.assertEqual(payload[0]["schema"], "robot-ops-trajectory/1")
            self.assertEqual(payload[1]["t"], 0.0)
            self.assertEqual(payload[2]["t"], 0.1)
            self.assertAlmostEqual(payload[1]["q_cmd"][0], math.radians(2))
            self.assertEqual(payload[1]["gripper_cmd"], 52.0)
            self.assertEqual(manifest["errors"], [])

    def test_nonfinite_episode_is_reported_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_dataset(base / "input", "broken", bad_value=float("nan"))

            manifest = evaluate_recordings.build_corpus(
                base / "input", base / "out", base / "missing-inventory.json"
            )

            self.assertEqual(manifest["episode_count"], 0)
            self.assertEqual(len(manifest["errors"]), 1)
            self.assertEqual(manifest["errors"][0]["stage"], "normalize")
            self.assertIn("NaN", manifest["errors"][0]["error"])

    def test_identity_changes_when_fps_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_dataset(base / "input", "ten_hz", fps=10)
            make_dataset(base / "input", "twenty_hz", fps=20)

            manifest = evaluate_recordings.build_corpus(
                base / "input", base / "out", base / "missing-inventory.json"
            )

            self.assertEqual(manifest["episode_count"], 2)
            self.assertEqual(manifest["unique_episode_count"], 2)
            self.assertTrue(all(row["duplicate_of"] is None for row in manifest["episodes"]))

    def test_identity_changes_when_joint_name_mapping_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_dataset(base / "input", "normal")
            swapped = [JOINTS[1], JOINTS[0], *JOINTS[2:]]
            make_dataset(base / "input", "swapped", joint_names=swapped)

            manifest = evaluate_recordings.build_corpus(
                base / "input", base / "out", base / "missing-inventory.json"
            )

            self.assertEqual(manifest["episode_count"], 2)
            self.assertEqual(manifest["unique_episode_count"], 2)

    def test_repeated_timestamp_is_reported_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_dataset(base / "input", "broken_time", timestamps=[7.2, 7.2])

            manifest = evaluate_recordings.build_corpus(
                base / "input", base / "out", base / "missing-inventory.json"
            )

            self.assertEqual(manifest["episode_count"], 0)
            self.assertEqual(len(manifest["errors"]), 1)
            self.assertIn("증가하지 않습니다", manifest["errors"][0]["error"])

    def test_normalized_episode_id_collision_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_dataset(base / "input", "same name")
            make_dataset(base / "input", "same_name")

            manifest = evaluate_recordings.build_corpus(
                base / "input", base / "out", base / "missing-inventory.json"
            )

            self.assertEqual(manifest["episode_count"], 1)
            self.assertEqual(len(manifest["errors"]), 1)
            self.assertIn("episode id가 중복", manifest["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
