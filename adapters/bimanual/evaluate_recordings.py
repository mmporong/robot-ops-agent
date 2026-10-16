"""LeRobot v3 녹화 묶음을 CPU에서 평가용 궤적으로 정규화한다.

원본 데이터셋은 읽기만 하며, 결과는 지정한 ``--out`` 아래에만 쓴다.
LeRobot, 시뮬레이터, 하드웨어 모듈을 import하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA = "robot-ops-field-corpus/1"
TRAJECTORY_SCHEMA = "robot-ops-trajectory/1"
ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
READ_COLUMNS = ("observation.state", "action", "timestamp", "episode_index", "frame_index")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def display_path(value: Path) -> str:
    text = str(value)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home):]
    user = os.environ.get("USER")
    data_root = f"/data/{user}" if user else None
    if data_root and (text == data_root or text.startswith(data_root + os.sep)):
        return "/data/$USER" + text[len(data_root):]
    return text


def canonical_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_inventory(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        datasets = payload.get("so101", {}).get("datasets", {})
        return {
            str(name): dict(entry.get("data_parquet_sha256") or {})
            for name, entry in datasets.items()
            if isinstance(entry, dict)
        }
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {}


def episode_metadata(dataset_root: Path) -> list[dict[str, Any]]:
    paths = sorted((dataset_root / "meta" / "episodes").glob("*/*.parquet"))
    if not paths:
        raise ValueError("meta/episodes parquet가 없습니다")
    columns = ["episode_index", "length", "data/chunk_index", "data/file_index"]
    return pa.concat_tables([pq.read_table(path, columns=columns) for path in paths]).to_pylist()


def normalized_episode(
    *,
    info: dict[str, Any],
    dataset_root: Path,
    episode: int,
    data_rel: str,
    data_sha256: str,
    table: pa.Table,
) -> tuple[dict[str, Any], list[dict[str, Any]], bytes]:
    features = info.get("features") or {}
    state_names = list((features.get("observation.state") or {}).get("names") or [])
    action_names = list((features.get("action") or {}).get("names") or [])
    if state_names != action_names:
        raise ValueError("state·action 관절 이름 순서가 다릅니다")
    missing = [name for name in ARM_JOINTS if name not in state_names]
    if missing:
        raise ValueError(f"필수 팔 관절이 없습니다: {missing}")
    joint_indices = [state_names.index(name) for name in ARM_JOINTS]
    gripper_index = state_names.index("gripper") if "gripper" in state_names else None

    ep_index = np.asarray(table["episode_index"].to_pylist(), dtype=np.int64)
    keep = np.flatnonzero(ep_index == episode)
    if keep.size == 0:
        raise ValueError(f"{data_rel}에 에피소드 {episode} 행이 없습니다")
    frames = np.asarray(table["frame_index"].to_pylist(), dtype=np.int64)[keep]
    order = np.argsort(frames, kind="stable")
    if len(np.unique(frames)) != len(frames):
        raise ValueError("frame_index가 중복되었습니다")
    state = np.asarray(table["observation.state"].to_pylist(), dtype=float)[keep][order]
    action = np.asarray(table["action"].to_pylist(), dtype=float)[keep][order]
    timestamp = np.asarray(table["timestamp"].to_pylist(), dtype=float)[keep][order]
    if state.ndim != 2 or action.ndim != 2 or state.shape != action.shape:
        raise ValueError("state·action 배열 모양이 올바르지 않습니다")
    if state.shape[1] != len(state_names):
        raise ValueError("관절 이름 수와 state·action 열 수가 다릅니다")
    if not (np.isfinite(state).all() and np.isfinite(action).all() and np.isfinite(timestamp).all()):
        raise ValueError("비유한 수치(NaN 또는 Inf)가 있습니다")
    if len(timestamp) > 1 and np.any(np.diff(timestamp) <= 0):
        raise ValueError("frame_index 정렬 후 timestamp가 증가하지 않습니다")
    timestamp = timestamp - timestamp[0]

    fps = int(info["fps"])
    header = {
        "schema": TRAJECTORY_SCHEMA,
        "robot_id": str(info.get("robot_type", "so101_follower")),
        "arm": "left",
        "joint_names": list(ARM_JOINTS),
        "joint_map": {name: f"left_{name}" for name in ARM_JOINTS},
        "units": "rad",
        "units_in": "lerobot_degrees",
        "gripper_units": "lerobot_raw",
        "fps": fps,
        "source": {
            "kind": "lerobot_v3",
            "path": display_path(dataset_root),
            "episode": episode,
            "file": data_rel,
            "sha256": data_sha256,
        },
        "origin": "real_recording",
        "note_ko": "q_cmd=action, q_obs=observation.state. 팔 5관절만 도에서 rad로 변환했다",
    }
    rows: list[dict[str, Any]] = []
    identity = hashlib.sha256()
    identity.update(canonical_line({
        "schema": TRAJECTORY_SCHEMA,
        "robot_id": header["robot_id"],
        "arm": header["arm"],
        "joint_names": header["joint_names"],
        "units": header["units"],
        "units_in": header["units_in"],
        "gripper_units": header["gripper_units"],
        "fps": header["fps"],
    }))
    for index in range(len(timestamp)):
        row = {
            "t": round(float(timestamp[index]), 6),
            "q_cmd": [math.radians(float(action[index, j])) for j in joint_indices],
            "q_obs": [math.radians(float(state[index, j])) for j in joint_indices],
            "gripper_cmd": float(action[index, gripper_index]) if gripper_index is not None else None,
            "gripper_obs": float(state[index, gripper_index]) if gripper_index is not None else None,
        }
        rows.append(row)
        identity.update(canonical_line(row))
    return header, rows, identity.digest()


def write_trajectory(path: Path, header: dict[str, Any], rows: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        line = canonical_line(header)
        handle.write(line)
        digest.update(line)
        for value in rows:
            line = canonical_line(value)
            handle.write(line)
            digest.update(line)
    return digest.hexdigest()


def build_corpus(root: Path, out: Path, inventory_path: Path) -> dict[str, Any]:
    root = root.expanduser().absolute()
    out = out.expanduser().absolute()
    inventory = read_inventory(inventory_path.expanduser())
    dataset_paths = sorted({path.parent.parent for path in root.glob("*/meta/info.json")})
    episodes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    input_hashes: dict[str, Any] = {}
    identity_owner: dict[str, str] = {}
    episode_ids: set[str] = set()

    for dataset_root in dataset_paths:
        dataset = dataset_root.name
        metadata_hashes: dict[str, str] = {}
        parquet_hashes: dict[str, str] = {}
        try:
            info_path = dataset_root / "meta" / "info.json"
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("codebase_version") != "v3.0":
                raise ValueError(f"LeRobot v3.0이 아닙니다: {info.get('codebase_version')}")
            for meta_path in sorted((dataset_root / "meta").rglob("*")):
                if meta_path.is_file():
                    metadata_hashes[str(meta_path.relative_to(dataset_root))] = sha256_file(meta_path)
            meta_rows = episode_metadata(dataset_root)
        except Exception as exc:  # 데이터셋 단위 오류를 manifest에 남기고 다른 데이터셋은 계속 읽는다.
            errors.append({"dataset": dataset, "episode": None, "stage": "metadata", "error": str(exc)})
            input_hashes[dataset] = {"metadata_sha256": metadata_hashes, "data_parquet_sha256": parquet_hashes}
            continue

        by_file: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for meta in meta_rows:
            by_file[(int(meta["data/chunk_index"]), int(meta["data/file_index"]))].append(meta)

        for (chunk_index, file_index), file_episodes in sorted(by_file.items()):
            data_rel = str(info["data_path"]).format(chunk_index=chunk_index, file_index=file_index)
            data_path = dataset_root / data_rel
            try:
                data_sha256 = sha256_file(data_path)
                parquet_hashes[data_rel] = data_sha256
                table = pq.read_table(data_path, columns=list(READ_COLUMNS))
            except Exception as exc:
                for meta in file_episodes:
                    errors.append({
                        "dataset": dataset,
                        "episode": int(meta["episode_index"]),
                        "stage": "parquet",
                        "error": str(exc),
                    })
                continue

            for meta in sorted(file_episodes, key=lambda row: int(row["episode_index"])):
                episode = int(meta["episode_index"])
                episode_id = f"{SAFE_NAME.sub('_', dataset)}--ep{episode}"
                trajectory_path = out / "trajectories" / f"{episode_id}.jsonl"
                try:
                    if episode_id in episode_ids:
                        raise ValueError(f"episode id가 중복되었습니다: {episode_id}")
                    episode_ids.add(episode_id)
                    header, rows, identity_bytes = normalized_episode(
                        info=info,
                        dataset_root=dataset_root,
                        episode=episode,
                        data_rel=data_rel,
                        data_sha256=data_sha256,
                        table=table,
                    )
                    expected_length = int(meta["length"])
                    if len(rows) != expected_length:
                        raise ValueError(f"행 수 {len(rows)} != meta length {expected_length}")
                    trajectory_sha256 = write_trajectory(trajectory_path, header, rows)
                    identity_sha256 = identity_bytes.hex()
                    duplicate_of = identity_owner.setdefault(identity_sha256, episode_id)
                    if duplicate_of == episode_id:
                        duplicate_of = None
                    prior_match = inventory.get(dataset, {}).get(data_rel) == data_sha256
                    episodes.append({
                        "id": episode_id,
                        "dataset": dataset,
                        "episode": episode,
                        "frame_count": len(rows),
                        "duration_s": round(float(rows[-1]["t"]), 6),
                        "fps": int(info["fps"]),
                        "trajectory_path": str(trajectory_path.resolve()),
                        "trajectory_sha256": trajectory_sha256,
                        "identity_sha256": identity_sha256,
                        "duplicate_of": duplicate_of,
                        "source": {
                            "path": display_path(dataset_root),
                            "file": data_rel,
                            "sha256": data_sha256,
                        },
                        "prior_inventory_match": prior_match,
                    })
                except Exception as exc:
                    errors.append({"dataset": dataset, "episode": episode, "stage": "normalize", "error": str(exc)})

        input_hashes[dataset] = {
            "metadata_sha256": metadata_hashes,
            "data_parquet_sha256": parquet_hashes,
        }

    manifest = {
        "schema": SCHEMA,
        "root": display_path(root),
        "dataset_count": len(dataset_paths),
        "episode_count": len(episodes),
        "unique_episode_count": len({episode["identity_sha256"] for episode in episodes}),
        "duplicate_episode_count": sum(episode["duplicate_of"] is not None for episode in episodes),
        "episodes": episodes,
        "input_hashes": input_hashes,
        "errors": errors,
    }
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LeRobot v3 녹화를 평가용 궤적으로 정규화")
    parser.add_argument("--root", type=Path, default=Path.home() / "so101_datasets")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, default=Path("evaluations/replay/inventory.json"))
    args = parser.parse_args(argv)
    manifest = build_corpus(args.root, args.out, args.inventory)
    summary = {
        "manifest": str((args.out.expanduser().absolute() / "manifest.json")),
        "datasets": manifest["dataset_count"],
        "episodes": manifest["episode_count"],
        "unique_episodes": manifest["unique_episode_count"],
        "duplicates": manifest["duplicate_episode_count"],
        "errors": len(manifest["errors"]),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
