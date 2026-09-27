"""M0 인벤토리 스캔 (계획 §1, §3.5, §6 M0). 읽기 전용.

SO-101 LeRobot v3.0 데이터셋, 관측 동결, 정적 구간 교정 차, 영상 시점,
양팔 로봇 프로젝트 result.json, URDF 왼팔 관절 매핑, MuJoCo 미러 파일 SHA를 모은다.
실물 로봇·모터 모듈은 import하지 않는다. 팀 저장소에는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

HOME = Path.home()
USER = os.environ.get("USER", "lim")
REPO_ROOT = Path(__file__).resolve().parents[2]
DATASETS = HOME / "so101_datasets"
TEAM_REPO = HOME / "bimanual-robot"
ARTIFACTS = Path(f"/data/{USER}/robot-artifacts")
MIRROR = HOME / "so101_tools"
URDF = "src/hold_flow_description/urdf/hold_flow.urdf"
CUP_TOOLS = ["tools/simulate_cup_contact.py", "tools/cup_contact_model.py", "tools/cup_contact_recovery.py"]
LEROBOT_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
M0_DIR = REPO_ROOT / ".local" / "m0"

# 관측 동결 후보. 단위 = 도(LeRobot DEGREES), 프레임 간 차분
FREEZE_CANDIDATES = [{"eps_deg": 0.05, "delta_deg": 0.5, "k": 3}, {"eps_deg": 0.1, "delta_deg": 0.5, "k": 3}]
FREEZE_CHOSEN = {"eps_deg": 0.1, "delta_deg": 0.5, "k": 3}
# 정적 구간: state·action 모두 프레임 간 |Δ| < 0.1° (전 관절), 연속 ≥ 5프레임
STATIC = {"eps_deg": 0.1, "k": 5}
CALIB_SUSPECT_DEG = 5.0
INFRA_RE = re.compile(r"^\w+(Error|Exception)\b|Errno")


def norm(path: Path | str) -> str:
    s = str(path)
    for real, token in ((f"/data/{USER}", "/data/$USER"), (str(HOME), "~")):
        if s == real or s.startswith(real + "/"):
            return token + s[len(real):]
    return s


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(TEAM_REPO), *args], check=True,
                          capture_output=True, text=True).stdout


def git_blob_sha256(rev: str, path: str) -> str:
    data = subprocess.run(["git", "-C", str(TEAM_REPO), "show", f"{rev}:{path}"],
                          check=True, capture_output=True).stdout
    return hashlib.sha256(data).hexdigest()


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def freeze_stats(state: np.ndarray, action: np.ndarray, ts: np.ndarray, eps: float, delta: float, k: int) -> dict:
    """|Δstate| < eps (전 관절) 이고 |Δaction| > delta (한 관절 이상)인 차분 프레임의 연속 ≥ k 구간."""
    ds = np.abs(np.diff(state, axis=0))
    da = np.abs(np.diff(action, axis=0))
    mask = (ds < eps).all(1) & (da > delta).any(1)
    segs = [(a, b) for a, b in runs(mask) if b - a >= k]
    frozen = sum(b - a for a, b in segs)
    longest = max(segs, key=lambda s: s[1] - s[0], default=None)
    out = {"ratio": round(frozen / max(len(mask), 1), 4), "frozen_frames": int(frozen),
           "runs": len(segs), "longest_run_frames": int(longest[1] - longest[0]) if longest else 0}
    if longest:
        # 차분 인덱스 i는 프레임 i→i+1 전환이므로 구간 = ts[a] ~ ts[b]
        out["longest_run_t_s"] = [round(float(ts[longest[0]]), 3), round(float(ts[longest[1]]), 3)]
    return out


def static_mask(state: np.ndarray, action: np.ndarray) -> np.ndarray:
    ds = np.abs(np.diff(state, axis=0))
    da = np.abs(np.diff(action, axis=0))
    m = (ds < STATIC["eps_deg"]).all(1) & (da < STATIC["eps_deg"]).all(1)
    keep = np.zeros(len(state), bool)
    for a, b in runs(m):
        if b - a >= STATIC["k"]:
            keep[a:b + 1] = True
    return keep


def scan_dataset(ds: Path) -> tuple[dict, list[dict]]:
    info = json.loads((ds / "meta" / "info.json").read_text())
    feats = info["features"]
    cams = {k: {"height": v["shape"][0], "width": v["shape"][1], "codec": v.get("info", {}).get("video.codec")}
            for k, v in feats.items() if v.get("dtype") == "video"}
    ep_files = sorted(glob.glob(str(ds / "meta/episodes/chunk-*/*.parquet")))
    cols = ["episode_index", "length", "data/chunk_index", "data/file_index", "dataset_from_index", "dataset_to_index"]
    eps_meta = pa.concat_tables([pq.read_table(f, columns=cols) for f in ep_files]).to_pylist()
    data_links = {p: norm(os.path.realpath(ds / p)) for p in ("data", "videos") if (ds / p).is_symlink()}
    data_files = sorted({(e["data/chunk_index"], e["data/file_index"]) for e in eps_meta})
    tables, file_sha = [], {}
    for c, f in data_files:
        p = ds / info["data_path"].format(chunk_index=c, file_index=f)
        file_sha[str(p.relative_to(ds))] = sha256_file(p)
        tables.append(pq.read_table(p, columns=["observation.state", "action", "episode_index", "timestamp"]))
    t = pa.concat_tables(tables)
    state = np.array(t["observation.state"].to_pylist(), float)
    action = np.array(t["action"].to_pylist(), float)
    ep_idx = np.array(t["episode_index"].to_pylist())
    ts = np.array(t["timestamp"].to_pylist(), float)

    episodes, static_diffs, static_eps = [], [], 0
    for e in eps_meta:
        m = ep_idx == e["episode_index"]
        s, a, tt = state[m], action[m], ts[m]
        row = {"episode": e["episode_index"], "frames": int(m.sum()), "meta_length": e["length"],
               "freeze": {f"eps{c['eps_deg']}_delta{c['delta_deg']}_k{c['k']}":
                          freeze_stats(s, a, tt, c["eps_deg"], c["delta_deg"], c["k"]) for c in FREEZE_CANDIDATES}}
        row["state_unique_rows"] = int(len(np.unique(s, axis=0)))
        # 에피소드 전체에서 state가 1~2값뿐이면 전 구간 동결: 교정 차·재생 후보에서 뺀다
        row["state_constant_episode"] = row["state_unique_rows"] <= 2
        sm = static_mask(s, a)
        row["static_frames"] = int(sm.sum())
        if sm.any() and not row["state_constant_episode"]:
            static_diffs.append(a[sm] - s[sm])
            static_eps += 1
        episodes.append(row)
    names = feats["observation.state"]["names"]
    calib = None
    if static_diffs:
        d = np.concatenate(static_diffs)
        mean = d.mean(0)
        calib = {"frames": int(len(d)), "episodes_used": static_eps,
                 "mean_action_minus_state_deg": {n: round(float(v), 3) for n, v in zip(names, mean)},
                 "std_deg": {n: round(float(v), 3) for n, v in zip(names, d.std(0))},
                 "calibration_suspect": [n for n, v in zip(names, mean) if abs(v) > CALIB_SUSPECT_DEG]}
    summary = {
        "path": norm(ds), "codebase_version": info.get("codebase_version"), "fps": info["fps"],
        "robot_type": info.get("robot_type"),
        "episodes": len(eps_meta), "frames": int(sum(e["length"] for e in eps_meta)),
        "total_frames_info": info.get("total_frames"),
        "features": sorted(feats), "joint_names": names,
        "camera_keys": cams, "data_parquet_sha256": file_sha,
        "symlinks": data_links or None,
        "static_calibration": calib,
    }
    return summary, episodes


def extract_frame(video: Path, t_s: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t_s), "-i", str(video),
                    "-frames:v", "1", str(out)], check=True)


def scan_results() -> dict:
    found = {}
    for root in (TEAM_REPO / "logs", ARTIFACTS):
        for r, _dirs, files in os.walk(root, followlinks=True):
            if "result.json" in files:
                p = os.path.join(r, "result.json")
                found.setdefault(os.path.realpath(p), p)
    head_tool_sha = {Path(p).name: git_blob_sha256("HEAD", p) for p in CUP_TOOLS}
    families = collections.defaultdict(lambda: collections.Counter())
    cup = collections.defaultdict(lambda: {"runs": 0, "fields_present": collections.Counter(),
                                           "real_finray_grasp_verified_true": 0, "recovery_enabled_true": 0,
                                           "hardware_accessed_true": 0, "tool_sha256_match_head": collections.Counter(),
                                           "observation_source": collections.Counter()})
    by_root = collections.Counter()
    for real in sorted(found):
        by_root["logs" if real.startswith(str(TEAM_REPO)) else "robot-artifacts"] += 1
        rp = Path(real)
        rel = rp.relative_to(TEAM_REPO / "logs") if real.startswith(str(TEAM_REPO / "logs")) else rp.relative_to(ARTIFACTS)
        top = rel.parts[0]
        try:
            d = json.loads(rp.read_text())
        except (OSError, json.JSONDecodeError):
            families[top]["unreadable"] += 1
            continue
        fam = f"{top}:{d['mode']}" if top == "restaurant" and d.get("mode") else top
        if "task_pass" in d:
            key = "task_pass"
        elif "rigid_proxy_lift_pass" in d:
            key = "rigid_proxy_lift_pass"
        elif "completed" in d:
            key = "completed"
        else:
            families[fam]["not_a_run"] += 1
            continue
        failure = d.get("failure") or d.get("stop_reason") or ""
        if d[key]:
            families[fam]["pass"] += 1
        elif isinstance(failure, str) and INFRA_RE.search(failure):
            families[fam]["infra"] += 1
        else:
            families[fam]["fail"] += 1
        families[fam][f"pass_key:{key}"] += 1
        if top.startswith("cup_"):
            plan_p = rp.parent / "plan.json"
            plan = json.loads(plan_p.read_text()) if plan_p.exists() else {}
            c = cup[top]
            c["runs"] += 1
            for fld in ("task_pass", "rigid_proxy_lift_pass", "real_finray_grasp_verified", "recovery_enabled",
                        "hardware_accessed"):
                where = "result" if fld in d else ("plan" if fld in plan else None)
                c["fields_present"][f"{fld}@{where}"] += 1
            c["real_finray_grasp_verified_true"] += int(bool(d.get("real_finray_grasp_verified")))
            c["recovery_enabled_true"] += int(bool(d.get("recovery_enabled", plan.get("recovery_enabled"))))
            c["hardware_accessed_true"] += int(bool(d.get("hardware_accessed")))
            c["observation_source"][str(d.get("observation_source", plan.get("observation_source")))] += 1
            for name, sha in (plan.get("tool_sha256") or {}).items():
                c["tool_sha256_match_head"][f"{name}:{'same' if head_tool_sha.get(name) == sha else 'differs'}"] += 1
    return {
        "roots": [norm(TEAM_REPO / "logs"), norm(ARTIFACTS)],
        "dedupe": "os.walk(followlinks=True) 후 realpath 기준 중복 제거",
        "unique_result_json": len(found), "by_realpath_root": dict(by_root),
        "pass_rule": "task_pass → rigid_proxy_lift_pass → completed 순으로 첫 키. 실패 문자열이 예외·Errno면 infra",
        "families": {k: dict(v) for k, v in sorted(families.items())},
        "cup_families": {k: {kk: (dict(vv) if isinstance(vv, collections.Counter) else vv) for kk, vv in v.items()}
                         for k, v in sorted(cup.items())},
        "cup_tools_head_sha256": head_tool_sha,
        "tool_sha256_source": "각 실행 폴더 plan.json의 tool_sha256",
    }


def urdf_left_joints() -> dict:
    root = ET.fromstring(git("show", f"HEAD:{URDF}"))
    joints = {j.get("name"): j for j in root.iter("joint")}
    mapping = {}
    for n in LEROBOT_JOINTS:
        j = joints.get(f"left_{n}")
        lim = j.find("limit") if j is not None else None
        mapping[n] = None if j is None else {
            "urdf": f"left_{n}", "type": j.get("type"),
            "limit": {k: float(lim.get(k)) for k in ("lower", "upper", "effort", "velocity")} if lim is not None else None}
    return {"file": URDF, "sha256": git_blob_sha256("HEAD", URDF),
            "left_joints_all": sorted(n for n in joints if n.startswith("left_")), "lerobot_to_urdf": mapping}


def mirror_files() -> dict:
    # arm_lib.load_mapping()은 arm_lib.py 옆 mapping.json을 읽는다(없으면 기본값을 써 넣으므로 호출하지 않는다)
    files = {"mapping": MIRROR / "mapping.json", "arm_lib": MIRROR / "arm_lib.py",
             "sim_frame": MIRROR / "sim" / "sim_frame.json", "mjcf": MIRROR / "sim" / "so101_new_calib.xml",
             "sim_core": MIRROR / "sim" / "sim_core.py"}
    return {"root": norm(MIRROR / "sim"),
            "load_mapping_reads": norm(files["mapping"]),
            "files": {k: {"path": norm(p), "sha256": sha256_file(p) if p.exists() else None} for k, p in files.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "evaluations/replay/inventory.json")
    args = ap.parse_args()

    datasets, freeze_table = {}, {}
    for ds in sorted(DATASETS.glob("so101_*")):
        if not (ds / "meta" / "info.json").exists():
            continue
        summary, eps = scan_dataset(ds)
        datasets[ds.name] = summary
        freeze_table[ds.name] = eps

    chosen_key = f"eps{FREEZE_CHOSEN['eps_deg']}_delta{FREEZE_CHOSEN['delta_deg']}_k{FREEZE_CHOSEN['k']}"
    other_key = f"eps{FREEZE_CANDIDATES[0]['eps_deg']}_delta{FREEZE_CANDIDATES[0]['delta_deg']}_k{FREEZE_CANDIDATES[0]['k']}"
    agree = all(e["freeze"][chosen_key] == e["freeze"][other_key] for v in freeze_table.values() for e in v)
    # 재생 후보: 선택 기준 최장 연속 구간이 가장 긴 에피소드. _fix·심링크 파생·전 구간 동결 에피소드 제외
    best = None
    for name, eps in freeze_table.items():
        if name.endswith("_fix") or datasets[name]["symlinks"]:
            continue
        for e in eps:
            if e["state_constant_episode"]:
                continue
            f = e["freeze"][chosen_key]
            if f["longest_run_frames"] and (best is None or f["longest_run_frames"] > best[2]["longest_run_frames"]):
                best = (name, e["episode"], f)
    replay_candidate = None
    if best:
        replay_candidate = {"dataset": best[0], "episode": best[1],
                            "t_start_s": best[2]["longest_run_t_s"][0], "t_end_s": best[2]["longest_run_t_s"][1],
                            "frames": best[2]["longest_run_frames"], "episode_freeze_ratio": best[2]["ratio"],
                            "cameras": sorted(datasets[best[0]]["camera_keys"]),
                            "rule": ("선택 기준 최장 연속 동결 구간이 가장 긴 에피소드. _fix·심링크 파생 데이터셋과 "
                                     "state가 에피소드 전체에서 상수인 에피소드(동결 시작 시각이 없음)는 제외")}
    constant_eps = {n: [e["episode"] for e in v if e["state_constant_episode"]] for n, v in freeze_table.items()}
    constant_eps = {n: v for n, v in constant_eps.items() if v}

    # 영상 시점: 재생 후보 에피소드에서 depth·wrist 각 1프레임
    view = None
    both = [n for n, d in datasets.items() if {"observation.images.depth", "observation.images.wrist"} <= set(d["camera_keys"])
            and not d["symlinks"] and not n.endswith("_fix")]
    if both:
        # 두 카메라가 모두 있는 데이터셋(후보 데이터셋 우선)의 에피소드 0, 5초 지점
        vname = replay_candidate["dataset"] if replay_candidate and replay_candidate["dataset"] in both else both[-1]
        ds = DATASETS / vname
        ep = replay_candidate["episode"] if replay_candidate and vname == replay_candidate["dataset"] else 0
        cols = ["episode_index"] + [f"videos/observation.images.{c}/{x}" for c in ("depth", "wrist")
                                    for x in ("chunk_index", "file_index", "from_timestamp")]
        meta = pa.concat_tables([pq.read_table(f, columns=cols)
                                 for f in sorted(glob.glob(str(ds / "meta/episodes/chunk-*/*.parquet")))]).to_pylist()
        m = next(r for r in meta if r["episode_index"] == ep)
        info = json.loads((ds / "meta/info.json").read_text())
        pngs = {}
        for cam in ("depth", "wrist"):
            key = f"observation.images.{cam}"
            vid = ds / info["video_path"].format(video_key=key, chunk_index=m[f"videos/{key}/chunk_index"],
                                                  file_index=m[f"videos/{key}/file_index"])
            t = m[f"videos/{key}/from_timestamp"] + 5.0
            out = M0_DIR / f"{vname}_ep{ep}_{cam}.png"
            extract_frame(vid, t, out)
            pngs[cam] = {"video": norm(vid), "video_sha256": sha256_file(vid), "t_s": round(t, 3),
                         "png": str(out.relative_to(REPO_ROOT))}
        view = {
            "dataset": vname, "episode": ep,
            "frames": pngs,
            "judgement": {
                "observation.images.depth": "third_person",
                "observation.images.wrist": "wrist",
            },
            "note_ko": ("depth 키 영상은 깊이 맵이 아니라 RGB 3인칭 고정 시점이다(팔 전체·책상·큐브가 보임). "
                        "wrist 키는 손목 카메라 시점(그리퍼 앞 근거리). 판단은 PNG 육안 확인."),
        }

    team_head = git("rev-parse", "HEAD").strip()
    out = {
        "schema": "robot-ops-inventory/1",
        "generated_by": "adapters/bimanual/inventory_scan.py",
        "so101": {
            "root": norm(DATASETS),
            "datasets": datasets,
            "totals": {"datasets": len(datasets), "episodes": sum(d["episodes"] for d in datasets.values()),
                       "frames": sum(d["frames"] for d in datasets.values()),
                       "note": "_fix·심링크 파생(pick_pm_wrist→pick_pm) 중복 포함"},
        },
        "freeze": {
            "definition": ("차분 프레임 i→i+1에서 모든 관절(그리퍼 포함) |Δstate| < ε 이고 어느 관절이든 |Δaction| > δ, "
                           "이런 프레임이 연속 k개 이상인 구간만 센다. ratio = 구간 프레임 합 / 차분 프레임 수. 단위 도"),
            "candidates": FREEZE_CANDIDATES,
            "chosen": FREEZE_CHOSEN,
            "chosen_reason": ("두 후보가 모든 에피소드에서 같은 결과(ε 비민감)"
                              if agree else "두 후보 결과가 다르다. 계획 후보값(0.1°)을 쓴다") +
                             ". 계획 §6 M0 후보값 0.1°/0.5°/3을 확정 기준으로 쓴다",
            "candidates_agree": agree,
            "per_episode": freeze_table,
        },
        "replay_candidate": replay_candidate,
        "state_constant_episodes": {
            "episodes": constant_eps,
            "note_ko": ("observation.state가 에피소드 전체에서 1~2개 값뿐인 에피소드. 동결이 전 구간에 걸쳐 시작 시각이 없으므로 "
                        "재생 후보와 교정 차 계산에서 뺀다. 이 에피소드의 동결 ratio는 action이 움직인 구간 비율과 같다"),
        },
        "static_calibration": {
            "definition": (f"state·action 모두 전 관절 프레임 간 |Δ| < {STATIC['eps_deg']}°가 연속 {STATIC['k']}차분 이상인 "
                           "프레임에서 관절별 평균 action − state(도). 데이터셋별 값은 so101.datasets.*.static_calibration"),
            "calibration_suspect_threshold_deg": CALIB_SUSPECT_DEG,
            "replay_candidate_dataset": (datasets[replay_candidate["dataset"]]["static_calibration"]
                                         if replay_candidate else None),
        },
        "video_viewpoint": view,
        "bimanual": {
            "repo": norm(TEAM_REPO), "head_commit": team_head,
            "results": scan_results(),
            "urdf_left_arm": urdf_left_joints(),
        },
        "mujoco_mirror": mirror_files(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    for real in (str(HOME), f"/data/{USER}"):
        assert real + "/" not in text, f"정규화되지 않은 경로: {real}"
    args.out.write_text(text)
    print(json.dumps({"replay_candidate": replay_candidate, "unique_result_json": out["bimanual"]["results"]["unique_result_json"],
                      "freeze_candidates_agree": agree}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
