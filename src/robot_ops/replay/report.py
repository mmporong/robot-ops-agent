"""`robot-ops report build` — 정적 리포트 데이터(JSON) 굽기.

- 기본은 공개 빌드: profile `[public] exclude_status`(after_fix_regression)에 해당하는
  시나리오는 카드·regress 데이터·개수까지 전부 뺀다(존재 자체를 드러내지 않는다).
- `--private`일 때만 전체 포함(로컬 확인용, 배포 금지).
- 이전 빌드 잔재(비공개 빌드의 JSON)가 공개 빌드에 남지 않도록 `data/`를 비우고 다시 쓴다.
- 화면 자산(index.html·app.js·style.css)과 미디어 생성은 M4에서 추가한다.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .profile import Profile, display_path
from .scenario_bank import STATUS_RULE_TASKS, is_public, list_scenarios

REPORT_SCHEMA = "robot-ops-report/1"


def _card(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": doc["scenario_id"],
        "title_ko": doc["title_ko"],
        "origin": doc["origin"],
        "judgement_basis": doc["judgement_basis"],
        "assembly": doc["assembly"],
        "task": doc["task"],
        "status": doc["status"],
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


def latest_regress_results(artifacts_root: Path) -> dict[str, dict[str, Any]]:
    """시나리오별 가장 최근(created_at 기준) regress_result.json."""
    latest: dict[str, dict[str, Any]] = {}
    runs = artifacts_root / "runs"
    if not runs.is_dir():
        return latest
    for path in sorted(runs.glob("*/regress_result.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = doc.get("scenario_id")
        if not isinstance(sid, str):
            continue
        current = latest.get(sid)
        if current is None or str(doc.get("created_at", "")) >= str(current.get("created_at", "")):
            latest[sid] = doc
    return latest


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _reset_data_dir(data_dir: Path) -> None:
    if data_dir.is_symlink():
        raise RuntimeError(f"리포트 data 폴더가 심링크라 비우지 않습니다: {data_dir}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)


def build(
    profile: Profile,
    *,
    private: bool = False,
    out_dir: Path | None = None,
    scenarios_dir: Path | None = None,
) -> dict[str, Any]:
    out = out_dir or profile.report_dir
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

    data_dir = out / "data"
    _reset_data_dir(data_dir)
    cards = [_card(s) for s in scenarios]
    _write_json(data_dir / "scenarios.json", cards)
    for sid, doc in sorted(regress.items()):
        _write_json(data_dir / "regress" / f"{sid}.json", doc)
    summary = {
        "schema": REPORT_SCHEMA,
        "build": "private" if private else "public",
        "profile": profile.id,
        "title_ko": profile.project.get("title_ko"),
        "scenario_count": len(cards),
        "regress_count": len(regress),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _write_json(data_dir / "build.json", summary)
    return {**summary, "out_dir": display_path(out)}
