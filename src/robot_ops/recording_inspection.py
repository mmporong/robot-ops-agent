"""저장된 궤적·녹화 묶음의 검사 결과와 확인 화면을 만든다. 실물·시뮬 실행은 없다."""

from __future__ import annotations

import html
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .replay.artifacts import sha256_file, write_json
from .replay.divergence import read_trajectory
from .replay.profile import Profile, display_path, expand_path, load_profile
from .replay.recording_check import inspect_recording

LABELS = {"shoulder_pan": "어깨 회전", "shoulder_lift": "어깨 올림", "elbow_flex": "팔꿈치",
          "wrist_flex": "손목 굽힘", "wrist_roll": "손목 회전", "gripper": "그리퍼"}
ASSETS = Path(__file__).parent / "replay/report_assets"


def inspect_source_identity(rows: list[dict], *, min_repeat_s: float = 0.3, max_gap_s: float = 0.25) -> dict:
    """선택 메타데이터의 반복을 검사한다. 서로 다른 시계의 절대값을 빼지 않는다."""
    fields = [key for key in ("source_sample_timestamp_s", "source_sequence") if any(r.get(key) is not None for r in rows)]
    if not fields:
        return {"status": "not_assessable", "fields": [], "missing_frames": len(rows), "issues": [],
                "reason_ko": "원본 센서의 측정 시각·순서 번호가 없어 갱신 여부를 확인할 수 없습니다."}
    available = []
    for row in rows:
        for field in fields:
            value = row.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{field}: 유한한 수치가 필요합니다")
            if field == "source_sequence" and (not isinstance(value, int) or value < 0):
                raise ValueError("source_sequence: 0 이상의 정수여야 합니다")
        available.append(all(row.get(field) is not None for field in fields))
    issues, anchor = [], None

    def close(end: int) -> None:
        if anchor is None or end <= anchor:
            return
        duration = rows[end]["t"] - rows[anchor]["t"]
        if duration >= min_repeat_s - 1e-9:
            issues.append({"kind": "source_identity_stagnation", "joint": "source_metadata",
                           "reference_frame": anchor, "frame_start": anchor + 1, "frame_end": end,
                           "n_frames": end - anchor, "t_start_s": rows[anchor + 1]["t"], "t_end_s": rows[end]["t"],
                           "duration_s": rows[end]["t"] - rows[anchor + 1]["t"], "evidence_duration_s": duration,
                           "command_span": 0.0, "observation_span": 0.0, "unit": "s", "symptom_only": True,
                           "reason_ko": "기록된 센서 측정 식별자가 검사 기간 동안 바뀌지 않아 수집 주기 확인이 필요합니다."})

    for i, row in enumerate(rows):
        if not available[i]:
            close(i - 1)
            anchor = None
            continue
        if anchor is None:
            anchor = i
        elif row["t"] - rows[i - 1]["t"] > max_gap_s or any(row[f] != rows[i - 1].get(f) for f in fields):
            close(i - 1)
            anchor = i
    close(len(rows) - 1)
    return {"status": "checked_source_identity", "fields": fields, "missing_frames": available.count(False),
            "issues": issues, "min_repeat_s": min_repeat_s,
            "reason_ko": "기록된 측정 식별자의 반복을 검사했습니다. 센서 주기와 수집기 상태는 별도 확인이 필요합니다."}


def check_rows(header: dict, rows: list[dict]) -> dict:
    check = inspect_recording(header, rows)
    source = inspect_source_identity(rows, min_repeat_s=check["parameters"]["min_hold_s"],
                                     max_gap_s=check["parameters"]["effective_max_gap_s"])
    check["coverage"]["observation_freshness"] = source["status"]
    check["coverage"]["freshness"] = {k: v for k, v in source.items() if k != "issues"}
    check["coverage"]["observation_freshness_reason_ko"] = source["reason_ko"]
    check["issues"].extend(source["issues"])
    check["issues"].sort(key=lambda item: (item["frame_start"], item["kind"], item["joint"]))
    check["status"] = "review_required" if check["issues"] else "no_issue_in_checked_scope"
    return check


