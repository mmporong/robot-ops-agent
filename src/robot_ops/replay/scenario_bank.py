"""`scenario.json` 로드·검증·소스 SHA 확인·status 규칙.

status 우선순위: after_fix_regression > not_reproducible_in_model > stale > active.
동시에 성립하는 모델 관련 사실(not_reproducible_in_model)은 status가 더 높은 값일 때
`model_note`에 보존한다.

status 규칙은 코드 수정 전/후 대조 과제(`drive_kinematic`, A=수정 전, B=HEAD)에만 적용한다.
- A에서 실패 셀이 하나도 없으면(사고 미재현) → not_reproducible_in_model
- 같은 격자점에서 A 통과·B 실패인 셀이 있으면 → after_fix_regression
- A에서 재현됐고 B에 실패 셀이 없으면(과거 사고가 HEAD에서 사라짐) → stale
- 그 밖 → active
컵(`cup_contact`) 등 다른 과제는 기존 status를 그대로 둔다(A/B는 기능 켬/끔).

replayable.value는 코어가 다음 경우 false로 고정한다.
- `replayable.layer == "hardware"`
- incident.code가 전기·펌웨어 코드
- 궤적 소스(role이 incident_run 또는 recording)가 없음
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .artifacts import sha256_file
from .profile import ProfileError, expand_path
from .schema import validate_scenario

STATUS_PRIORITY = ("after_fix_regression", "not_reproducible_in_model", "stale", "active")
HARDWARE_CODES = frozenset(
    {"SERVO_THERMAL_LATCH", "OVERLOAD_TRIP", "USB_DEADLOCK", "BUS_DEATH", "I2C_BUS_DEATH"}
)
TRAJECTORY_ROLES = frozenset({"incident_run", "recording"})
STATUS_RULE_TASKS = frozenset({"drive_kinematic"})


def enforce_replayable(doc: dict[str, Any]) -> dict[str, Any]:
    replayable = dict(doc["replayable"])
    reasons = []
    if replayable.get("layer") == "hardware":
        reasons.append("layer=hardware")
    code = (doc.get("incident") or {}).get("code")
    if code in HARDWARE_CODES:
        reasons.append(f"전기·펌웨어 코드 {code}")
    if not any(s.get("role") in TRAJECTORY_ROLES for s in doc.get("sources", [])):
        reasons.append("궤적 없음")
    if reasons:
        replayable["value"] = False
        replayable["reason"] = "코어 고정: " + ", ".join(reasons)
    doc["replayable"] = replayable
    return doc


def load_scenario(path: Path) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_scenario(doc)
    return enforce_replayable(doc)


def list_scenarios(scenarios_dir: Path) -> list[dict[str, Any]]:
    if not scenarios_dir.is_dir():
        return []
    return [load_scenario(p) for p in sorted(scenarios_dir.glob("*/scenario.json"))]


def verify_sources(doc: dict[str, Any], base_dir: Path | None = None) -> list[dict[str, Any]]:
    results = []
    for source in doc.get("sources", []):
        entry = {"path": source.get("path"), "expected": source.get("sha256"), "ok": False}
        try:
            path = expand_path(str(source.get("path")), base_dir)
        except ProfileError as error:
            entry["reason"] = str(error)
            results.append(entry)
            continue
        if not path.is_file():
            entry["reason"] = "파일 없음"
        else:
            actual = sha256_file(path)
            entry["actual"] = actual
            entry["ok"] = actual == source.get("sha256")
            if not entry["ok"]:
                entry["reason"] = "SHA-256 불일치"
        results.append(entry)
    return results


def resolve_status(candidates: Iterable[str]) -> tuple[str, str | None]:
    found = set(candidates) or {"active"}
    unknown = found - set(STATUS_PRIORITY)
    if unknown:
        raise ValueError(f"알 수 없는 status: {sorted(unknown)}")
    status = next(s for s in STATUS_PRIORITY if s in found)
    model_note = (
        "not_reproducible_in_model"
        if "not_reproducible_in_model" in found and status != "not_reproducible_in_model"
        else None
    )
    return status, model_note


def _cells(result: dict[str, Any], name: str) -> dict[tuple[Any, Any], dict[str, Any]]:
    cond = result.get("conditions", {}).get(name) or {}
    return {
        (c["x_mm"], c["y_mm"]): c for c in cond.get("cells", []) if c.get("status") == "ok"
    }


def status_candidates(task: str, regress_result: dict[str, Any]) -> set[str]:
    if task not in STATUS_RULE_TASKS:
        return set()
    before = _cells(regress_result, "A")
    after = _cells(regress_result, "B")
    candidates = set()
    reproduced = any(c.get("verdict") == "fail" for c in before.values())
    if not reproduced:
        candidates.add("not_reproducible_in_model")
    if any(
        cell.get("verdict") == "pass" and after.get(key, {}).get("verdict") == "fail"
        for key, cell in before.items()
    ):
        candidates.add("after_fix_regression")
    if reproduced and after and not any(c.get("verdict") == "fail" for c in after.values()):
        candidates.add("stale")
    return candidates or {"active"}


def apply_status(doc: dict[str, Any], regress_result: dict[str, Any]) -> dict[str, Any]:
    """drive_kinematic이면 regress 결과로 status·model_note를 정하고, 아니면 그대로 둔다."""
    if doc.get("task") not in STATUS_RULE_TASKS:
        return doc
    status, note = resolve_status(status_candidates(doc["task"], regress_result))
    updated = dict(doc)
    updated["status"] = status
    updated["model_note"] = note
    return updated


def is_public(doc: dict[str, Any], exclude_status: Iterable[str]) -> bool:
    return doc.get("status") not in set(exclude_status)
