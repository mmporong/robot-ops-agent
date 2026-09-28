"""LeRobot v3 에피소드 → `robot-ops-trajectory/1` JSONL + 카메라 영상 (lerobot python).

- q_cmd = action, q_obs = observation.state (팔 5관절, 도 → rad). 그리퍼는 따로 둔다
  (`gripper_cmd`·`gripper_obs`, LeRobot 원값 그대로 — 단위는 헤더 `gripper_units`).
- 관절 도는 LeRobot DEGREES 정규화(캘리브 범위 중점 = 0°)이고 so101_new_calib URDF 영점도 범위
  중점이라, 녹화 당시 팔로워 캘리브(2026-08-19 저장본)에서는 rad 변환값이 곧 URDF q다(오프셋 없음).
  이 등식은 그 캘리브로 녹화한 데이터(2026-08-19 ~ 2026-09-09)에만 성립한다.
- 헤더 `source`에 에피소드가 든 data parquet의 SHA-256을 적는다(계획 §3.5).
- 영상: 에피소드 구간을 AV1(libdav1d)로 디코딩해 H.264(`libx264 -preset slow -crf 23
  -pix_fmt yuv420p -g <fps> -movflags +faststart`, 짝수 크기)로 다시 인코딩한다.
  해상도는 원본 그대로 두고, 손목 영상 확대(높이 480)는 리포트 미디어 단계가 맡는다.
- 이 모듈은 팀 저장소를 읽지도 쓰지도 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
# M0 inventory.video_viewpoint: depth 키는 깊이 맵이 아니라 RGB 3인칭 고정 시점이다.
CAMERA_LABELS = {
    "observation.images.depth": ("third_person", "실측 3인칭 카메라"),
    "observation.images.wrist": ("wrist", "실측 손목 카메라"),
}
CAMERA_ORDER = ("observation.images.depth", "observation.images.wrist")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(value: str | Path) -> str:
    text = str(value)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home):]
    data_user = f"/data/{os.environ.get('USER', '')}"
    if os.environ.get("USER") and (text == data_user or text.startswith(data_user + "/")):
        return "/data/$USER" + text[len(data_user):]
    return text


def _episode_meta(root: Path, episode: int) -> dict:
    for path in sorted((root / "meta" / "episodes").glob("*/*.parquet")):
        table = pq.read_table(path)
        rows = [r for r in table.to_pylist() if r["episode_index"] == episode]
        if rows:
            return rows[0]
    raise ValueError(f"에피소드 {episode}가 meta/episodes에 없습니다")


def _encode_segment(src: Path, start_s: float, end_s: float, fps: int, out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-c:v", "libdav1d", "-ss", f"{start_s:.3f}", "-i", str(src),
        "-t", f"{max(0.0, end_s - start_s):.3f}",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
        "-g", str(fps), "-r", str(fps), "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(out)],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    return {
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "n_frames": int(stream.get("nb_read_frames") or 0),
    }


def convert(source: dict, workdir: Path) -> dict:
    if source.get("kind") != "lerobot_v3":
        raise ValueError(f"지원하지 않는 source.kind: {source.get('kind')}")
    root = Path(os.path.abspath(Path(str(source["path"])).expanduser()))  # 심링크는 풀지 않는다(표기 경로 유지)
    episode = int(source["episode"])
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    if info.get("codebase_version") != "v3.0":
        raise ValueError(f"LeRobot v3.0만 지원합니다: {info.get('codebase_version')}")
    fps = int(info["fps"])
    names = list(info["features"]["observation.state"]["names"])
    action_names = list(info["features"]["action"]["names"])
    if names != action_names:
        raise ValueError("state·action 관절 이름 순서가 다릅니다")
    idx = [names.index(j) for j in ARM_JOINTS]
    grip = names.index("gripper") if "gripper" in names else None

    meta = _episode_meta(root, episode)
    data_rel = info["data_path"].format(
        chunk_index=meta["data/chunk_index"], file_index=meta["data/file_index"]
    )
    data_path = root / data_rel
    table = pq.read_table(data_path, columns=["observation.state", "action", "timestamp", "episode_index", "frame_index"])
    ep = np.asarray(table["episode_index"].to_pylist())
    keep = np.nonzero(ep == episode)[0]
    if keep.size == 0:
        raise ValueError(f"{data_rel}에 에피소드 {episode} 행이 없습니다")
    state = np.asarray(table["observation.state"].to_pylist(), dtype=float)[keep]
    action = np.asarray(table["action"].to_pylist(), dtype=float)[keep]
    ts = np.asarray(table["timestamp"].to_pylist(), dtype=float)[keep]
    order = np.argsort(np.asarray(table["frame_index"].to_pylist())[keep], kind="stable")
    state, action, ts = state[order], action[order], ts[order]
    ts = ts - ts[0]

    source_sha = _sha256(data_path)
    header = {
        "schema": "robot-ops-trajectory/1",
        "robot_id": str(info.get("robot_type", "so101_follower")),
        "arm": "left",
        "joint_names": list(ARM_JOINTS),
        "joint_map": {j: f"left_{j}" for j in ARM_JOINTS},
        "units": "rad",
        "units_in": "lerobot_degrees",
        "gripper_units": "lerobot_raw",
        "fps": fps,
        "source": {
            "kind": "lerobot_v3",
            "path": display_path(root),
            "episode": episode,
            "file": data_rel,
            "sha256": source_sha,
        },
        "origin": "real_recording",
        "note_ko": "q_cmd=action(리더 명령), q_obs=observation.state(팔로워 관측). 도→rad 변환만 했다 — "
                   "LeRobot DEGREES(범위 중점 0°)가 녹화 당시 캘리브에서 URDF q와 같아 교정 차를 빼지 않는다",
    }
    workdir.mkdir(parents=True, exist_ok=True)
    traj_path = workdir / "trajectory.jsonl"
    with traj_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for i in range(len(ts)):
            row = {
                "t": round(float(ts[i]), 6),
                "q_cmd": [math.radians(float(action[i, j])) for j in idx],
                "q_obs": [math.radians(float(state[i, j])) for j in idx],
                "gripper_cmd": float(action[i, grip]) if grip is not None else None,
                "gripper_obs": float(state[i, grip]) if grip is not None else None,
            }
            handle.write(json.dumps(row) + "\n")

    videos = []
    video_path_tpl = info.get("video_path")
    for key in CAMERA_ORDER:
        feature = info["features"].get(key)
        if not feature or feature.get("dtype") != "video" or not video_path_tpl:
            continue
        rel = video_path_tpl.format(
            video_key=key,
            chunk_index=meta[f"videos/{key}/chunk_index"],
            file_index=meta[f"videos/{key}/file_index"],
        )
        src = root / rel
        start = float(meta[f"videos/{key}/from_timestamp"])
        end = float(meta[f"videos/{key}/to_timestamp"])
        role, label = CAMERA_LABELS.get(key, (key, key))
        out = workdir / "media" / f"camera_{role}.mp4"
        probe = _encode_segment(src, start, end, fps, out)
        videos.append({
            "path": str(out),
            "camera": role,
            "camera_key": key,
            "label_ko": label,
            "source": {"path": display_path(src), "sha256": _sha256(src),
                       "from_s": start, "to_s": end, "codec": feature.get("info", {}).get("video.codec")},
            "sha256": _sha256(out),
            **probe,
        })

    return {
        "trajectory_path": str(traj_path),
        "sha256": _sha256(traj_path),
        "source_sha256": source_sha,
        "fps": fps,
        "n_frames": int(len(ts)),
        "video": videos[0] if videos else None,
        "videos": videos,
    }


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    return {"status": "ok", **convert(request["source"], workdir)}
