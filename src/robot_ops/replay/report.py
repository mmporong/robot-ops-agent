"""`robot-ops report build` — 정적 리포트(데이터 JSON + 화면 자산 + 미디어) 굽기.

- 기본은 공개 빌드: profile `[public] exclude_status`(after_fix_regression)에 해당하는
  시나리오는 카드·regress 데이터·미디어·개수까지 전부 뺀다(존재 자체를 드러내지 않는다).
- `--private`일 때만 전체 포함(로컬 확인용, 배포 금지). 출력 폴더를 따로 주지 않으면
  `<report_dir>-private`(예: `.local/report-private`)에 구워 공개 폴더를 덮어쓰지 않는다.
- 공개 빌드는 마지막에 산출물 전체 바이트에서 제외 시나리오 id·제외 status 문자열을 찾고, 있으면 빌드 실패.
- 이전 빌드 잔재(비공개 빌드의 JSON·미디어)가 공개 빌드에 남지 않도록 `data/`를 비우고,
  `media/`는 이번 빌드가 쓴 파일만 남긴다.
- 출력 `<report_dir>/{index.html, app.js, style.css, data/*.json, media/*}`. 서버·빌드 도구 없음.
- 모든 표시 경로는 `~`·`/data/$USER`로 정규화하고, 마지막에 빌드 산출물 전체에서
  `/home/<user>`·`/data/<user>`가 남지 않았는지 검사한다(남으면 빌드 실패).

히어로 영상 소스(`hero_source`, 환경변수 `ROBOT_OPS_HERO_SOURCE`로도 지정):
- `auto`(기본): 컵 격자 regress 결과에 조건 A 녹화가 있으면 `cup_grid`, 없으면 `observation_freeze`.
- `cup_grid`: 조건 A 실패 격자점 클립(접촉 순간 강조) → 같은 격자점 조건 B 클립 → 통과 지도 A|B.
  조건 A 실패가 0이면 판정 여유가 가장 작은 조건 A 셀의 녹화로 대신한다.
- `observation_freeze`: 관측 동결 시나리오의 실측 3인칭 카메라 | 고스트 렌더 나란히 영상(사고 띠 포함).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import media as mediacmd
from .artifacts import sha256_file, write_json
from .profile import Profile, current_user, display_path
from .scenario_bank import STATUS_RULE_TASKS, is_public, list_scenarios

REPORT_SCHEMA = "robot-ops-report/1"
ASSETS_DIR = Path(__file__).resolve().parent / "report_assets"
ASSET_FILES = ("index.html", "app.js", "style.css")
HERO_SOURCES = ("auto", "cup_grid", "observation_freeze")
GRID_TASKS = frozenset({"cup_contact", "drive_kinematic"})
FPS = 10
CLIP_BUDGET = 3 * 1024 * 1024
HERO_BUDGET = 6 * 1024 * 1024
SITE_BUDGET = 60 * 1024 * 1024
HERO_FREEZE_LENGTH_S = 18.0

# 상태 표기. 공개 빌드에 나오지 않는 상태 이름이 화면 자산(app.js)에 남지 않도록 데이터에 싣는다.
STATUS_KO = {
    "active": "활성",
    "stale": "과거 기록(현재 코드에서 사라짐)",
    "after_fix_regression": "수정 후 회귀",
    "not_reproducible_in_model": "모델에서 미재현",
}

# 궤적 재생 대상이 아닌 사고(전기·펌웨어 계층이거나 궤적 기록이 없음).
OUT_OF_SCOPE = [
    {"title_ko": "서보 발연", "reason_ko": "전기·펌웨어"},
    {"title_ko": "서보 온도 레지스터 래치", "reason_ko": "전기·펌웨어"},
    {"title_ko": "과부하 오발·버스 급사", "reason_ko": "전기·펌웨어"},
    {"title_ko": "Astra 카메라 USB 교착", "reason_ko": "전기·펌웨어"},
    {"title_ko": "OLED 불량으로 I2C 버스 사망", "reason_ko": "전기·펌웨어"},
    {"title_ko": "그리퍼 죠 바닥 충돌", "reason_ko": "궤적 기록 없음"},
    {"title_ko": "토크 해제 뒤 팔 추락", "reason_ko": "궤적 기록 없음"},
    {"title_ko": "wrist_flex 간헐 무응답", "reason_ko": "궤적 기록 없음"},
]


# ---------------------------------------------------------------- 경로·JSON 도우미


def _private_prefixes() -> list[str]:
    return [str(Path.home()), f"/data/{current_user()}"]


def scrub(value: Any) -> Any:
    """문자열 어디에 있든 홈·/data/<user>를 `~`·`/data/$USER`로 바꾼다(재귀)."""
    if isinstance(value, str):
        home, data_user = _private_prefixes()
        return value.replace(home, "~").replace(data_user, "/data/$USER")
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _resolve(path: str | None) -> Path | None:
    if not path:
        return None
    text = str(path).replace("${USER}", current_user()).replace("$USER", current_user())
    return Path(text).expanduser()


def _card(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": doc["scenario_id"],
        "title_ko": doc["title_ko"],
        "origin": doc["origin"],
        "judgement_basis": doc["judgement_basis"],
        "assembly": doc["assembly"],
        "task": doc["task"],
        "status": doc["status"],
        "status_ko": STATUS_KO.get(doc["status"], doc["status"]),
        "model_note": doc.get("model_note"),
        "replayable": doc["replayable"],
        "incident": doc.get("incident"),
        "failure_record_id": doc.get("failure_record_id"),
        "conditions": {
            name: {"label_ko": c.get("label_ko"), "judgement_basis": c.get("judgement_basis")}
            for name, c in doc["conditions"].items()
        },
        "sources": [
            {"path": display_path(s["path"]), "sha256": s["sha256"], "role": s["role"]}
            for s in doc.get("sources", [])
        ],
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _regress_rank(doc: dict[str, Any]) -> tuple[int, int, str]:
    """완전한 격자(조건 수·칸 수가 많은 것)를 우선하고, 같으면 최신(created_at)."""
    conditions = doc.get("conditions") if isinstance(doc.get("conditions"), dict) else {}
    cells = sum(len(c.get("cells") or []) for c in conditions.values() if isinstance(c, dict))
    return (len(conditions), cells, str(doc.get("created_at", "")))


def _iter_regress_docs(artifacts_root: Path):
    runs = artifacts_root / "runs"
    if not runs.is_dir():
        return
    for path in sorted(runs.glob("*/regress_result.json")):
        doc = _load_json(path)
        if doc is not None and isinstance(doc.get("scenario_id"), str):
            yield doc


def latest_regress_results(artifacts_root: Path) -> dict[str, dict[str, Any]]:
    """시나리오별 대표 regress_result.json.

    녹화 전용 실행(`mode="record"`)은 제외한다. 격자 실행 도중의 부분 결과(첫 1회 실측 등)가
    완전한 격자를 덮지 않도록 (조건 수, 칸 수, created_at) 순으로 고른다.
    """
    latest: dict[str, dict[str, Any]] = {}
    for doc in _iter_regress_docs(artifacts_root):
        if doc.get("mode") == "record":
            continue
        sid = doc["scenario_id"]
        current = latest.get(sid)
        if current is None or _regress_rank(doc) >= _regress_rank(current):
            latest[sid] = doc
    return latest


def record_results(artifacts_root: Path) -> dict[str, dict[tuple[str, float, float], dict[str, Any]]]:
    """녹화 전용 실행(`mode="record"`)의 격자점별 최신 칸: {scenario_id: {(조건, x, y): cell}}."""
    found: dict[str, dict[tuple[str, float, float], tuple[str, dict[str, Any]]]] = {}
    for doc in _iter_regress_docs(artifacts_root):
        if doc.get("mode") != "record":
            continue
        created = str(doc.get("created_at", ""))
        for name, cond in (doc.get("conditions") or {}).items():
            for cell in (cond or {}).get("cells") or []:
                key = (name, *_cell_key(cell))
                bucket = found.setdefault(doc["scenario_id"], {})
                if key not in bucket or created >= bucket[key][0]:
                    bucket[key] = (created, cell)
    return {sid: {k: v[1] for k, v in bucket.items()} for sid, bucket in found.items()}


def is_complete_grid(scenario: dict[str, Any], regress: dict[str, Any]) -> bool:
    xs, ys = _grid_axes(scenario)
    need = len(xs) * len(ys)
    conditions = regress.get("conditions") or {}
    return bool(need) and all(
        len((conditions.get(name) or {}).get("cells") or []) >= need for name in scenario.get("conditions", {})
    )


def latest_replay_results(artifacts_root: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """(데이터셋 표시 경로, 에피소드)별 가장 최근 replay_result.json (+ `_run_dir`)."""
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    runs = artifacts_root / "runs"
    if not runs.is_dir():
        return latest
    for path in sorted(runs.glob("*/replay_result.json")):
        doc = _load_json(path)
        if doc is None or not isinstance(doc.get("source"), dict) or not isinstance(doc.get("divergence"), dict):
            continue
        key = (display_path(str(doc["source"].get("path", ""))), int(doc["source"].get("episode", -1)))
        doc["_run_dir"] = path.parent
        current = latest.get(key)
        if current is None or str(doc.get("created_at", "")) >= str(current.get("created_at", "")):
            latest[key] = doc
    return latest


def _reset_data_dir(data_dir: Path) -> None:
    if data_dir.is_symlink():
        raise RuntimeError(f"리포트 data 폴더가 심링크라 비우지 않습니다: {data_dir}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)


def _sha256(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


# ---------------------------------------------------------------- 미디어 굽기(캐시)


class MediaBuilder:
    """`media/`에 파일을 만들고, 입력·명령이 같으면 다시 인코딩하지 않는다.

    크기 예산을 넘으면 crf를 올려 최대 두 번 다시 인코딩하고, 그래도 넘으면 경고를 남긴다.
    """

    def __init__(self, out_dir: Path, *, enabled: bool, runner: Callable[..., Any] | None = None) -> None:
        self.dir = out_dir / "media"
        self.enabled = enabled
        self.runner = runner
        self.used: set[str] = set()
        self.warnings: list[str] = []
        self.cache_path = self.dir / ".cache.json"
        self.cache: dict[str, str] = {}
        if enabled:
            if self.dir.is_symlink():
                raise RuntimeError(f"리포트 media 폴더가 심링크라 쓰지 않습니다: {self.dir}")
            self.dir.mkdir(parents=True, exist_ok=True)
            self.cache = (_load_json(self.cache_path) or {}) if self.cache_path.exists() else {}
        self.font = mediacmd.find_cjk_font() if enabled else None
        self.bold = mediacmd.find_cjk_font(bold=True) if enabled else None
        if enabled and not self.font:
            self.warnings.append("CJK 글꼴(fc-match 'Noto Sans CJK KR')을 찾지 못해 라벨 없는 미디어를 만듭니다")

    def _run(self, argv: list[str]) -> None:
        if self.runner is None:
            mediacmd.run_media_command(argv)
        else:
            mediacmd.run_media_command(argv, runner=self.runner)

    def make(
        self,
        name: str,
        command: Callable[[Path, int], list[str]],
        inputs: list[Path],
        *,
        budget: int | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        out = self.dir / name
        stats = []
        for src in inputs:
            try:
                st = src.stat()
            except OSError:
                self.warnings.append(f"미디어 입력 없음: {display_path(src)}")
                return None
            stats.append([str(src), st.st_size, int(st.st_mtime)])
        key = hashlib.sha256(
            json.dumps({"argv": command(Path("OUT"), 23), "inputs": stats}, ensure_ascii=False).encode()
        ).hexdigest()
        if self.cache.get(name) != key or not out.exists():
            crf = 23
            for attempt in range(3):
                self._run(command(out, crf))
                if budget is None or out.stat().st_size <= budget or attempt == 2:
                    break
                crf += 3
            if budget is not None and out.stat().st_size > budget:
                self.warnings.append(f"{name} 크기 {out.stat().st_size} B가 예산 {budget} B를 넘습니다")
            self.cache[name] = key
        self.used.add(name)
        return f"media/{name}"

    def finish(self) -> None:
        if not self.enabled:
            return
        for path in sorted(self.dir.iterdir()):
            if path.name == ".cache.json":
                continue
            if path.name not in self.used and path.is_file() and not path.is_symlink():
                path.unlink()
        self.cache = {k: v for k, v in self.cache.items() if k in self.used}
        self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- 관측 동결(lerobot_episode) 상세


def _primary_incident(divergence: dict[str, Any]) -> dict[str, Any] | None:
    freezes = [i for i in divergence.get("incidents", []) if i.get("kind") == "freeze"]
    if freezes:
        return max(freezes, key=lambda i: (i.get("n_frames", 0), -float(i.get("t_start_s", 0.0))))
    incidents = divergence.get("incidents") or []
    return incidents[0] if incidents else None


def _window_max(series: dict[str, Any], a: float, b: float) -> float | None:
    values = [
        max(abs(v) for v in row)
        for t, row in zip(series.get("t", []), series.get("joint_err_deg", []))
        if a - 1e-6 <= float(t) <= b + 1e-6 and row
    ]
    return max(values) if values else None


def _round_series(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "t": [round(float(t), 3) for t in series.get("t", [])],
        "joint_err_deg": [[round(float(v), 2) for v in row] for row in series.get("joint_err_deg", [])],
        "tcp_mm": [None if v is None else round(float(v), 2) for v in series.get("tcp_mm", [])],
    }


def _replay_view(
    profile: Profile,
    scenario: dict[str, Any],
    cond_name: str,
    cond: dict[str, Any],
    replay: dict[str, Any],
    media: MediaBuilder,
) -> dict[str, Any]:
    sid = scenario["scenario_id"]
    div = replay["divergence"]
    run_dir: Path = replay["_run_dir"]
    incident = _primary_incident(div) or {}
    a = float(incident.get("t_start_s", 0.0))
    b = float(incident.get("t_end_s", a))
    convert_video = replay.get("convert_video") or {}
    real_src = _resolve(convert_video.get("path"))
    ghost_src = _resolve(replay.get("video_path"))
    mujoco = _load_json(run_dir / "mujoco_result.json") or {}
    stem = f"{sid}--{cond_name}"
    real_label = convert_video.get("label_ko") or "실측 카메라"
    upscaled = int(convert_video.get("height") or 480) < mediacmd.DEFAULT_HEIGHT
    view: dict[str, Any] = {
        "kind": "replay",
        "condition": cond_name,
        "label_ko": cond.get("label_ko"),
        "run_id": replay.get("run_id"),
        "fps": FPS,
        "duration_s": round(float(div["series"]["t"][-1]) + 1.0 / FPS, 3) if div.get("series", {}).get("t") else None,
        "joint_names": div.get("joint_names", []),
        "series": _round_series(div.get("series", {})),
        "freezes": [
            {"t_start_s": i["t_start_s"], "t_end_s": i["t_end_s"], "n_frames": i.get("n_frames")}
            for i in div.get("incidents", [])
            if i.get("kind") == "freeze"
        ],
        "incident": {"t_start_s": a, "t_end_s": b, "kind": incident.get("kind"), "n_frames": incident.get("n_frames")},
        "thresholds": div.get("thresholds"),
        "calibration_offset_deg": div.get("calibration_offset_deg"),
        "metrics": {
            "max_tcp_mm": (div.get("tcp") or {}).get("max_mm"),
            "tcp_calibration_suspect": bool((div.get("tcp") or {}).get("calibration_suspect")),
            "incident_joint_err_deg": _window_max(div.get("series", {}), a, b),
            "incident_t_s": a,
        },
        "real": {"label_ko": real_label, "camera": convert_video.get("camera"), "upscaled": upscaled},
        "ghost": {"label_ko": "명령(반투명)·관측", "available": bool(ghost_src and ghost_src.exists())},
    }
    poster_at = round((a + b) / 2, 2)
    if real_src and real_src.exists():
        view["real"]["src"] = media.make(
            f"{stem}--real.mp4",
            lambda out, crf: mediacmd.clip_command(real_src, out, fps=FPS, crf=crf),
            [real_src],
            budget=CLIP_BUDGET,
        )
        view["real"]["poster"] = media.make(
            f"{stem}--real.webp", lambda out, crf: mediacmd.poster_command(real_src, out, at_s=poster_at), [real_src]
        )
    if ghost_src and ghost_src.exists():
        view["ghost"]["src"] = media.make(
            f"{stem}--ghost.mp4",
            lambda out, crf: mediacmd.clip_command(ghost_src, out, fps=FPS, crf=crf),
            [ghost_src],
            budget=CLIP_BUDGET,
        )
        view["ghost"]["poster"] = media.make(
            f"{stem}--ghost.webp", lambda out, crf: mediacmd.poster_command(ghost_src, out, at_s=poster_at), [ghost_src]
        )
    traj = _resolve(replay.get("trajectory_path"))
    ghost_meta = mujoco.get("ghost") or {}
    source = replay.get("source") or {}
    profile_arg = display_path(profile.path)
    try:
        profile_arg = str(profile.path.resolve().relative_to(profile.base_dir.resolve()))
    except ValueError:
        pass
    view["provenance"] = scrub(
        {
            "run_dir": display_path(run_dir),
            "items": [
                {"label_ko": "기록(parquet)", "path": source.get("path"), "sha256": source.get("sha256")},
                {
                    "label_ko": real_label + " 원본",
                    "path": (convert_video.get("source") or {}).get("path"),
                    "sha256": (convert_video.get("source") or {}).get("sha256"),
                },
                {
                    "label_ko": "궤적(trajectory/1)",
                    "path": display_path(traj) if traj else None,
                    "sha256": _sha256(traj) if traj else None,
                },
                {
                    "label_ko": "고스트 렌더",
                    "path": display_path(ghost_src) if ghost_src else None,
                    "sha256": ghost_meta.get("video_sha256"),
                },
            ],
            "tool_sha256": mujoco.get("tool_sha256") or {},
            "reproduce": [
                f"robot-ops replay --profile {profile_arg} --source lerobot:{source.get('path')} "
                f"--episode {source.get('episode')} --ghost",
                f"robot-ops report build --profile {profile_arg}",
            ],
            "ghost_alpha": ghost_meta.get("alpha"),
            "physics_steps": mujoco.get("physics_steps"),
        }
    )
    view["fidelity"] = _fidelity(profile, cond_name, mujoco)
    view["_real_path"] = real_src
    view["_ghost_path"] = ghost_src
    return view


def _fidelity(profile: Profile, cond_name: str, mujoco: dict[str, Any]) -> dict[str, Any]:
    """고스트 렌더의 장면·카메라 설정과, 있으면 실측 대조 기록(`<report_dir 옆>/fidelity/<조건>/`).

    - camera_fit.json: 실측 3인칭 영상의 손으로 고른 점에 맞춘 카메라(점 수·RMS). 이 조건 폴더에 없으면
      고스트 카메라 설정과 같은 값을 가진 다른 조건의 적합 결과를 쓴다.
    - tracking_lag.json: 명령을 시간 이동해 실측 영상에 겹쳐 본 시각별 지연(s).
    """
    ghost = mujoco.get("ghost") or {}
    scene = mujoco.get("scene") or {}
    out: dict[str, Any] = {
        "scene_layout": scene.get("layout"),
        "removed_bodies": scene.get("removed_bodies") or [],
        "camera_fit": None,
        "lag": None,
    }
    root = profile.report_dir.parent / "fidelity"
    setup = ghost.get("camera_setup_panel") or {}
    fits = [root / cond_name / "camera_fit.json"] + sorted(root.glob("*/camera_fit.json"))
    for path in fits:
        doc = _load_json(path) if path.exists() else None
        result = (doc or {}).get("result") or {}
        same = path.parent.name == cond_name or (
            setup and all(
                isinstance(result.get(k), (int, float)) and abs(float(result[k]) - float(setup.get(k, 1e9))) < 0.2
                for k in ("distance", "azimuth", "elevation")
            )
        )
        if doc and same and isinstance(doc.get("keypoints"), list):
            out["camera_fit"] = {"keypoints": len(doc["keypoints"]), "rms_px": result.get("rms_px"),
                                 "fit_condition": path.parent.name}
            break
    lag = _load_json(root / cond_name / "tracking_lag.json") if (root / cond_name / "tracking_lag.json").exists() else None
    by_t = (lag or {}).get("lag_s_by_t") or {}
    if by_t:
        items = sorted((float(t), float(v)) for t, v in by_t.items())
        lagged = [(t, v) for t, v in items if v > 0]
        out["lag"] = {
            "aligned_t_s": [t for t, v in items if v == 0],
            "lagged_t_s": [t for t, _ in lagged],
            "lag_range_s": [min(v for _, v in lagged), max(v for _, v in lagged)] if lagged else None,
            "method_ko": lag.get("method_ko"),
        }
    return out


# ---------------------------------------------------------------- 격자(regress) 데이터·통과 지도

PAD_KO = {"fixed": "고정 죠 패드", "moving": "이동 죠 패드"}
FAILURE_KO = {
    "premature_cup_contact": "조기 접촉",
    "cup_displaced_before_close": "닫기 전 컵 이동",
    "not_arrived_within_horizon": "시간 내 미도착",
    "reversal_rate_exceeded": "반전율 초과",
}


def _mm(value: float) -> str:
    return f"{float(value):g}".replace("-", "−")


def _short_label(label: str) -> str:
    """'복구 켬(정답 좌표 조건)' → '복구 켬'."""
    return label.split("(")[0].strip() or label


def failed_lines(cond: dict[str, Any], xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """모든 칸이 실패인 행(y 값)과 열(x 값)."""
    fails = {_cell_key(c) for c in cond.get("cells") or [] if _cell_state(c) == "fail"}
    rows = [y for y in sorted(ys) if len(xs) > 1 and all((x, y) in fails for x in xs)]
    cols = [x for x in sorted(xs) if len(ys) > 1 and all((x, y) in fails for y in ys)]
    return rows, cols


def grid_patterns(cond: dict[str, Any], xs: list[float], ys: list[float]) -> list[str]:
    """통과 지도에서 행·열 단위로 전부 실패한 패턴을 데이터에서 찾아 문장으로 만든다."""
    cells = {_cell_key(c): c for c in cond.get("cells") or []}
    fails = {k for k, c in cells.items() if _cell_state(c) == "fail"}
    if not cells:
        return []
    if not fails:
        return ["실패 없음"]
    out: list[str] = []
    covered: set[tuple[float, float]] = set()

    def phrase(keys: list[tuple[float, float]]) -> str:
        codes = {cells[k].get("failure_code") for k in keys}
        return FAILURE_KO.get(next(iter(codes)), "실패") if len(codes) == 1 else "실패"

    for y in sorted(ys):
        row = [(x, y) for x in xs]
        if len(row) > 1 and all(k in fails for k in row):
            out.append(f"y {_mm(y)} mm 행 {len(row)}점 전부 {phrase(row)}")
            covered.update(row)
    for x in sorted(xs):
        col = [(x, y) for y in ys]
        if len(col) > 1 and all(k in fails for k in col):
            out.append(f"x {_mm(x)} mm 열 {len(col)}점 전부 {phrase(col)}")
            covered.update(col)
    rest = sorted(fails - covered)
    if rest:
        out.append(f"{'그 밖 ' if out else ''}실패 {len(rest)}점({phrase(rest)})")
    return out


def _cell_key(cell: dict[str, Any]) -> tuple[float, float]:
    return (float(cell.get("x_mm", 0)), float(cell.get("y_mm", 0)))


def _cell_state(cell: dict[str, Any] | None) -> str | None:
    if cell is None:
        return None
    if cell.get("status") not in (None, "ok"):
        return "infra"
    verdict = cell.get("verdict")
    return verdict if verdict in ("pass", "fail") else "infra"


def _cell_recording(artifacts_root: Path, run_id: str | None) -> tuple[Path | None, float | None]:
    """격자점 실행 폴더의 녹화 MP4와 판정 시각.

    규약: run.response.json `video_path`, 없으면 폴더의 *.mp4. 시각은 응답의 `incident_t_s`,
    없으면 `sim_time_s`(컵 과제는 실패 판정 시각에 시뮬이 끝난다). 둘 다 없으면 None.
    """
    if not run_id:
        return None, None
    run_dir = artifacts_root / "runs" / run_id
    response = _load_json(run_dir / "run.response.json") or {}
    incident_t = response.get("incident_t_s", response.get("sim_time_s"))
    video = _resolve(response.get("video_path"))
    if video is None or not video.exists():
        found = sorted(run_dir.glob("*.mp4")) + sorted(run_dir.glob("*/*.mp4"))
        video = found[0] if found else None
    return video, (float(incident_t) if isinstance(incident_t, (int, float)) else None)


def _check_margin(cell: dict[str, Any]) -> float:
    margins = []
    for check in cell.get("checks") or []:
        value, threshold = check.get("value"), check.get("threshold")
        if isinstance(value, (int, float)) and isinstance(threshold, (int, float)) and threshold:
            margins.append(abs(float(value) - float(threshold)) / abs(float(threshold)))
    return min(margins) if margins else float("inf")


def _grid_axes(scenario: dict[str, Any]) -> tuple[list[float], list[float]]:
    grid = scenario.get("grid") or {}
    return (
        [float(v) for v in grid.get("spawn_x_offset_mm", [])],
        [float(v) for v in grid.get("spawn_y_offset_mm", [])],
    )


def _grid_summary(scenario: dict[str, Any]) -> dict[str, Any]:
    xs, ys = _grid_axes(scenario)
    return {"points": len(xs) * len(ys), "range_mm": max([abs(v) for v in xs + ys] or [0])}


# ---------------------------------------------------------------- 컵 접촉 증거(샘플 시계열·접촉 기하)
#
# 팀 규칙(bimanual-robot tools/cup_contact_model.py `preclose_failure`, 고정 커밋 8a09a02): 아래 단계(닫기 전)에서
# 패드 접촉력 최대값이 설정 `minimum_contact_force_n` 이상이면 premature_cup_contact, 컵 수평 이동이
# `maximum_preclose_displacement_m`를 넘으면 cup_displaced_before_close. 임계값은 결과 폴더의 plan.json에서 읽는다.
PRECLOSE_PHASES = frozenset({"RESET", "REORIENT_ABOVE", "PREGRASP_ABOVE", "ALIGN_MIDDLE", "APPROACH"})
# 샘플 `contact_force_n`의 순서 = cup_contact_model.FINGER_LINKS(고정 죠 링크, 이동 죠 링크).
PAD_KEYS = ("fixed", "moving")
URDF_PAD_NAMES = {"fixed": "assumed_fixed_pad", "moving": "assumed_moving_pad"}
EVIDENCE_BUCKET_S = 0.05
# 패드 아래면이 컵 윗면보다 이만큼 위까지는 '같은 높이'로 본다(시뮬 contact offset 1 mm 안쪽).
RIM_TOLERANCE_M = 0.0005


def _mat(r: list[list[float]], t: list[float]) -> list[list[float]]:
    return [[*r[0], t[0]], [*r[1], t[1]], [*r[2], t[2]], [0.0, 0.0, 0.0, 1.0]]


def _mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inv(m: list[list[float]]) -> list[list[float]]:
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    return _mat(r, [-sum(r[i][k] * m[k][3] for k in range(3)) for i in range(3)])


def _apply(m: list[list[float]], p: list[float]) -> list[float]:
    return [sum(m[i][k] * p[k] for k in range(3)) + m[i][3] for i in range(3)]


def _rpy(roll: float, pitch: float, yaw: float) -> list[list[float]]:
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch),
                              math.cos(yaw), math.sin(yaw))
    return [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr]]


def _axis_angle(axis: list[float], q: float) -> list[list[float]]:
    n = math.sqrt(sum(v * v for v in axis))
    x, y, z = (v / n for v in axis)
    c, s, k = math.cos(q), math.sin(q), 1 - math.cos(q)
    return [[c + x * x * k, x * y * k - z * s, x * z * k + y * s],
            [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
            [z * x * k - y * s, z * y * k + x * s, c + z * z * k]]


def _floats(text: str | None, n: int = 3) -> list[float]:
    values = [float(v) for v in (text or "").split()]
    return values if len(values) == n else [0.0] * n


def _urdf_parts(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """URDF의 가정 패드 상자(링크·중심·크기)와 관절(부모·자식·원점·축)."""
    root = ET.parse(path).getroot()
    pads: dict[str, dict[str, Any]] = {}
    for link in root.findall("link"):
        for col in link.findall("collision"):
            box = col.find("geometry/box")
            if col.get("name") in URDF_PAD_NAMES.values() and box is not None:
                origin = col.find("origin")
                pads[col.get("name")] = {
                    "link": link.get("name"),
                    "center": _floats(origin.get("xyz") if origin is not None else None),
                    "size": _floats(box.get("size")),
                }
    joints: dict[str, dict[str, Any]] = {}
    for joint in root.findall("joint"):
        origin, axis = joint.find("origin"), joint.find("axis")
        parent, child = joint.find("parent"), joint.find("child")
        if parent is None or child is None:
            continue
        joints[joint.get("name")] = {
            "parent": parent.get("link"),
            "child": child.get("link"),
            "xyz": _floats(origin.get("xyz") if origin is not None else None),
            "rpy": _floats(origin.get("rpy") if origin is not None else None),
            "axis": _floats(axis.get("xyz")) if axis is not None else [0.0, 0.0, 1.0],
            "type": joint.get("type"),
        }
    return pads, joints


def _chain(joints: dict[str, dict[str, Any]], frm: str, to: str) -> list[dict[str, Any]]:
    by_child = {j["child"]: j for j in joints.values()}
    path, cur = [], to
    while cur != frm:
        joint = by_child.get(cur)
        if joint is None:
            raise ValueError(f"URDF에서 {frm} → {to} 경로를 찾지 못했습니다")
        path.append(joint)
        cur = joint["parent"]
    return list(reversed(path))


def _chain_tf(chain: list[dict[str, Any]], q: float = 0.0) -> list[list[float]]:
    out = _mat([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0])
    for joint in chain:
        out = _mul(out, _mat(_rpy(*joint["rpy"]), joint["xyz"]))
        if joint["type"] in ("revolute", "continuous") and q:
            out = _mul(out, _mat(_axis_angle(joint["axis"], q), [0.0, 0.0, 0.0]))
    return out


def robot_axes(joints: dict[str, dict[str, Any]]) -> dict[str, str] | None:
    """월드 x·y가 로봇 기준 어느 쪽인지 URDF로 정한다.

    시뮬은 로봇을 장면 원점에 회전 없이 놓고 중력은 −z다(월드 = 로봇 base 좌표, z 위).
    base_link에 고정된 `left_*` 부품(왼팔 장착부 등)이 모두 y > 0, `right_*` 부품이 모두 y < 0이면
    +y = 로봇 왼쪽이고, 오른손 좌표(x = y × z)에서 +x = 로봇 앞이다. 근거가 맞지 않으면 None.
    """
    base = [j for j in joints.values() if j["parent"] == "base_link" and j["type"] == "fixed"]
    left = [j["xyz"][1] for j in base if j["child"].startswith("left_")]
    right = [j["xyz"][1] for j in base if j["child"].startswith("right_")]
    if left and right and min(left) > 0 > max(right):
        return {"x_plus_ko": "앞", "x_minus_ko": "뒤", "y_plus_ko": "왼쪽", "y_minus_ko": "오른쪽"}
    return None


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted({(round(p[0], 6), round(p[1], 6)) for p in points})
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    upper: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _poly_distance(p: tuple[float, float], poly: list[tuple[float, float]]) -> tuple[float, tuple[float, float]]:
    """점에서 볼록 다각형 경계까지 거리(안쪽이면 음수)와 가장 가까운 경계점."""
    best, nearest, inside = float("inf"), p, True
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
        q = (a[0] + t * dx, a[1] + t * dy)
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if d < best:
            best, nearest = d, q
        if dx * (p[1] - a[1]) - dy * (p[0] - a[0]) < 0:
            inside = False
    return (-best if inside else best), nearest


def _tool_rotation(measurement: dict[str, Any]) -> list[list[float]] | None:
    """계획 자세의 접근축·닫힘축으로 도구 좌표 회전(왼팔: 접근 = tool z, 닫힘 = tool x;
    팀 plan_body_side_grasp.grasp_axes 규약)."""
    z, x = measurement.get("approach_axis"), measurement.get("closing_axis")
    if not (isinstance(z, list) and isinstance(x, list) and len(z) == 3 and len(x) == 3):
        return None
    nz = math.sqrt(sum(v * v for v in z))
    z = [v / nz for v in z]
    d = sum(a * b for a, b in zip(x, z))
    x = [a - d * b for a, b in zip(x, z)]
    nx = math.sqrt(sum(v * v for v in x))
    x = [v / nx for v in x]
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    return [[x[0], y[0], z[0]], [x[1], y[1], z[1]], [x[2], y[2], z[2]]]


def _mm_list(values: list[float], digits: int = 2) -> list[float]:
    return [round(float(v) * 1000.0, digits) for v in values]


class _PadModel:
    """샘플(접촉 중심 위치·그리퍼 각)과 계획 축으로 두 패드 상자의 월드 꼭짓점을 계산한다."""

    def __init__(self, plan: dict[str, Any], urdf: Path) -> None:
        if plan.get("active_side") != "left":
            raise ValueError("접촉 기하는 왼팔 규약만 계산합니다")
        pads, joints = _urdf_parts(urdf)
        self.fixed = pads[URDF_PAD_NAMES["fixed"]]
        self.moving = pads[URDF_PAD_NAMES["moving"]]
        tcp = (plan.get("plan") or {}).get("tcp_frame")
        if not tcp:
            raise ValueError("plan.json에 tcp_frame이 없습니다")
        self.tcp_from_link = _inv(_chain_tf(_chain(joints, self.fixed["link"], tcp)))
        self.jaw_chain = _chain(joints, self.fixed["link"], self.moving["link"])
        self.axes = robot_axes(joints)

    def corners(
        self, rotation: list[list[float]], tcp_m: list[float], gripper_rad: float
    ) -> dict[str, list[list[float]]]:
        world_link = _mul(_mat(rotation, tcp_m), self.tcp_from_link)
        world_jaw = _mul(world_link, _chain_tf(self.jaw_chain, gripper_rad))
        out = {}
        for key, tf, pad in (("fixed", world_link, self.fixed), ("moving", world_jaw, self.moving)):
            c, s = pad["center"], pad["size"]
            out[key] = [
                _apply(tf, [c[0] + sx * s[0] / 2, c[1] + sy * s[1] / 2, c[2] + sz * s[2] / 2])
                for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)
            ]
        return out


def _pose_axes(result: dict[str, Any], plan: dict[str, Any]) -> dict[tuple[int, str], list[list[float]]]:
    """(시도, 단계) → 그 단계 목표 자세의 도구 회전. 시도 0은 plan.json, 재계획 시도는 결과 events의 plan."""
    out: dict[tuple[int, str], list[list[float]]] = {}
    sources = [(0, (plan.get("plan") or {}).get("poses") or [])]
    sources += [
        (int(ev.get("attempt", 0)), ((ev.get("plan") or {}).get("poses") or []))
        for ev in result.get("events") or []
        if ev.get("type") == "replanned"
    ]
    for attempt, poses in sources:
        for pose in poses:
            rot = _tool_rotation(pose.get("measurement") or {})
            if rot is not None:
                out[(attempt, pose.get("name"))] = rot
    return out


def _attempt_targets(result: dict[str, Any], plan: dict[str, Any]) -> dict[int, list[float]]:
    """시도별 APPROACH 목표(접촉 중심을 둘 컵 위치, m)."""
    out: dict[int, list[float]] = {}
    sources = [(0, (plan.get("plan") or {}).get("poses") or [])]
    sources += [
        (int(ev.get("attempt", 0)), ((ev.get("plan") or {}).get("poses") or []))
        for ev in result.get("events") or []
        if ev.get("type") == "replanned"
    ]
    for attempt, poses in sources:
        for pose in poses:
            if pose.get("name") == "APPROACH" and isinstance(pose.get("target_m"), list):
                out[attempt] = pose["target_m"]
    return out


def _snapshot(
    model: _PadModel, sample: dict[str, Any], rotation: list[list[float]], cfg: dict[str, Any], *, kind: str,
    target: list[float] | None, contact_pad: str | None,
) -> dict[str, Any]:
    corners = model.corners(rotation, sample["contact_center_m"], float(sample.get("gripper_actual_rad") or 0.0))
    cup = sample["cup_position_m"]
    r, h = float(cfg["cup_radius_m"]), float(cfg["cup_height_m"])
    top = cup[2] + h / 2
    pads = []
    contact_xy = None
    nearest_xy: dict[str, tuple[float, float]] = {}
    for key in PAD_KEYS:
        pts = corners[key]
        poly = _hull([(p[0], p[1]) for p in pts])
        dist, near = _poly_distance((cup[0], cup[1]), poly)
        nearest_xy[key] = near
        pads.append({
            "key": key,
            "top_mm": [[round(x * 1000, 2), round(y * 1000, 2)] for x, y in poly],
            "front_mm": [[round(y * 1000, 2), round(z * 1000, 2)] for y, z in _hull([(p[1], p[2]) for p in pts])],
            "gap_mm": round((dist - r) * 1000, 2),
            "bottom_above_rim_mm": round((min(p[2] for p in pts) - top) * 1000, 2),
        })
        if key == contact_pad and dist < r:
            d = math.hypot(near[0] - cup[0], near[1] - cup[1]) or 1.0
            edge = (cup[0] + (near[0] - cup[0]) / d * r, cup[1] + (near[1] - cup[1]) / d * r)
            contact_xy = ((near[0] + edge[0]) / 2, (near[1] + edge[1]) / 2)
    focus_key = contact_pad or min(pads, key=lambda p: p["gap_mm"])["key"]
    focus = contact_xy or nearest_xy[focus_key]
    # 앞에서 본 단면: 접촉(또는 가장 가까운) 점을 지나는 x 평면에서 컵 현(chord)의 y 범위
    half = math.sqrt(max(0.0, r * r - (focus[0] - cup[0]) ** 2))
    return {
        "kind": kind,
        "t_s": round(float(sample["time_s"]), 3),
        "phase": sample.get("phase"),
        "attempt": int(sample.get("attempt", 0)),
        "cup_mm": _mm_list(cup),
        "tcp_mm": _mm_list(sample["contact_center_m"]),
        "target_mm": _mm_list(target) if target else None,
        "pads": pads,
        "contact_mm": [round(contact_xy[0] * 1000, 2), round(contact_xy[1] * 1000, 2)] if contact_xy else None,
        "contact_pad": contact_pad if contact_xy else None,
        "section": {
            "x_mm": round(focus[0] * 1000, 2),
            "focus_y_mm": round(focus[1] * 1000, 2),
            "cup_y_mm": [round((cup[1] - half) * 1000, 2), round((cup[1] + half) * 1000, 2)],
            "cup_z_mm": [round((cup[2] - h / 2) * 1000, 2), round(top * 1000, 2)],
        },
    }


def _cup_geometry(result: dict[str, Any], plan: dict[str, Any], urdf: Path) -> dict[str, Any]:
    cfg = plan["config"]
    model = _PadModel(plan, urdf)
    axes = _pose_axes(result, plan)
    targets = _attempt_targets(result, plan)
    samples = result["samples"]
    top_rel = float(cfg["cup_height_m"]) / 2
    r = float(cfg["cup_radius_m"])
    fail_index = {int(ev["attempt"]): int(ev["sample_index"]) for ev in result.get("events") or []
                  if ev.get("type") == "preclose_failure" and isinstance(ev.get("sample_index"), int)}
    best: dict[int, tuple[float, int, str]] = {}
    for i, s in enumerate(samples):
        if s.get("phase") not in PRECLOSE_PHASES:
            continue
        attempt = int(s.get("attempt", 0))
        rot = axes.get((attempt, s["phase"]))
        if rot is None:
            continue
        corners = model.corners(rot, s["contact_center_m"], float(s.get("gripper_actual_rad") or 0.0))
        cup = s["cup_position_m"]
        for key in PAD_KEYS:
            pts = corners[key]
            if min(p[2] for p in pts) > cup[2] + top_rel + RIM_TOLERANCE_M:
                continue  # 패드가 아직 컵 윗면보다 위 — 위에서 겹쳐 보여도 닿을 수 없다
            dist, _ = _poly_distance((cup[0], cup[1]), _hull([(p[0], p[1]) for p in pts]))
            if attempt not in best or dist - r < best[attempt][0]:
                best[attempt] = (dist - r, i, key)
    snapshots = []
    min_gap = []
    for attempt in sorted(set(best) | set(fail_index)):
        if attempt in fail_index:
            i, kind = fail_index[attempt], "verdict"
            force = samples[i].get("contact_force_n") or [0.0, 0.0]
            pad = PAD_KEYS[max(range(len(force)), key=lambda k: force[k])] if max(force, default=0.0) > 0 else None
        else:
            i, kind, pad = best[attempt][1], "min_gap", None
        s = samples[i]
        rot = axes.get((attempt, s.get("phase")))
        if rot is None:
            continue
        snapshots.append(_snapshot(model, s, rot, cfg, kind=kind, target=targets.get(attempt), contact_pad=pad))
        if attempt in best:
            gap, gi, key = best[attempt]
            min_gap.append({"attempt": attempt, "gap_mm": round(gap * 1000, 2), "pad": key,
                            "t_s": round(float(samples[gi]["time_s"]), 3)})
    offset = cfg.get("cup_spawn_offset_m") or [0.0, 0.0, 0.0]
    return {
        "basis": "sample_tcp_plan_axes_urdf_pads",
        "axes": model.axes,
        "planned_cup_mm": _mm_list(cfg["cup_center_m"]),
        "offset_mm": _mm_list(offset),
        "cup_radius_mm": round(r * 1000, 2),
        "cup_height_mm": round(float(cfg["cup_height_m"]) * 1000, 2),
        "snapshots": snapshots,
        "min_gap": min_gap,
    }


def _series_buckets(samples: list[dict[str, Any]]) -> dict[str, list]:
    """0.05 s 칸마다 최대값(짧은 접촉 봉우리가 사라지지 않게). 시각은 칸의 마지막 샘플 시각."""
    t: list[float] = []
    f0: list[float] = []
    f1: list[float] = []
    disp: list[float] = []
    key = None
    for s in samples:
        bucket = (int(s.get("attempt", 0)), int(float(s["time_s"]) / EVIDENCE_BUCKET_S + 1e-9))
        force = list(s.get("contact_force_n") or [0.0, 0.0]) + [0.0, 0.0]
        d = float(s.get("cup_displacement_from_start_m") or 0.0) * 1000
        if bucket != key:
            key = bucket
            t.append(float(s["time_s"]))
            f0.append(float(force[0]))
            f1.append(float(force[1]))
            disp.append(d)
        else:
            t[-1] = float(s["time_s"])
            f0[-1] = max(f0[-1], float(force[0]))
            f1[-1] = max(f1[-1], float(force[1]))
            disp[-1] = max(disp[-1], d)
    return {
        "t": [round(v, 3) for v in t],
        "force_n": [[round(v, 3) for v in f0], [round(v, 3) for v in f1]],
        "cup_disp_mm": [round(v, 3) for v in disp],
    }


def _phase_spans(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for s in samples:
        name, attempt, t = s.get("phase"), int(s.get("attempt", 0)), round(float(s["time_s"]), 3)
        if spans and spans[-1]["name"] == name and spans[-1]["attempt"] == attempt:
            spans[-1]["t1"] = t
            continue
        if spans:
            spans[-1]["t1"] = t
        spans.append({"name": name, "attempt": attempt, "t0": t, "t1": t, "preclose": name in PRECLOSE_PHASES})
    return spans


def cup_evidence(result_path: Path) -> dict[str, Any] | None:
    """격자점 실행의 Isaac 결과(result.json·plan.json·contact_proxy.urdf)에서 접촉 증거를 만든다."""
    result = _load_json(result_path)
    if result is None or not isinstance(result.get("samples"), list) or not result["samples"]:
        return None
    plan = _load_json(result_path.parent / "plan.json") or {}
    cfg = plan.get("config") or {}
    samples = result["samples"]
    events = [
        {k: v for k, v in {"type": ev.get("type"), "t_s": round(float(ev.get("time_s", 0.0)), 3),
                           "attempt": ev.get("attempt"), "failure_code": ev.get("failure_code")}.items()
         if v is not None}
        for ev in result.get("events") or []
        if isinstance(ev, dict)
    ]
    first = next((ev for ev in result.get("events") or [] if ev.get("type") == "preclose_failure"), None)
    verdict = None
    if first is not None and isinstance(first.get("sample_index"), int) and 0 <= first["sample_index"] < len(samples):
        s = samples[first["sample_index"]]
        force = [round(float(v), 3) for v in s.get("contact_force_n") or []]
        verdict = {
            "t_s": round(float(first.get("time_s", s["time_s"])), 3),
            "failure_code": first.get("failure_code"),
            "phase": s.get("phase"),
            "attempt": int(first.get("attempt", 0)),
            "force_n": force,
            "pad": PAD_KEYS[force.index(max(force))] if force and max(force) > 0 else None,
            "cup_disp_mm": round(float(s.get("cup_displacement_from_start_m") or 0.0) * 1000, 3),
        }
    peaks = []
    for attempt in sorted({int(s.get("attempt", 0)) for s in samples}):
        pre = [s for s in samples if int(s.get("attempt", 0)) == attempt and s.get("phase") in PRECLOSE_PHASES]
        if not pre:
            continue
        top = max(pre, key=lambda s: max(s.get("contact_force_n") or [0.0]))
        force = top.get("contact_force_n") or [0.0]
        peaks.append({
            "attempt": attempt,
            "force_n": round(float(max(force)), 3),
            "pad": PAD_KEYS[force.index(max(force))] if max(force) > 0 else None,
            "t_s": round(float(top["time_s"]), 3),
            "cup_disp_mm": round(max(float(s.get("cup_displacement_from_start_m") or 0.0) for s in pre) * 1000, 3),
        })
    doc: dict[str, Any] = {
        **_series_buckets(samples),
        "duration_s": round(float(samples[-1]["time_s"]), 3),
        "phases": _phase_spans(samples),
        "events": events,
        "threshold_n": cfg.get("minimum_contact_force_n"),
        "preclose_disp_mm": (round(float(cfg["maximum_preclose_displacement_m"]) * 1000, 3)
                             if isinstance(cfg.get("maximum_preclose_displacement_m"), (int, float)) else None),
        "verdict": verdict,
        "preclose_peaks": peaks,
        "outcome": {"task_pass": result.get("task_pass"), "stop_reason": result.get("stop_reason"),
                    "retry_count": result.get("retry_count"), "recovery_enabled": result.get("recovery_enabled")},
        "offset_mm": _mm_list(cfg.get("cup_spawn_offset_m") or [0.0, 0.0, 0.0]),
        "geometry": None,
    }
    urdf = result_path.parent / "contact_proxy.urdf"
    if cfg and urdf.exists():
        try:
            doc["geometry"] = _cup_geometry(result, plan, urdf)
        except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError, ET.ParseError):
            doc["geometry"] = None
    return doc


def _cell_result_path(artifacts_root: Path, run_id: str | None) -> Path | None:
    if not run_id:
        return None
    run_dir = artifacts_root / "runs" / run_id
    response = _load_json(run_dir / "run.response.json") or {}
    path = _resolve(response.get("result_path"))
    if path is None or not path.exists():
        path = run_dir / "isaac" / "result.json"
    return path if path.exists() else None


def _gap_of(evidence: dict[str, Any] | None, attempt: int = 0) -> float | None:
    for item in ((evidence or {}).get("geometry") or {}).get("min_gap") or []:
        if item.get("attempt") == attempt:
            return float(item["gap_mm"])
    return None


def grid_step(values: list[float]) -> float | None:
    diffs = sorted({round(b - a, 6) for a, b in zip(sorted(values), sorted(values)[1:]) if b > a})
    return diffs[0] if diffs else None


def grid_readings(cond: dict[str, Any], xs: list[float], ys: list[float],
                  axes: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """한쪽 끝에서 이어진 행(또는 열)만 전부 실패하고 나머지는 모두 통과일 때,
    그쪽 여유의 범위를 격자 간격으로 추정한다."""
    cells = {_cell_key(c): _cell_state(c) for c in cond.get("cells") or []}
    if len(cells) < len(xs) * len(ys) or any(v not in ("pass", "fail") for v in cells.values()):
        return []
    fails = {k for k, v in cells.items() if v == "fail"}
    out = []
    for axis, lines, others in (("y", sorted(ys), xs), ("x", sorted(xs), ys)):
        def key(line, other, axis=axis):
            return (other, line) if axis == "y" else (line, other)

        bad = [v for v in lines if all(key(v, o) in fails for o in others)]
        covered = {key(v, o) for v in bad for o in others}
        if not bad or covered != fails or len(bad) == len(lines):
            continue
        step = grid_step(lines)
        for edge, sign in ((lines[: len(bad)], -1), (lines[len(lines) - len(bad):], 1)):
            if edge != bad:
                continue
            inner = lines[len(bad)] if sign < 0 else lines[len(lines) - len(bad) - 1]
            outer = bad[-1] if sign < 0 else bad[0]
            if inner * sign < 0 or outer * sign <= 0:
                continue  # 원래 위치(0)에서도 실패하면 여유 범위를 말할 수 없다
            side = (axes or {}).get(f"{axis}_{'plus' if sign > 0 else 'minus'}_ko")
            out.append({
                "axis": axis,
                "fail_lines_mm": bad,
                "pass_line_mm": inner,
                "sign": sign,
                "side_ko": side,
                "bounds_mm": [abs(inner), abs(outer)],
                "step_mm": step,
            })
    return out


def _reading_text(reading: dict[str, Any]) -> str:
    axis, sign = reading["axis"], reading["sign"]
    lines = ", ".join(f"{_mm(v)}" for v in reading["fail_lines_mm"])
    unit = "행" if axis == "y" else "열"
    signed_axis = f"{'+' if sign > 0 else '−'}{axis}"
    where = f"로봇 {reading['side_ko']}({signed_axis})" if reading.get("side_ko") else signed_axis
    lo, hi = reading["bounds_mm"]
    return (f"{axis} {lines} mm {unit}만 실패 → {where} 방향 여유는 {lo:g} mm 이상 {hi:g} mm 미만"
            f"(격자 간격 {reading['step_mm']:g} mm로 본 추정)")


def _incident_record(scenario: dict[str, Any]) -> dict[str, Any] | None:
    """시나리오 원천(role=incident_run)의 결과 파일에서 기록된 사고의 판정 시각·접촉력·컵 오프셋을 읽는다."""
    for src in scenario.get("sources") or []:
        path = _resolve(src.get("path"))
        if src.get("role") != "incident_run" or path is None or path.name != "result.json" or not path.exists():
            continue
        result = _load_json(path) or {}
        plan = _load_json(path.parent / "plan.json") or {}
        event = next((ev for ev in result.get("events") or [] if ev.get("type") == "preclose_failure"), None)
        samples = result.get("samples") or []
        if event is None or not isinstance(event.get("sample_index"), int) or event["sample_index"] >= len(samples):
            continue
        s = samples[event["sample_index"]]
        force = [round(float(v), 3) for v in s.get("contact_force_n") or []]
        return {
            "run_name": path.parent.name,
            "path": display_path(path),
            "offset_mm": _mm_list((plan.get("config") or {}).get("cup_spawn_offset_m") or [0.0, 0.0, 0.0]),
            "recovery_enabled": result.get("recovery_enabled"),
            "t_s": round(float(event.get("time_s", s["time_s"])), 3),
            "failure_code": event.get("failure_code"),
            "phase": s.get("phase"),
            "force_n": force,
            "pad": PAD_KEYS[force.index(max(force))] if force and max(force) > 0 else None,
            "cup_disp_mm": round(float(s.get("cup_displacement_from_start_m") or 0.0) * 1000, 3),
        }
    return None


def _map_subtitle(cond: dict[str, Any]) -> str:
    text = f"통과 {cond.get('pass', 0)}/{cond.get('valid', 0)}"
    infra = int(cond.get("infra", 0) or 0) + int(cond.get("timeout", 0) or 0)
    if infra:
        text += f"  infra {infra}"
    if cond.get("invalid"):
        text += "  지도 무효"
    return text


def _regress_view(
    profile: Profile,
    scenario: dict[str, Any],
    regress: dict[str, Any],
    media: MediaBuilder,
    records: dict[tuple[str, float, float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sid = scenario["scenario_id"]
    xs, ys = _grid_axes(scenario)
    doc = scrub({k: v for k, v in regress.items() if not k.startswith("_")})
    doc["grid"] = {"x_mm": xs, "y_mm": ys}
    doc["verifiers"] = scenario.get("verifiers", [])
    doc["scenario_conditions"] = {
        name: {"label_ko": c.get("label_ko"), "judgement_basis": c.get("judgement_basis")}
        for name, c in scenario["conditions"].items()
    }
    doc["runs_root"] = display_path(profile.artifacts_root / "runs")
    doc["complete"] = is_complete_grid(scenario, regress)
    cup_task = scenario["task"] == "cup_contact"
    recordings: dict[tuple[str, float, float], dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    by_key: dict[tuple[str, float, float], dict[str, Any]] = {}
    for name, cond in (doc.get("conditions") or {}).items():
        for cell in cond.get("cells") or []:
            x, y = _cell_key(cell)
            ev = None
            if cup_task:
                result_path = _cell_result_path(profile.artifacts_root, cell.get("run_id"))
                ev = cup_evidence(result_path) if result_path else None
                if ev is not None:
                    stem = f"{name}_x{x:g}_y{y:g}".replace(".", "p")
                    rel = f"data/regress-cell/{sid}/{stem}.json"
                    cell["evidence"] = rel
                    cell["min_gap_mm"] = _gap_of(ev, 0)
                    evidence[rel] = ev
                    by_key[(name, x, y)] = ev
            video, incident_t = _cell_recording(profile.artifacts_root, cell.get("run_id"))
            video_ev = ev
            record_cell = (records or {}).get((name, x, y))
            if video is None and record_cell is not None:
                video, incident_t = _cell_recording(profile.artifacts_root, record_cell.get("run_id"))
                if video is not None:
                    cell["record_run_id"] = record_cell.get("run_id")
                    cell["record_verdict"] = record_cell.get("verdict")
                    if cup_task:
                        record_path = _cell_result_path(profile.artifacts_root, record_cell.get("run_id"))
                        video_ev = (cup_evidence(record_path) if record_path else None) or ev
            if video is None:
                continue
            stem = f"{sid}--{name}--x{x:g}--y{y:g}".replace(".", "p")
            # 원거리 카메라라 컵·그리퍼 주변만 고정 ROI로 잘라 확대한다(컵 색으로 첫 프레임에서 찾음)
            roi = mediacmd.find_object_roi(video, 0.5) if media.enabled and scenario["task"] == "cup_contact" else None
            verdict = cell.get("record_verdict") or _cell_state(cell)
            cell["video"] = media.make(
                f"{stem}.mp4",
                lambda out, crf, v=video, r=roi: mediacmd.clip_command(v, out, fps=FPS, crop=r, crf=crf),
                [video],
                budget=CLIP_BUDGET,
            )
            if cell["video"]:
                clip = media.dir / f"{stem}.mp4"
                at = (incident_t if verdict == "fail" and incident_t else mediacmd.probe_duration(clip) - 0.5)
                cell["poster"] = media.make(
                    f"{stem}.webp",
                    lambda out, crf, c=clip, t=max(0.0, at - 0.1): mediacmd.poster_command(c, out, at_s=t),
                    [clip],
                )
            cell["roi"] = list(roi) if roi else None
            recordings[(name, x, y)] = {
                "path": video, "incident_t_s": incident_t, "cell": cell, "roi": roi, "evidence": video_ev,
            }
    axes = next((((ev.get("geometry") or {}).get("axes")) for ev in by_key.values()
                 if (ev.get("geometry") or {}).get("axes")), None)
    doc["axes"] = axes
    for cond in (doc.get("conditions") or {}).values():
        cond["pattern_ko"] = grid_patterns(cond, xs, ys) if doc["complete"] else []
        rows, cols = failed_lines(cond, xs, ys) if doc["complete"] else ([], [])
        cond["failed_rows_mm"], cond["failed_cols_mm"] = rows, cols
        readings = grid_readings(cond, xs, ys, axes) if doc["complete"] else []
        cond["readings"] = [{**r, "text_ko": _reading_text(r)} for r in readings]
    if cup_task and by_key:
        doc["geometry_check"] = _geometry_check(doc, by_key)
        doc["line_gaps"] = _line_gaps(doc, by_key, xs, ys)
        doc["story"] = _cup_story(scenario, doc, by_key)
    panels = []
    for name in ("A", "B"):
        cond = (doc.get("conditions") or {}).get(name)
        if cond is None:
            continue
        label = scenario["conditions"].get(name, {}).get("label_ko") or name
        subtitle = _map_subtitle(cond)
        if cond.get("pattern_ko"):
            subtitle += "   " + ", ".join(cond["pattern_ko"])
        panels.append(
            {
                "title": f"조건 {name}  {label}",
                "subtitle": subtitle,
                "x_mm": xs,
                "y_mm": ys,
                "cells": {_cell_key(c): _cell_state(c) for c in cond.get("cells") or []},
                "outline_rows": cond["failed_rows_mm"],
                "outline_cols": cond["failed_cols_mm"],
                "x_title": f"x 오프셋 (mm, + = 로봇 {axes['x_plus_ko']})" if axes else "x 오프셋 (mm)",
                "y_title": f"y (mm, + = 로봇 {axes['y_plus_ko']})" if axes else "y (mm)",
            }
        )
    footer = next((r["text_ko"] for r in ((doc.get("conditions") or {}).get("A") or {}).get("readings") or []), None)
    if panels and media.enabled and media.font:
        png = media.make(
            f"{sid}--passmap.png",
            lambda out, crf: mediacmd.pass_map_command(
                panels, out, font_path=media.font, bold_font_path=media.bold, footer=footer
            ),
            [],
        )
        doc["pass_map_png"] = png
        if png:
            doc["pass_map_webp"] = media.make(
                f"{sid}--passmap.webp",
                lambda out, crf: mediacmd.image_webp_command(media.dir / f"{sid}--passmap.png", out),
                [media.dir / f"{sid}--passmap.png"],
            )
    doc["_recordings"] = recordings
    doc["_evidence"] = evidence
    return doc


def _geometry_check(doc: dict[str, Any], by_key: dict[tuple[str, float, float], dict[str, Any]]) -> dict[str, Any]:
    """조건 A: 계산한 닫기 전 최소 여유(첫 시도)가 0 미만인 칸과 실패한 칸이 몇 칸 일치하는지."""
    agree = total = 0
    for cell in ((doc.get("conditions") or {}).get("A") or {}).get("cells") or []:
        gap = _gap_of(by_key.get(("A", *_cell_key(cell))), 0)
        state = _cell_state(cell)
        if gap is None or state not in ("pass", "fail"):
            continue
        total += 1
        agree += int((gap < 0) == (state == "fail"))
    return {"condition": "A", "agree": agree, "total": total}


def _line_gaps(
    doc: dict[str, Any], by_key: dict[tuple[str, float, float], dict[str, Any]], xs: list[float], ys: list[float]
) -> dict[str, list[dict[str, Any]]]:
    """조건 A의 가운데 열(x가 0에 가장 가까운 열)·가운데 행을 따라 계산한 최소 여유."""
    cells = {_cell_key(c): c for c in ((doc.get("conditions") or {}).get("A") or {}).get("cells") or []}
    out: dict[str, list[dict[str, Any]]] = {}
    if xs and ys:
        x0 = min(xs, key=abs)
        y0 = min(ys, key=abs)
        for axis, keys in (("y", [(x0, y) for y in sorted(ys)]), ("x", [(x, y0) for x in sorted(xs)])):
            items = []
            for k in keys:
                ev = by_key.get(("A", *k))
                gaps = ((ev or {}).get("geometry") or {}).get("min_gap") or []
                gap = next((g for g in gaps if g.get("attempt") == 0), None)
                if gap is None:
                    continue
                items.append({"x_mm": k[0], "y_mm": k[1], "gap_mm": gap["gap_mm"], "pad": gap["pad"],
                              "state": _cell_state(cells.get(k))})
            out[axis] = items
    return out


def _cup_story(
    scenario: dict[str, Any], doc: dict[str, Any], by_key: dict[tuple[str, float, float], dict[str, Any]]
) -> dict[str, Any]:
    """기록된 사고 → 같은 조건 재현(조건 A 같은 오프셋) → 격자 A → 격자 B 흐름에 쓰는 사실."""
    recorded = _incident_record(scenario)
    conditions = doc.get("conditions") or {}
    a_cells = {_cell_key(c): c for c in (conditions.get("A") or {}).get("cells") or []}
    point = None
    if recorded:
        point = (float(recorded["offset_mm"][0]), float(recorded["offset_mm"][1]))
    if point not in a_cells:
        fails = [k for k, c in a_cells.items() if _cell_state(c) == "fail"]
        point = min(fails, key=lambda k: (abs(k[0]) + abs(k[1]), k)) if fails else None
    cells: dict[str, Any] = {}
    for name, cond in conditions.items():
        cell = next((c for c in cond.get("cells") or [] if point and _cell_key(c) == point), None)
        if cell is None:
            continue
        ev = by_key.get((name, *point)) or {}
        cells[name] = {
            "state": _cell_state(cell),
            "failure_code": cell.get("failure_code"),
            "run_id": cell.get("run_id"),
            "evidence": cell.get("evidence"),
            "video": cell.get("video"),
            "poster": cell.get("poster"),
            "verdict": ev.get("verdict"),
            "outcome": ev.get("outcome"),
            "events": ev.get("events") or [],
            "min_gap": ((ev.get("geometry") or {}).get("min_gap")) or [],
        }
    a_verdict = (cells.get("A") or {}).get("verdict") or {}
    same_time = (
        bool(recorded) and isinstance(a_verdict.get("t_s"), (int, float))
        and abs(float(a_verdict["t_s"]) - float(recorded["t_s"])) < 1e-3
        and a_verdict.get("failure_code") == recorded.get("failure_code")
    )
    return {
        "recorded": recorded,
        "incident": scenario.get("incident"),
        "point": {"x_mm": point[0], "y_mm": point[1]} if point else None,
        "cells": cells,
        "same_time": same_time,
    }


# ---------------------------------------------------------------- 히어로


def _cup_hero(
    scenario: dict[str, Any], regress: dict[str, Any], media: MediaBuilder
) -> dict[str, Any] | None:
    sid = scenario["scenario_id"]
    conditions = regress.get("conditions") or {}
    cond_a, cond_b = conditions.get("A") or {}, conditions.get("B") or {}
    recordings: dict = regress.get("_recordings") or {}
    a_cells = [c for c in cond_a.get("cells") or [] if ("A", *_cell_key(c)) in recordings]
    fails = [c for c in a_cells if _cell_state(c) == "fail"]
    fallback = False
    if fails:
        b_ok = [c for c in fails if ("B", *_cell_key(c)) in recordings]
        chosen = sorted(b_ok or fails, key=lambda c: (abs(_cell_key(c)[0]) + abs(_cell_key(c)[1]), _cell_key(c)))[0]
    elif a_cells and int(cond_a.get("pass", 0)) == int(cond_a.get("valid", -1)):
        chosen = min(a_cells, key=_check_margin)
        fallback = True
    else:
        return None
    if not (media.enabled and media.font):
        return None
    x, y = _cell_key(chosen)
    default_t = float((scenario.get("incident") or {}).get("t_start_s") or 0.0)
    segments = []
    for name in ("A", "B"):
        rec = recordings.get((name, x, y))
        if rec is None:
            continue
        cell = rec["cell"]
        duration = mediacmd.probe_duration(rec["path"])
        label = _short_label(scenario["conditions"].get(name, {}).get("label_ko") or name)
        verdict = cell.get("record_verdict") or _cell_state(cell) or ""
        seg: dict[str, Any] = {"path": rec["path"], "crop": rec.get("roi")}
        if verdict == "fail":
            # 판정 시각 앞 4.5초 + 마지막 프레임 1초 정지(판정 순간을 멈춰 보여 준다)
            t_mark = rec["incident_t_s"] if rec["incident_t_s"] is not None else default_t
            start = max(0.0, t_mark - 4.5)
            length = min(6.0, max(1.0, duration - start) + 1.0)
            code = cell.get("failure_code")
            seg["label"] = f"조건 {name} {label} · x {_mm(x)} mm, y {_mm(y)} mm · {FAILURE_KO.get(code, '실패')}"
            rel = t_mark - start
            if 0.0 <= rel <= length:
                seg["highlight"] = (round(max(0.0, rel - 0.3), 3), round(length, 3))
                seg["highlight_label"] = f"{FAILURE_KO.get(code, '실패')} 판정 {t_mark:.3f} s"
                found = (rec.get("evidence") or {}).get("verdict") or {}
                if found.get("pad") and found.get("force_n"):
                    # 화면에서는 거의 보이지 않는 접촉이라 힘·이동량을 글자로 얹는다
                    seg["highlight_detail"] = (
                        f"{PAD_KO[found['pad']]} {max(found['force_n']):.2f} N 접촉 · "
                        f"컵 수평 이동 {found['cup_disp_mm']:.2f} mm"
                    )
        else:
            # 통과 셀은 결과(파지·들어 올림)가 끝부분에 있으므로 마지막 6초
            start = max(0.0, duration - 6.0)
            length = max(1.0, min(6.0, duration))
            recover = bool((scenario["conditions"].get(name, {}).get("params") or {}).get("recover"))
            events = [ev.get("type") for ev in (rec.get("evidence") or {}).get("events") or []]
            if recover and verdict == "pass" and "replanned" in events:
                outcome = "접촉 판정 뒤 후퇴·재계획 → 들어 올림 통과"
            else:
                plain = {"pass": "통과"}.get(verdict, "infra")
                outcome = "복구 후 들어 올림 통과" if recover and verdict == "pass" else plain
            seg["label"] = f"조건 {name} {label} · 같은 격자점 · {outcome}"
        seg["start_s"] = round(start, 3)
        seg["length_s"] = round(length, 3)
        segments.append(seg)
    passmap = media.dir / f"{sid}--passmap.png"
    if passmap.exists():
        segments.append({"path": passmap, "image": True, "length_s": 5.5, "fade_in": True})
    inputs = [Path(s["path"]) for s in segments]
    video = media.make(
        "hero.mp4",
        lambda out, crf: mediacmd.hero_sequence_command(segments, out, fps=FPS, font_path=media.font, crf=crf),
        inputs,
        budget=HERO_BUDGET,
    )
    # 포스터 = 조건 A 판정 강조 구간 한가운데(판정 순간이 첫 화면에 보이게)
    mark = segments[0].get("highlight") if segments else None
    poster_at = (mark[0] + mark[1]) / 2 if mark else 1.0
    poster = media.make(
        "hero.webp", lambda out, crf: mediacmd.poster_command(media.dir / "hero.mp4", out, at_s=poster_at),
        [media.dir / "hero.mp4"],
    )
    return {
        "source": "cup_grid",
        "scenario_id": sid,
        "video": video,
        "poster": poster,
        "fallback_min_margin": fallback,
        "point": {"x_mm": x, "y_mm": y},
        "grid": _grid_summary(scenario),
        "conditions": {
            name: {
                "label_ko": scenario["conditions"].get(name, {}).get("label_ko"),
                "judgement_basis": c.get("judgement_basis"),
                "pass": c.get("pass"),
                "valid": c.get("valid"),
                "invalid": bool(c.get("invalid")),
            }
            for name, c in (("A", cond_a), ("B", cond_b))
            if c
        },
        "assembly": scenario.get("assembly"),
        "width": mediacmd.HERO_SIZE[0],
        "height": mediacmd.HERO_SIZE[1],
        "pattern_ko": {name: c.get("pattern_ko") or [] for name, c in (("A", cond_a), ("B", cond_b)) if c},
        "readings": {
            name: [r["text_ko"] for r in c.get("readings") or []] for name, c in (("A", cond_a), ("B", cond_b)) if c
        },
        "segments": [
            {k: seg.get(k) for k in ("label", "highlight_label", "highlight_detail", "length_s") if seg.get(k)}
            for seg in segments
        ],
        "length_s": round(sum(float(s["length_s"]) for s in segments), 2),
    }


def _freeze_hero(scenario: dict[str, Any], view: dict[str, Any], media: MediaBuilder) -> dict[str, Any] | None:
    real, ghost = view.get("_real_path"), view.get("_ghost_path")
    if not (real and ghost and real.exists() and ghost.exists() and media.enabled and media.font):
        return None
    duration = float(view.get("duration_s") or 0.0)
    a, b = view["incident"]["t_start_s"], view["incident"]["t_end_s"]
    length = min(HERO_FREEZE_LENGTH_S, duration)
    start = max(0.0, min(a - 3.0, duration - length))
    rel = (round(a - start, 3), round(b - start, 3))
    video = media.make(
        "hero.mp4",
        lambda out, crf: mediacmd.side_by_side_command(
            real, ghost, out, fps=FPS,
            labels=(view["real"]["label_ko"], "명령(반투명)·관측  SO-101 MuJoCo 미러"),
            font_path=media.font, incident=rel, incident_label="관측 동결 구간",
            start_s=round(start, 3), length_s=round(length, 3), crf=crf,
        ),
        [real, ghost],
        budget=HERO_BUDGET,
    )
    poster = media.make(
        "hero.webp",
        lambda out, crf: mediacmd.poster_command(media.dir / "hero.mp4", out, at_s=(rel[0] + rel[1]) / 2),
        [media.dir / "hero.mp4"],
    )
    return {
        "source": "observation_freeze",
        "scenario_id": scenario["scenario_id"],
        "condition": view["condition"],
        "video": video,
        "poster": poster,
        "clip": {"start_s": round(start, 3), "length_s": round(length, 3)},
        "incident": view["incident"],
        "metrics": view["metrics"],
        "origin": scenario["origin"],
        "judgement_basis": scenario["judgement_basis"],
        "real_label_ko": view["real"]["label_ko"],
    }


def _story_summary(story: dict[str, Any] | None) -> dict[str, Any] | None:
    if not story:
        return None
    recorded = story.get("recorded") or {}
    a = ((story.get("cells") or {}).get("A") or {}).get("verdict") or {}
    return {
        "point": story.get("point"),
        "recorded_run": recorded.get("run_name"),
        "recorded_t_s": recorded.get("t_s"),
        "recorded_force_n": max(recorded.get("force_n") or [0.0]) if recorded else None,
        "replay_t_s": a.get("t_s"),
        "replay_force_n": max(a.get("force_n") or [0.0]) if a else None,
        "replay_pad": a.get("pad"),
        "replay_cup_disp_mm": a.get("cup_disp_mm"),
        "same_time": story.get("same_time"),
    }


def _cases(
    first_freeze: tuple[dict[str, Any], dict[str, Any]] | None,
    cup: dict[str, Any] | None,
    cup_regress: dict[str, Any] | None,
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """첫 화면의 사례 두 가지: ① 실측 기록 재생 ② 시뮬 회귀 평가. 둘은 서로 다른 로봇·기록이다."""
    posters = {c["scenario_id"]: c.get("poster") for c in cards}
    out = []
    if first_freeze is not None:
        scenario, view = first_freeze
        out.append({
            "kind": "replay",
            "scenario_id": scenario["scenario_id"],
            "title_ko": scenario["title_ko"],
            "origin": scenario["origin"],
            "judgement_basis": scenario["judgement_basis"],
            "assembly": scenario.get("assembly"),
            "condition": view["condition"],
            "incident": view["incident"],
            "metrics": view["metrics"],
            "poster": view["real"].get("poster") or posters.get(scenario["scenario_id"]),
        })
    if cup is not None:
        measured = cup_regress is not None and bool(cup_regress.get("complete"))
        conditions = (cup_regress or {}).get("conditions") or {}
        out.append({
            "kind": "grid",
            "scenario_id": cup["scenario_id"],
            "title_ko": cup["title_ko"],
            "origin": cup["origin"],
            "judgement_basis": cup["judgement_basis"],
            "assembly": cup.get("assembly"),
            "measured": measured,
            "grid": _grid_summary(cup),
            "conditions": {
                name: {
                    "label_ko": c.get("label_ko"),
                    **({k: (conditions.get(name) or {}).get(k) for k in ("pass", "valid")} if measured else {}),
                }
                for name, c in cup["conditions"].items()
            },
            "readings": [r["text_ko"] for r in (conditions.get("A") or {}).get("readings") or []] if measured else [],
            "poster": (cup_regress or {}).get("pass_map_webp") or posters.get(cup["scenario_id"]),
        })
    return out


# ---------------------------------------------------------------- 빌드


def _copy_assets(out: Path) -> None:
    for name in ASSET_FILES:
        src = ASSETS_DIR / name
        if not src.exists():
            raise RuntimeError(f"리포트 화면 자산 없음: {name}")
        shutil.copyfile(src, out / name)


def check_no_private_paths(out: Path) -> list[str]:
    """빌드 산출물 전체(바이너리 포함)에서 `/home/<user>`·`/data/<user>`를 찾는다."""
    needles = [p.encode() for p in _private_prefixes()]
    hits = []
    for path in sorted(out.rglob("*")):
        if not path.is_file() or path.name == ".cache.json":
            continue
        blob = path.read_bytes()
        if any(n in blob for n in needles):
            hits.append(str(path.relative_to(out)))
    return hits


def check_no_excluded(out: Path, needles: list[str]) -> list[str]:
    """공개 빌드 산출물 전체(바이너리·미디어 캐시 포함)에서 제외 시나리오 id·제외 status 문자열을 찾는다."""
    encoded = [n.encode() for n in needles if n]
    hits = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and any(n in path.read_bytes() for n in encoded):
            hits.append(str(path.relative_to(out)))
    return hits


def default_out_dir(profile: Profile, *, private: bool) -> Path:
    """비공개 빌드는 공개 폴더를 덮어쓰지 않도록 `<report_dir>-private`에 굽는다."""
    if not private:
        return profile.report_dir
    return profile.report_dir.with_name(profile.report_dir.name + "-private")


def _site_size(out: Path) -> int:
    return sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and p.name != ".cache.json")


def build(
    profile: Profile,
    *,
    private: bool = False,
    out_dir: Path | None = None,
    scenarios_dir: Path | None = None,
    media: bool = True,
    hero_source: str | None = None,
) -> dict[str, Any]:
    hero_source = hero_source or os.environ.get("ROBOT_OPS_HERO_SOURCE") or "auto"
    if hero_source not in HERO_SOURCES:
        raise ValueError(f"hero_source는 {HERO_SOURCES} 중 하나여야 합니다: {hero_source}")
    out = out_dir or default_out_dir(profile, private=private)
    if out.is_symlink():
        raise RuntimeError(f"리포트 폴더가 심링크라 쓰지 않습니다: {out}")
    out.mkdir(parents=True, exist_ok=True)
    scenarios = list_scenarios(scenarios_dir or profile.base_dir / "scenarios")
    all_regress = latest_regress_results(profile.artifacts_root)
    # regress가 정한 status(drive_kinematic)가 있으면 카드 status로 쓴다. 공개 제외는 파일 status와
    # regress status 중 하나라도 제외 대상이면 적용한다.
    excluded_ids = set()
    for doc in scenarios:
        if not is_public(doc, profile.public_exclude_status):
            excluded_ids.add(doc["scenario_id"])
        derived = (all_regress.get(doc["scenario_id"]) or {}).get("scenario_status")
        if doc["task"] in STATUS_RULE_TASKS and isinstance(derived, dict) and derived.get("status"):
            doc["status"] = derived["status"]
            doc["model_note"] = derived.get("model_note")
            if not is_public(doc, profile.public_exclude_status):
                excluded_ids.add(doc["scenario_id"])
    if not private:
        scenarios = [s for s in scenarios if s["scenario_id"] not in excluded_ids]
    included = {s["scenario_id"] for s in scenarios}
    regress = {sid: doc for sid, doc in all_regress.items() if sid in included}
    replays = latest_replay_results(profile.artifacts_root)
    records = record_results(profile.artifacts_root)

    data_dir = out / "data"
    _reset_data_dir(data_dir)
    builder = MediaBuilder(out, enabled=media)

    cards = []
    details: dict[str, dict[str, Any]] = {}
    regress_views: dict[str, dict[str, Any]] = {}
    first_freeze: tuple[dict[str, Any], dict[str, Any]] | None = None
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        card = _card(scenario)
        detail: dict[str, Any] = {**card, "views": [], "provenance": scenario.get("provenance") or {}}
        if scenario["task"] == "lerobot_episode":
            for cond_name, cond in scenario["conditions"].items():
                raw = str((cond.get("params") or {}).get("source", ""))
                kind, _, src_path = raw.partition(":")
                episode = (cond.get("params") or {}).get("episode")
                if kind != "lerobot" or not isinstance(episode, int):
                    continue
                replay = replays.get((display_path(str(_resolve(src_path))), episode)) or replays.get(
                    (src_path, episode)
                )
                if replay is None:
                    continue
                view = _replay_view(profile, scenario, cond_name, cond, replay, builder)
                detail["views"].append(view)
                if first_freeze is None and view["ghost"]["available"]:
                    first_freeze = (scenario, view)
        if sid in regress and scenario["task"] in GRID_TASKS:
            regress_views[sid] = _regress_view(profile, scenario, regress[sid], builder, records.get(sid))
            detail["regress"] = {
                name: {k: c.get(k) for k in ("label_ko", "judgement_basis", "pass", "valid", "invalid")}
                for name, c in (regress_views[sid].get("conditions") or {}).items()
            }
        poster = None
        for view in detail["views"]:
            poster = view["real"].get("poster") or view["ghost"].get("poster")
            if poster:
                break
        if poster is None and sid in regress_views:
            a_cells = ((regress_views[sid].get("conditions") or {}).get("A") or {}).get("cells") or []
            fail_posters = [c["poster"] for c in a_cells if c.get("poster") and _cell_state(c) == "fail"]
            poster = (fail_posters[len(fail_posters) // 2] if fail_posters else None) or regress_views[sid].get(
                "pass_map_webp"
            )
        card["poster"] = poster
        card["view_count"] = len(detail["views"])
        card["has_regress"] = sid in regress_views
        cards.append(card)
        details[sid] = detail

    hero: dict[str, Any] | None = None
    cup = next((s for s in scenarios if s["task"] == "cup_contact"), None)
    cup_regress = regress_views.get(cup["scenario_id"]) if cup is not None else None
    if hero_source in ("auto", "cup_grid") and cup_regress is not None and cup_regress.get("complete"):
        hero = _cup_hero(cup, cup_regress, builder)
    if hero is None and hero_source in ("auto", "observation_freeze") and first_freeze is not None:
        hero = _freeze_hero(*first_freeze, builder)
    if hero is None:
        hero = {"source": None}
    if cup is not None:
        measured = cup_regress is not None and bool(cup_regress.get("complete"))
        hero["cup_grid"] = {
            "scenario_id": cup["scenario_id"],
            "measured": measured,
            "grid": _grid_summary(cup),
            "conditions": {
                name: {
                    "label_ko": c.get("label_ko"),
                    "judgement_basis": c.get("judgement_basis"),
                    **(
                        {k: (cup_regress["conditions"].get(name) or {}).get(k) for k in ("pass", "valid", "invalid")}
                        if measured
                        else {}
                    ),
                }
                for name, c in cup["conditions"].items()
            },
            "assembly": cup.get("assembly"),
            "origin": cup.get("origin"),
            "readings": {
                name: [r["text_ko"] for r in (c or {}).get("readings") or []]
                for name, c in ((cup_regress or {}).get("conditions") or {}).items()
            } if measured else {},
            "pattern_ko": {
                name: (c or {}).get("pattern_ko") or []
                for name, c in ((cup_regress or {}).get("conditions") or {}).items()
            } if measured else {},
            "geometry_check": (cup_regress or {}).get("geometry_check") if measured else None,
            "story": _story_summary((cup_regress or {}).get("story")) if measured else None,
        }
    hero["cases"] = _cases(first_freeze, cup, cup_regress, cards)

    write_json(data_dir / "scenarios.json", cards)
    for sid, detail in details.items():
        for view in detail["views"]:
            view.pop("_real_path", None)
            view.pop("_ghost_path", None)
        write_json(data_dir / "scenario" / f"{sid}.json", scrub(detail))
    for sid, doc in sorted(regress_views.items()):
        doc.pop("_recordings", None)
        for rel, ev in sorted((doc.pop("_evidence", None) or {}).items()):
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            # 시계열 배열이 길어 들여쓰기 없이 쓴다
            path.write_text(json.dumps(scrub(ev), ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        write_json(data_dir / "regress" / f"{sid}.json", scrub(doc))
    write_json(data_dir / "hero.json", scrub(hero))
    write_json(data_dir / "out_of_scope.json", OUT_OF_SCOPE)
    _copy_assets(out)
    builder.finish()
    size = _site_size(out)
    warnings = list(builder.warnings)
    if size > SITE_BUDGET:
        warnings.append(f"사이트 크기 {size} B가 예산 {SITE_BUDGET} B를 넘습니다")
    summary = {
        "schema": REPORT_SCHEMA,
        "build": "private" if private else "public",
        "profile": profile.id,
        "title_ko": profile.project.get("title_ko"),
        "scenario_count": len(cards),
        "regress_count": len(regress_views),
        "hero_source": hero.get("source"),
        "hero_request": hero_source,
        "media": bool(media),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "warnings": scrub(warnings),
    }
    write_json(data_dir / "build.json", summary)
    summary["size_bytes"] = _site_size(out)
    leaks = check_no_private_paths(out)
    if leaks:
        raise RuntimeError(f"빌드 산출물에 개인 절대경로가 남았습니다: {leaks}")
    if not private:
        excluded_hits = check_no_excluded(out, sorted(excluded_ids) + list(profile.public_exclude_status))
        if excluded_hits:
            raise RuntimeError(f"공개 빌드 산출물에 제외 시나리오 흔적이 남았습니다: {excluded_hits}")
    return {**summary, "out_dir": display_path(out)}


# ---------------------------------------------------------------- 미리보기 서버(Range 지원)
#
# `python3 -m http.server`는 HTTP Range를 지원하지 않아 Chrome에서 영상 seekable 범위가 [0, 0]이 된다
# (그래프 클릭·`사고 지점으로`·영상 동기가 동작하지 않음). GitHub Pages 등 실제 호스팅은 Range를 지원한다.
# 로컬 미리보기는 아래 stdlib 서버를 쓴다:
#   uv run python -m robot_ops.replay.report serve --dir .local/report --port 8766


def _range_handler():
    import http.server
    import re

    class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
        def send_head(self):  # noqa: ANN201 - stdlib 시그니처
            header = self.headers.get("Range")
            path = self.translate_path(self.path)
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip()) if header else None
            if match is None or not os.path.isfile(path):
                return super().send_head()
            size = os.path.getsize(path)
            start_text, end_text = match.groups()
            if start_text:
                start = int(start_text)
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                start = max(0, size - int(end_text or 0))
                end = size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
            handle = open(path, "rb")  # noqa: SIM115 - copyfile 뒤 stdlib가 닫는다
            handle.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._range_left = end - start + 1
            return handle

        def copyfile(self, source, outputfile):  # noqa: ANN001
            left = getattr(self, "_range_left", None)
            if left is None:
                return super().copyfile(source, outputfile)
            while left > 0:
                chunk = source.read(min(1 << 16, left))
                if not chunk:
                    break
                outputfile.write(chunk)
                left -= len(chunk)
            self._range_left = None
            return None

        def end_headers(self) -> None:
            if not getattr(self, "_range_left", None):
                self.send_header("Accept-Ranges", "bytes")
            super().end_headers()

    return RangeRequestHandler


def serve_in_thread(directory: Path, *, port: int = 0, bind: str = "127.0.0.1"):
    """테스트용: 백그라운드 스레드에서 미리보기 서버를 띄우고 서버 객체를 돌려준다(shutdown 필요)."""
    import functools
    import http.server
    import threading

    handler = functools.partial(_range_handler(), directory=str(directory))
    httpd = http.server.ThreadingHTTPServer((bind, port), handler)
    threading.Thread(target=httpd.serve_forever, name="robot-ops-report-serve", daemon=True).start()
    return httpd


def serve(directory: Path, *, port: int = 8766, bind: str = "127.0.0.1") -> None:
    import functools
    import http.server

    handler = functools.partial(_range_handler(), directory=str(directory))
    with http.server.ThreadingHTTPServer((bind, port), handler) as httpd:
        print(f"미리보기: http://{bind}:{port}/ (Ctrl+C로 종료)", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m robot_ops.replay.report")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="Range 지원 로컬 미리보기 서버")
    serve_parser.add_argument("--dir", type=Path, default=Path(".local/report"))
    serve_parser.add_argument("--port", type=int, default=8766)
    serve_parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    serve(args.dir, port=args.port, bind=args.bind)