def _matching_media(header: dict, profile: Profile | None, out: Path, rows: list[dict]) -> dict:
    media = {"real": None, "ghost": None, "note_ko": "이 기록과 출처가 일치하는 녹화가 없습니다."}
    if profile is None:
        return media
    from .replay.report import latest_replay_results
    source = header.get("source") or {}
    key = (display_path(expand_path(source["path"], profile.base_dir)), int(source["episode"]))
    replay = latest_replay_results(profile.artifacts_root).get(key)
    if not replay or replay["source"].get("sha256") != source.get("sha256"):
        return media
    # 같은 원본 파일이어도 가공된 자세가 다르면 녹화와 섞지 않는다.
    recorded_header, recorded_rows = read_trajectory(expand_path(replay["trajectory_path"], profile.base_dir))
    if recorded_header["joint_names"] != header["joint_names"] or len(rows) != len(recorded_rows):
        return media
    for row, recorded in zip(rows, recorded_rows):
        if any(row.get(k) != recorded.get(k) for k in ("t", "q_cmd", "q_obs", "gripper_cmd", "gripper_obs")):
            return media
    candidates = {"real": (replay.get("convert_video") or {}).get("path"), "ghost": replay.get("video_path")}
    copied = {}
    for kind, raw_path in candidates.items():
        if not raw_path:
            continue
        src = expand_path(raw_path, profile.base_dir)
        if not src.is_file():
            continue
        (out / "media").mkdir(parents=True, exist_ok=True)
        target = out / "media" / f"{kind}.mp4"
        shutil.copy2(src, target)
        copied[kind] = f"media/{kind}.mp4"
        media[f"{kind}_sha256"] = sha256_file(src)
    media.update(copied)
    if copied:
        media["note_ko"] = "같은 기록의 실물 녹화와 저장된 모델 재생입니다. 노란 팔은 기록된 위치값, 파란 팔은 목표 명령입니다."
    return media


def _page(data: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in ("inspection.js", "inspection.css"):
        shutil.copy2(ASSETS / name, out / name)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    page = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'">
<title>로봇 수집 기록 검사</title><link rel="icon" href="data:,"><link rel="stylesheet" href="inspection.css">
<script src="inspection.js" defer></script></head><body><main id="inspection-app"></main>
<script type="application/json" id="inspection-data">{encoded}</script><noscript>검사 결과 JSON을 확인해 주세요.</noscript></body></html>'''
    (out / "index.html").write_text(page, encoding="utf-8")
    write_json(out / "inspection.json", data)


def inspect_path(path: Path, out: Path, *, profile: Profile | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".jsonl":
            raise ValueError("검사 입력은 일반 .jsonl 궤적 파일이어야 합니다")
        header, rows = read_trajectory(path)
        check = check_rows(header, rows)
        series = [{"name": name, "label_ko": LABELS.get(name, name), "unit": "deg",
                   "command": [math.degrees(r["q_cmd"][j]) for r in rows],
                   "observation": [math.degrees(r["q_obs"][j]) for r in rows]}
                  for j, name in enumerate(header["joint_names"])]
        if check["coverage"]["gripper_checked"]:
            series.append({"name": "gripper", "label_ko": "그리퍼", "unit": "lerobot_raw",
                           "command": [r.get("gripper_cmd") for r in rows], "observation": [r.get("gripper_obs") for r in rows]})
        poses = [(*r["q_obs"], r.get("gripper_obs")) for r in rows]
        try:
            media = _matching_media(header, profile, out, rows)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            media = {"real": None, "ghost": None,
                     "note_ko": "저장된 녹화를 확인하지 못했습니다. 관절 기록 검사 결과와 그래프는 확인할 수 있습니다.",
                     "lookup_error": type(exc).__name__}
        data = {"schema": "robot-ops-recording-inspection/1", "title_ko": "관절 상태가 명령을 따라 변하는지 검사합니다",
                "status": check["status"], "check": check,
                "summary": {"frame_count": len(rows), "duration_s": rows[-1]["t"] - rows[0]["t"],
                            "observation_unique_poses": len(set(poses)),
                            "observation_change_times_s": [rows[i]["t"] for i in range(1, len(rows)) if poses[i] != poses[i - 1]]},
                "source": {"label": f"로봇 수집 기록 · 에피소드 {header['source']['episode']}", "sha256": sha256_file(path)},
                "series": {"t": [r["t"] for r in rows], "joints": series},
                "media": media,
                "reproducibility": {"detector": check["detector"], "parameters": check["parameters"], "field_validated": False}}
    except (ValueError, TypeError, KeyError, OSError) as exc:
        data = {"schema": "robot-ops-recording-inspection/1", "title_ko": "기록을 검사할 수 없습니다",
                "status": "invalid_input", "error_ko": str(exc), "check": {"issues": [], "coverage": {}},
                "summary": {}, "source": {"label": path.name}, "series": {"t": [], "joints": []}, "media": {}}
    _page(data, out)
    return data


def inspect_corpus(path: Path, out: Path, *, profile: Profile | None = None) -> dict:
    from .replay.field_eval import input_identity

    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "robot-ops-field-corpus/1" or manifest.get("errors"):
        raise ValueError("오류 없는 정규화 corpus manifest가 필요합니다")
    entries, validated = [], {}
    metadata = manifest.get("episodes")
    if not isinstance(metadata, list) or not metadata or any(
        not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
        or not isinstance(entry.get("trajectory_path"), str)
        or any(not isinstance(entry.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", entry[key])
               for key in ("trajectory_sha256", "identity_sha256"))
        or entry.get("duplicate_of") is not None and not isinstance(entry["duplicate_of"], str)
        for entry in metadata
    ):
        raise ValueError("corpus에 경로·ID·SHA-256 해시가 있는 기록 목록이 필요합니다")
    by_id = {entry["id"]: entry for entry in metadata}
    if len(by_id) != len(metadata) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", key) for key in by_id):
        raise ValueError("corpus의 기록 ID가 중복되었거나 경로로 사용할 수 없습니다")
    for entry in metadata:
        trajectory = expand_path(entry["trajectory_path"], path.parent)
        if trajectory.is_symlink() or not trajectory.is_file() or trajectory.suffix.lower() != ".jsonl":
            raise ValueError(f"corpus 입력이 일반 JSONL 파일이 아닙니다: {entry['id']}")
        if sha256_file(trajectory) != entry["trajectory_sha256"]:
            raise ValueError(f"입력 해시가 다릅니다: {entry['id']}")
        header, rows = read_trajectory(trajectory)
        identity = input_identity(header, rows)
        if identity != entry.get("identity_sha256"):
            raise ValueError(f"입력 내용 identity가 다릅니다: {entry['id']}")
        validated[entry["id"]] = (trajectory, identity, header.get("recording_check"))
    owners = {}
    for entry in metadata:
        trajectory, identity, settings = validated[entry["id"]]
        target_id = entry.get("duplicate_of")
        if target_id:
            target = by_id.get(target_id)
            if not target or target is entry or target.get("duplicate_of") or validated[target_id][1:] != (identity, settings):
                raise ValueError(f"중복 대상이 없거나 검사 입력이 다릅니다: {entry['id']}")
            continue
        if identity in owners and validated[owners[identity]][2] == settings:
            raise ValueError(f"중복 표시가 없는 동일 입력입니다: {entry['id']}")
        owners[identity] = entry["id"]
        subdir = out / "records" / entry["id"]
        data = inspect_path(trajectory, subdir, profile=profile)
        entries.append({"id": entry["id"], "status": data["status"], "issues": len(data["check"]["issues"]),
                        "href": f"records/{entry['id']}/index.html"})
    out.mkdir(parents=True, exist_ok=True)
    labels = {"review_required": "확인 필요", "no_issue_in_checked_scope": "검사 항목에서 경고 없음", "invalid_input": "검사 불가"}
    ordered = sorted(entries, key=lambda e: (e["status"] == "no_issue_in_checked_scope", -e["issues"], e["id"]))
    rows = "".join(f'<tr><td><a href="{html.escape(e["href"], quote=True)}">{html.escape(e["id"])}</a></td><td>{labels[e["status"]]}</td><td>{e["issues"]}</td></tr>' for e in ordered)
    (out / "index.html").write_text(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>수집 기록 검사 결과</title>
<link rel="stylesheet" href="inspection.css"><body><main class="batch-report"><h1>확인이 필요한 수집 기록을 골랐습니다</h1><p>고유 입력 {len(entries)}건 · 확인 필요 {sum(e['status']=='review_required' for e in entries)}건 · 검사 불가 {sum(e['status']=='invalid_input' for e in entries)}건</p>
<p>경고가 없다는 표시는 검사한 항목에 한정됩니다. 실제 고장 원인과 학습 사용 가능 여부는 별도 확인이 필요합니다.</p><table><thead><tr><th>기록</th><th>검사 결과</th><th>확인 구간</th></tr></thead><tbody>{rows}</tbody></table></main></body></html>''', encoding="utf-8")
    shutil.copy2(ASSETS / "inspection.css", out / "inspection.css")
    summary = {"schema": "robot-ops-recording-inspection-batch/1", "records": entries,
               "deployment_verdict": "not_validated", "independent_labels_available": False}
    write_json(out / "inspection.json", summary)
    return summary


