"""replay 테스트 공용 도우미: 임시 프로파일·시나리오 생성."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.replay.profile import Profile, load_profile  # noqa: E402

FAKE_ADAPTER = Path(__file__).resolve().parent / "fake_adapter.py"
GRID5 = [-5, -2.5, 0, 2.5, 5]


def write_profile(tmp: Path, *, isolation: str = "pgid", extra: str = "") -> Profile:
    (tmp / "profiles").mkdir(parents=True, exist_ok=True)
    path = tmp / "profiles" / "fake.toml"
    path.write_text(
        f"""
[project]
id = "fake"
title_ko = "가짜 프로젝트"
source_repo = "{tmp / 'no-team-repo'}"
pinned_commit = "0000000"

[adapter]
entry = "{FAKE_ADAPTER}"
protocol = "robot-ops-adapter/1"

[interpreters]
fake = "{sys.executable}"

[adapter.tasks.cup_contact]
run = "fake"
judge = "core"

[adapter.tasks.drive_kinematic]
run = "fake"
judge = "core"

[adapter.tasks.lerobot_episode]
convert = "fake"
run = "fake"

[adapter.allowlist]
scripts = ["tools/simulate_cup_contact.py"]
imports = ["tools/mobile_service_control.py"]

[limits]
timeout_s = 30
kill_after_s = 1
memory_max = "1G"
min_free_disk_gb = 0
lock_file = "{tmp / 'run.lock'}"
isolation = "{isolation}"

[artifacts]
root = "{tmp / 'artifacts'}"
report_dir = "{tmp / 'report'}"
delete_patterns = ["scene.usda", "frames/*.png"]

[replay]
threshold = {{ joint_deg = 3.0, tcp_mm = 20.0 }}

[public]
exclude_status = ["after_fix_regression"]
{extra}
""",
        encoding="utf-8",
    )
    return load_profile(path)


BASE_SCENARIO: dict[str, Any] = {
    "schema": "robot-ops-scenario/1",
    "scenario_id": "fake-cup",
    "title_ko": "가짜 컵 시나리오",
    "origin": "sim_run",
    "judgement_basis": "rigid_proxy_lift",
    "assembly": "left_arm_proxy",
    "adapter": "fake",
    "task": "cup_contact",
    "replayable": {"value": True, "layer": "ik_reachability", "reason": "test"},
    "sources": [{"path": "~/does-not-exist/result.json", "sha256": "0" * 64, "role": "incident_run"}],
    "failure_record_id": None,
    "incident": {"t_start_s": 0.0, "t_end_s": 1.0, "code": "premature_cup_contact", "detected_by": "verifier"},
    "conditions": {
        "A": {"label_ko": "복구 끔", "params": {}, "judgement_basis": "rigid_proxy_lift"},
        "B": {"label_ko": "복구 켬(정답 좌표 조건)", "params": {}, "judgement_basis": "oracle_recovery_sim_coordinates"},
    },
    "grid": {"spawn_x_offset_mm": GRID5, "spawn_y_offset_mm": GRID5, "repeat_points": 5, "repeats": 3},
    "verifiers": ["proxy_lift_height"],
    "limits": {"timeout_s": None},
    "status": "active",
    "model_note": None,
    "provenance": {"created_at": "2026-09-27", "tool_version": "test"},
}


def make_scenario(**overrides: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE_SCENARIO)
    params = overrides.pop("params", None)
    if params is not None:
        for name, value in params.items():
            doc["conditions"][name]["params"] = value
    grid = overrides.pop("grid", None)
    if grid is not None:
        doc["grid"].update(grid)
    doc.update(overrides)
    return doc


def save_scenario(base: Path, doc: dict[str, Any]) -> Path:
    path = base / "scenarios" / doc["scenario_id"] / "scenario.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path
