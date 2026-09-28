"""lerobot_episode `run` (rlwalk python): SO-101 MuJoCo 미러로 TCP 계산 + 고스트 렌더.

- 궤적 모델 = SO-101 MuJoCo 미러(`~/so101_tools/sim`, URDF 대비 RMS 0.00 mm).
  관절 → qpos 변환은 미러의 `SimMirror.set_pose_deg(deg, attach=False)`를 그대로 쓰되,
  서보 각 → URDF q 매핑은 현재 차량 팔의 `mapping.json`이 아니라 녹화 당시 값
  (`recording_mapping`, 부호 +1·오프셋 0)을 쓴다. 미러 생성자가 부르는 `arm_lib.load_mapping`을
  이 함수로 바꿔 끼우므로 `mapping.json`은 읽지도 쓰지도 않는다.
- 장면: 녹화는 책상 클램프 배치라 차량 받침대(`mobile_platform`)를 치우고 책상 상판을 팔 장착면
  (팬 축 아래 78 mm, `DESK_TOP_PANEL_M`)에 둔다. 원본 MJCF·미러 파일은 고치지 않는다(런타임 mocap 이동).
- TCP = `graspframe` site를 미러의 `sim_to_panel`로 팔 기준(팬 축 원점) 좌표로 옮긴 값(m).
- 고스트: 작업 물체(piece·piece_cyl·dropbox)를 장면 밖으로 치우고, 같은 카메라로
  명령 자세와 관측 자세를 각각 오프스크린 렌더한다. 명령 렌더의 팔 픽셀(세그멘테이션)만
  alpha 0.55로 관측 렌더 위에 합성하고, 명령 팔 윤곽 2 px를 진한 파랑으로 칠해 구분을 돕는다. `mj_step`은 부르지 않는다(물리 스텝 0회,
  호출 횟수를 세어 결과에 남긴다).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
sys.dont_write_bytecode = True

import numpy as np  # noqa: E402

DEFAULT_MIRROR_ROOT = Path.home() / "so101_tools" / "sim"
ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
TASK_OBJECTS = ("piece", "piece_cyl", "dropbox")
GHOST_ALPHA = 0.55
GHOST_TINT = np.array([20.0, 90.0, 255.0])  # 명령 팔 색(채도 높은 파랑) — 관측 팔(원래 색)과 구분
TINT_WEIGHT = 0.75
GHOST_OUTLINE = np.array([10.0, 60.0, 210.0])  # 명령 팔 윤곽
OUTLINE_PX = 2
OUTLINE_ALPHA = 0.9
WIDTH, HEIGHT = 640, 480
# 실측 3인칭 카메라(teleop_bench depth 키, 640×480) 시점. 팔 기준(패널) 좌표로 lookat을 준다.
# teleop_bench ep0에서 관측 상태가 실물과 같은 두 프레임(t=0 접힘, t=18 s 파지 직전)의 관절 혼·
# 손가락 끝 8점을 영상에서 찍어 자유 카메라(모델 기본 fovy 45°)로 최소제곱 적합했다(재투영 RMS 9.3 px,
# 카메라 위치 ≈ 팔 뒤·오른쪽 위 (-0.16, -0.24, 0.42) m). 정밀 캘리브레이션 카메라는 아니다.
CAMERA = {"lookat_panel_m": [0.127, -0.045, 0.059], "distance": 0.5, "azimuth": 35.1, "elevation": -46.4}
# 녹화(2026-08-24, 책상 클램프) 배치. 책상 상판 = 팔 장착면 = 팬 축 아래 78 mm.
# 당시 servo_gain.json `floor_z_m`(so101_tools 8113095) 실측값이고, 지금 미러의 차량 상판
# (floor -0.238 + 받침대 0.160)과 같은 면이다. 책상 중심은 미러 기본 배치(패널 (0.15, 0))를 따른다.
DESK_TOP_PANEL_M = -0.078
DESK_CENTER_PANEL_XY = (0.15, 0.0)
STAND_BODY = "mobile_platform"
# 녹화 당시 관절 변환의 근거. 패널 서보 각 = LeRobot DEGREES 정규화(캘리브 범위 중점 = 0°, 360/4095)이고
# so101_new_calib URDF 영점도 관절 범위 중점이라 q = 도 그대로다(부호 +1, 오프셋 0).
JOINT_CONVERSION = {
    "q_urdf_deg": "lerobot_degrees (signs +1, offsets 0)",
    "follower_calibration": "~/.cache/huggingface/lerobot/calibration/robots/so_follower/follower.json "
                            "(2026-08-19 14:34 저장, 2026-09-09 백업본 .bak-20260909-170917)",
    "mapping_source": "~/so101_tools/mapping.json @ 8113095 (2026-08-25, 부호 +1·오프셋 0) — 현재 파일은 읽지 않음",
    "wrist_roll_display_offset_deg": -90.0,
    "gripper": "LeRobot RANGE_0_100 값 → 미러 gripper 관절 도(1:1, -10~100 클램프)",
}


def recording_mapping() -> dict:
    """녹화 당시 서보 각 → URDF q 매핑(`arm_lib.servo_to_rad` 형식)."""
    return {"signs": {j: 1 for j in ARM_JOINTS}, "offsets": {j: 0.0 for j in ARM_JOINTS}}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_path(value: str | Path) -> str:
    text = str(value)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home):]
    user = os.environ.get("USER")
    if user and text.startswith(f"/data/{user}"):
        return "/data/$USER" + text[len(f"/data/{user}"):]
    return text


def read_trajectory(path: Path) -> tuple[dict, list[dict]]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[0]), [json.loads(ln) for ln in lines[1:]]


def load_mirror(mirror_root: Path):
    """미러 모듈을 불러오되 매핑은 녹화 당시 값으로 바꿔 끼운다(mapping.json을 읽지도 쓰지도 않음)."""
    for p in (str(mirror_root), str(mirror_root.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import arm_lib  # noqa: PLC0415 - 미러 경로를 넣은 뒤에만 import 가능

    arm_lib.load_mapping = recording_mapping
    import mujoco  # noqa: PLC0415
    import sim_core  # noqa: PLC0415

    files = {
        "sim_core.py": mirror_root / "sim_core.py",
        "scene_mirror.xml": mirror_root / "scene_mirror.xml",
        "so101_new_calib.xml": mirror_root / "so101_new_calib.xml",
        "sim_frame.json": mirror_root / "sim_frame.json",
        "arm_lib.py": Path(arm_lib.__file__),
    }
    tool_sha = {name: _sha256(path) for name, path in files.items() if path.is_file()}
    return mujoco, sim_core, tool_sha


class StepCounter:
    """mj_step·mj_step1·mj_step2 호출 횟수를 센다(0이어야 한다)."""

    def __init__(self, mujoco) -> None:
        self.count = 0
        self._mujoco = mujoco
        self._saved = {}
        for name in ("mj_step", "mj_step1", "mj_step2"):
            original = getattr(mujoco, name)
            self._saved[name] = original

            def wrapped(*args, _original=original, **kwargs):
                self.count += 1
                return _original(*args, **kwargs)

            setattr(mujoco, name, wrapped)

    def restore(self) -> None:
        for name, original in self._saved.items():
            setattr(self._mujoco, name, original)


def _arm_geom_ids(model, mujoco) -> set[int]:
    root = model.jnt_bodyid[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "shoulder_pan")]
    arm_bodies = set()
    for b in range(model.nbody):
        cur = b
        while cur != 0:
            if cur == root:
                arm_bodies.add(b)
                break
            cur = model.body_parentid[cur]
    return {g for g in range(model.ngeom) if model.geom_bodyid[g] in arm_bodies}


def _pose_deg(q_rad: list[float], gripper) -> dict:
    deg = {j: math.degrees(v) for j, v in zip(ARM_JOINTS, q_rad)}
    if gripper is not None:
        deg["gripper"] = float(gripper)
    return deg


def _camera_record(renderer, cam, model) -> dict:
    """렌더에 실제로 쓰인 OpenGL 카메라(외부)와 투영(내부) 파라미터."""
    gl = renderer.scene.camera[0]
    pos = np.array(gl.pos, dtype=float)
    fwd = np.array(gl.forward, dtype=float)
    up = np.array(gl.up, dtype=float)
    right = np.cross(fwd, up)
    rot = np.stack([right, up, -fwd])  # world → camera (OpenGL 규약)
    extrinsic = np.eye(4)
    extrinsic[:3, :3] = rot
    extrinsic[:3, 3] = -rot @ pos
    fovy = float(model.vis.global_.fovy)
    f = 0.5 * HEIGHT / math.tan(math.radians(fovy) / 2.0)
    intrinsic = [[f, 0.0, WIDTH / 2.0], [0.0, f, HEIGHT / 2.0], [0.0, 0.0, 1.0]]
    return {
        "lookat": [float(v) for v in cam.lookat],
        "distance": float(cam.distance),
        "azimuth": float(cam.azimuth),
        "elevation": float(cam.elevation),
        "fovy_deg": fovy,
        "frustum": [float(gl.frustum_near), float(gl.frustum_far), float(gl.frustum_bottom),
                    float(gl.frustum_top), float(gl.frustum_center), float(gl.frustum_width)],
        "extrinsic_world_to_cam": np.round(extrinsic, 9).tolist(),
        "intrinsic": intrinsic,
        "size": [WIDTH, HEIGHT],
    }


def _render(renderer, data, cam, segmentation: bool):
    if segmentation:
        renderer.enable_segmentation_rendering()
    else:
        renderer.disable_segmentation_rendering()
    renderer.update_scene(data, camera=cam)
    return renderer.render()


def _outline(mask: np.ndarray, px: int = OUTLINE_PX) -> np.ndarray:
    """마스크 안쪽 가장자리 `px` 픽셀(4-이웃 침식을 px번 한 뒤의 차)."""
    inner = mask.copy()
    for _ in range(px):
        eroded = inner.copy()
        eroded[1:, :] &= inner[:-1, :]
        eroded[:-1, :] &= inner[1:, :]
        eroded[:, 1:] &= inner[:, :-1]
        eroded[:, :-1] &= inner[:, 1:]
        inner = eroded
    return mask & ~inner


def _write_png(path: Path, rgb: np.ndarray) -> None:
    from PIL import Image  # noqa: PLC0415

    Image.fromarray(rgb.astype(np.uint8)).save(path)


def _encode(frames_dir: Path, fps: int, out: Path) -> dict:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-framerate", str(fps), "-i", str(frames_dir / "%05d.png"),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
         "-g", str(fps), "-movflags", "+faststart", str(out)],
        check=True,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(out)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(probe.stdout)["streams"][0]


def run(request: dict, workdir: Path) -> dict:
    started = time.monotonic()
    params = request.get("params") or {}
    traj_in = Path(str(params["trajectory_path"]))
    if not traj_in.is_absolute():
        traj_in = workdir / traj_in
    ghost = bool(params.get("ghost", request.get("record", False)))
    mirror_root = Path(str(params.get("mirror_root", DEFAULT_MIRROR_ROOT))).expanduser()
    camera = {**CAMERA, **(params.get("camera") or {})}
    header, rows = read_trajectory(traj_in)
    if list(header["joint_names"]) != list(ARM_JOINTS):
        raise ValueError(f"미러 관절 순서와 다릅니다: {header['joint_names']}")
    fps = int(header["fps"])

    mujoco, sim_core, tool_sha = load_mirror(mirror_root)
    counter = StepCounter(mujoco)
    try:
        mirror = sim_core.SimMirror(width=WIDTH, height=HEIGHT)
        model, data = mirror.model, mirror.data
        removed = []
        for name in TASK_OBJECTS:
            mid = mirror.mocap_id[name]
            data.mocap_pos[mid] = (0.0, 0.0, -50.0)
            removed.append(name)
        data.mocap_pos[mirror.mocap_id[STAND_BODY]] = (0.0, 0.0, -50.0)
        desk_half_m = float(model.geom("desk_top").size[2])
        data.mocap_pos[mirror.mocap_id["desk"]] = (
            mirror.panel_to_sim((*DESK_CENTER_PANEL_XY, DESK_TOP_PANEL_M)) - np.array([0.0, 0.0, desk_half_m])
        )
        mujoco.mj_forward(model, data)
        desk_top_m = float(mirror.sim_to_panel(data.geom("desk_top").xpos)[2]) + desk_half_m
        site = model.site("graspframe").id

        def tcp(deg: dict) -> list[float]:
            mirror.set_pose_deg(deg, attach=False)
            return [float(v) for v in mirror.sim_to_panel(np.array(data.site_xpos[site]))]

        enriched = []
        for r in rows:
            cmd_deg = _pose_deg(r["q_cmd"], r.get("gripper_cmd"))
            obs_deg = _pose_deg(r["q_obs"], r.get("gripper_obs"))
            enriched.append({**r, "tcp_cmd_m": tcp(cmd_deg), "tcp_obs_m": tcp(obs_deg)})

        out_header = {
            **header,
            "tcp_frame": "so101_mirror_panel (팬 축 원점, 팔 기준 모델 좌표, m)",
            "trajectory_model": "SO-101 MuJoCo 미러(양팔 로봇 왼팔과 같은 SO-101 기구)",
            "joint_conversion": JOINT_CONVERSION,
            "mirror_tool_sha256": tool_sha,
        }
        traj_out = workdir / "trajectory_mujoco.jsonl"
        with traj_out.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(out_header, ensure_ascii=False) + "\n")
            for r in enriched:
                handle.write(json.dumps(r) + "\n")
        dist_mm = [math.dist(r["tcp_cmd_m"], r["tcp_obs_m"]) * 1000.0 for r in enriched]

        ghost_info = None
        if ghost:
            cam = mujoco.MjvCamera()
            cam.lookat[:] = mirror.panel_to_sim(camera["lookat_panel_m"])
            cam.distance = float(camera["distance"])
            cam.azimuth = float(camera["azimuth"])
            cam.elevation = float(camera["elevation"])
            renderer = mujoco.Renderer(model, height=HEIGHT, width=WIDTH)
            arm_geoms = np.array(sorted(_arm_geom_ids(model, mujoco)))
            frames_dir = workdir / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            cam_cmd = cam_obs = None
            matrices_equal = True
            hero = int(np.argmax(dist_mm)) if dist_mm else 0
            composite_png = workdir / "ghost_composite.png"
            for i, r in enumerate(rows):
                mirror.set_pose_deg(_pose_deg(r["q_cmd"], r.get("gripper_cmd")), attach=False)
                seg = _render(renderer, data, cam, True)
                cmd_rgb = _render(renderer, data, cam, False).astype(float)
                rec_cmd = _camera_record(renderer, cam, model)
                mirror.set_pose_deg(_pose_deg(r["q_obs"], r.get("gripper_obs")), attach=False)
                obs_rgb = _render(renderer, data, cam, False).astype(float)
                rec_obs = _camera_record(renderer, cam, model)
                if rec_cmd != rec_obs:
                    matrices_equal = False
                if i == 0:
                    cam_cmd, cam_obs = rec_cmd, rec_obs
                mask = (seg[..., 1] == int(mujoco.mjtObj.mjOBJ_GEOM)) & np.isin(seg[..., 0], arm_geoms)
                tinted = cmd_rgb * (1 - TINT_WEIGHT) + GHOST_TINT * TINT_WEIGHT
                out = obs_rgb.copy()
                out[mask] = obs_rgb[mask] * (1 - GHOST_ALPHA) + tinted[mask] * GHOST_ALPHA
                edge = _outline(mask)
                out[edge] = out[edge] * (1 - OUTLINE_ALPHA) + GHOST_OUTLINE * OUTLINE_ALPHA
                _write_png(frames_dir / f"{i:05d}.png", out)
                if i == hero:
                    _write_png(composite_png, out)
            renderer.close()
            video = workdir / "ghost.mp4"
            probe = _encode(frames_dir, fps, video)
            ghost_info = {
                "video_path": str(video),
                "video_sha256": _sha256(video),
                "video": probe,
                "composite_png": str(composite_png),
                "composite_frame": hero,
                "composite_t_s": float(rows[hero]["t"]) if rows else None,
                "alpha": GHOST_ALPHA,
                "command_tint_rgb": GHOST_TINT.tolist(),
                "tint_weight": TINT_WEIGHT,
                "outline_rgb": GHOST_OUTLINE.tolist(),
                "outline_px": OUTLINE_PX,
                "camera_command": cam_cmd,
                "camera_observed": cam_obs,
                "camera_matrices_equal": matrices_equal,
                "camera_setup_panel": camera,
                "label_ko": "명령(반투명)·관측",
                "caption_ko": "궤적 모델 = SO-101 MuJoCo 미러(양팔 로봇 왼팔과 같은 SO-101 기구)",
            }
        physics_steps = counter.count
        sim_time = float(data.time)
    finally:
        counter.restore()
    if physics_steps != 0 or sim_time != 0.0:
        raise RuntimeError(f"물리 스텝이 돌았습니다: steps={physics_steps}, time={sim_time}")

    result = {
        "schema": "robot-ops-mujoco-episode/1",
        "task": "lerobot_episode",
        "source": header.get("source"),
        "n_frames": len(rows),
        "fps": fps,
        "tcp_distance_mm": {"max": max(dist_mm) if dist_mm else None,
                            "argmax_t_s": float(rows[int(np.argmax(dist_mm))]["t"]) if dist_mm else None},
        "physics_steps": physics_steps,
        "sim_time_s": sim_time,
        "removed_task_objects": removed,
        "scene": {
            "layout": "desk_clamp",
            "removed_bodies": [STAND_BODY],
            "desk_top_panel_m": round(desk_top_m, 6),
            "note_ko": "녹화 배치(책상 클램프): 차량 받침대를 치우고 책상 상판을 팔 장착면(팬 축 아래 78 mm)에 둠",
        },
        "joint_conversion": JOINT_CONVERSION,
        "mirror_root": display_path(mirror_root),
        "tool_sha256": tool_sha,
        "ghost": ghost_info,
        "hardware_accessed": False,
    }
    result_path = workdir / "mujoco_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "run_dir": str(workdir),
        "result_path": str(result_path),
        "trajectory_path": str(traj_out),
        "video_path": ghost_info["video_path"] if ghost_info else None,
        "composite_png": ghost_info["composite_png"] if ghost_info else None,
        "camera_matrices_equal": ghost_info["camera_matrices_equal"] if ghost_info else None,
        "physics_steps": physics_steps,
        "sim_time_s": 0.0,
        "wall_s": round(time.monotonic() - started, 3),
        "tool_sha256": tool_sha,
        "hardware_accessed": False,
    }


def handle(verb: str, task: str, request: dict, workdir: Path) -> dict:
    return {"status": "ok", **run(request, workdir)}