def command(args) -> int:
    profile = load_profile(args.profile) if args.profile else None
    out = args.out.expanduser().resolve()
    if args.dataset_root:
        if profile is None or "lerobot" not in profile.interpreters:
            raise ValueError("데이터셋 폴더 검사는 --profile의 lerobot 판독 환경이 필요합니다")
        reader = profile.base_dir / "adapters/bimanual/evaluate_recordings.py"
        subprocess.run([str(profile.interpreters["lerobot"]), "-B", str(reader),
                        "--root", str(args.dataset_root.expanduser().resolve()), "--out", str(out / "corpus"),
                        "--inventory", str(profile.base_dir / "evaluations/replay/inventory.json")],
                       check=True, timeout=180, capture_output=True, text=True)
        data = inspect_corpus(out / "corpus/manifest.json", out, profile=profile)
    elif args.corpus:
        data = inspect_corpus(args.corpus.expanduser().resolve(), out, profile=profile)
    else:
        data = inspect_path(args.trajectory.expanduser().absolute(), out, profile=profile)
    print(json.dumps({"report": display_path(out / "index.html"), "status": data.get("status", "batch_complete"),
                      "records": len(data.get("records", [])), "field_validated": False}, ensure_ascii=False, indent=2))
    return 2 if data.get("status") == "invalid_input" else 0
