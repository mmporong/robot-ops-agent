"""시나리오·궤적·어댑터 응답·regress 결과의 stdlib 검증기(`docs/replay-regress.md` 예시 기준)."""

from __future__ import annotations

import math
from typing import Any

SCENARIO_SCHEMA = "robot-ops-scenario/1"
TRAJECTORY_SCHEMA = "robot-ops-trajectory/1"
REGRESS_SCHEMA = "robot-ops-regress/1"

ORIGINS = ("sim_run", "real_recording", "fault_injection")
TASKS = ("cup_contact", "drive_kinematic", "lerobot_episode")
JUDGEMENT_BASES = (
    "rigid_proxy_lift",
    "oracle_recovery_sim_coordinates",
    "kinematic_drive",
    "isaac_drive",
    "physical",
)
ASSEMBLIES = ("left_arm_proxy", "bimanual_full", "single_arm_so101")
STATUSES = ("active", "stale", "after_fix_regression", "not_reproducible_in_model")
MODEL_NOTES = (None, "not_reproducible_in_model")
DETECTED_BY = ("verifier", "divergence", "record")
# incident_run·recording은 궤적이 있는 소스, failure_record는 궤적 없는 기록 문서
SOURCE_ROLES = ("incident_run", "recording", "failure_record")
RESPONSE_STATUSES = ("ok", "unsupported", "infra_error")
VERDICTS = ("pass", "fail")
CELL_STATUSES = ("ok", "infra", "timeout")


class SchemaError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


class _Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, doc: Any, keys: tuple[str, ...], where: str) -> bool:
        if not isinstance(doc, dict):
            self.errors.append(f"{where}: 객체여야 합니다")
            return False
        for key in keys:
            if key not in doc:
                self.errors.append(f"{where}.{key}: 필수 키가 없습니다")
        return True

    def enum(self, value: Any, allowed: tuple[Any, ...], where: str) -> None:
        if value not in allowed:
            self.errors.append(f"{where}: {value!r}는 {allowed} 중 하나여야 합니다")

    def type(self, value: Any, kind: type | tuple[type, ...], where: str, *, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
            self.errors.append(f"{where}: 형식이 {kind}가 아닙니다")

    def number(self, value: Any, where: str, *, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if not _is_number(value):
            self.errors.append(f"{where}: 유한한 숫자가 아닙니다")

    def done(self) -> None:
        if self.errors:
            raise SchemaError(self.errors)


SCENARIO_KEYS = (
    "schema",
    "scenario_id",
    "title_ko",
    "origin",
    "judgement_basis",
    "assembly",
    "adapter",
    "task",
    "replayable",
    "sources",
    "failure_record_id",
    "incident",
    "conditions",
    "grid",
    "verifiers",
    "limits",
    "status",
    "model_note",
    "provenance",
)


def validate_scenario(doc: Any) -> None:
    c = _Checker()
    if not c.require(doc, SCENARIO_KEYS, "scenario"):
        c.done()
    if doc.get("schema") != SCENARIO_SCHEMA:
        c.errors.append(f"scenario.schema: {SCENARIO_SCHEMA}여야 합니다")
    for key in ("scenario_id", "title_ko", "adapter"):
        c.type(doc.get(key), str, f"scenario.{key}")
    c.enum(doc.get("origin"), ORIGINS, "scenario.origin")
    c.enum(doc.get("judgement_basis"), JUDGEMENT_BASES, "scenario.judgement_basis")
    c.enum(doc.get("assembly"), ASSEMBLIES, "scenario.assembly")
    c.enum(doc.get("task"), TASKS, "scenario.task")
    c.enum(doc.get("status"), STATUSES, "scenario.status")
    c.enum(doc.get("model_note"), MODEL_NOTES, "scenario.model_note")
    c.type(doc.get("failure_record_id"), str, "scenario.failure_record_id", nullable=True)

    replayable = doc.get("replayable")
    if c.require(replayable, ("value", "layer", "reason"), "scenario.replayable"):
        c.type(replayable.get("value"), bool, "scenario.replayable.value")
        c.type(replayable.get("layer"), str, "scenario.replayable.layer")

    sources = doc.get("sources")
    if not isinstance(sources, list):
        c.errors.append("scenario.sources: 목록이어야 합니다")
    else:
        for i, source in enumerate(sources):
            where = f"scenario.sources[{i}]"
            if c.require(source, ("path", "sha256", "role"), where):
                c.type(source.get("path"), str, f"{where}.path")
                c.type(source.get("sha256"), str, f"{where}.sha256")
                c.enum(source.get("role"), SOURCE_ROLES, f"{where}.role")

    incident = doc.get("incident")
    if incident is not None and c.require(
        incident, ("t_start_s", "t_end_s", "code", "detected_by"), "scenario.incident"
    ):
        c.number(incident.get("t_start_s"), "scenario.incident.t_start_s", nullable=True)
        c.number(incident.get("t_end_s"), "scenario.incident.t_end_s", nullable=True)
        c.type(incident.get("code"), str, "scenario.incident.code")
        c.enum(incident.get("detected_by"), DETECTED_BY, "scenario.incident.detected_by")

    conditions = doc.get("conditions")
    if not isinstance(conditions, dict) or not conditions:
        c.errors.append("scenario.conditions: 비어 있지 않은 객체여야 합니다")
    else:
        for name, cond in conditions.items():
            where = f"scenario.conditions.{name}"
            if c.require(cond, ("label_ko", "params", "judgement_basis"), where):
                c.type(cond.get("params"), dict, f"{where}.params")
                c.enum(cond.get("judgement_basis"), JUDGEMENT_BASES, f"{where}.judgement_basis")

    grid = doc.get("grid")
    if c.require(
        grid,
        ("spawn_x_offset_mm", "spawn_y_offset_mm", "repeat_points", "repeats"),
        "scenario.grid",
    ):
        for axis in ("spawn_x_offset_mm", "spawn_y_offset_mm"):
            values = grid.get(axis)
            if not isinstance(values, list) or not values:
                c.errors.append(f"scenario.grid.{axis}: 비어 있지 않은 목록이어야 합니다")
            else:
                for v in values:
                    c.number(v, f"scenario.grid.{axis}[]")
        for key in ("repeat_points", "repeats"):
            value = grid.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                c.errors.append(f"scenario.grid.{key}: 0 이상 정수여야 합니다")

    verifiers = doc.get("verifiers")
    if not isinstance(verifiers, list) or not all(isinstance(v, str) for v in verifiers):
        c.errors.append("scenario.verifiers: 문자열 목록이어야 합니다")
    limits = doc.get("limits")
    if c.require(limits, ("timeout_s",), "scenario.limits"):
        c.number(limits.get("timeout_s"), "scenario.limits.timeout_s", nullable=True)
    c.type(doc.get("provenance"), dict, "scenario.provenance")
    c.done()


TRAJECTORY_HEADER_KEYS = (
    "schema",
    "robot_id",
    "arm",
    "joint_names",
    "units",
    "fps",
    "source",
    "origin",
)


def validate_trajectory_header(doc: Any) -> None:
    c = _Checker()
    if not c.require(doc, TRAJECTORY_HEADER_KEYS, "trajectory"):
        c.done()
    if doc.get("schema") != TRAJECTORY_SCHEMA:
        c.errors.append(f"trajectory.schema: {TRAJECTORY_SCHEMA}여야 합니다")
    if doc.get("units") != "rad":
        c.errors.append("trajectory.units: 'rad'여야 합니다")
    names = doc.get("joint_names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        c.errors.append("trajectory.joint_names: 비어 있지 않은 문자열 목록이어야 합니다")
    fps = doc.get("fps")
    if not _is_number(fps) or fps <= 0:
        c.errors.append("trajectory.fps: 양수여야 합니다")
    c.require(doc.get("source"), ("kind", "path", "episode", "sha256"), "trajectory.source")
    c.done()


def validate_trajectory_row(row: Any, n_joints: int, index: int) -> None:
    c = _Checker()
    where = f"trajectory.row[{index}]"
    if not c.require(row, ("t", "q_cmd", "q_obs"), where):
        c.done()
    c.number(row.get("t"), f"{where}.t")
    for key in ("q_cmd", "q_obs"):
        values = row.get(key)
        if not isinstance(values, list) or len(values) != n_joints:
            c.errors.append(f"{where}.{key}: 길이 {n_joints} 목록이어야 합니다")
        else:
            for v in values:
                c.number(v, f"{where}.{key}[]")
    for key in ("tcp_cmd_m", "tcp_obs_m"):
        values = row.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or len(values) != 3:
            c.errors.append(f"{where}.{key}: 길이 3 목록이어야 합니다")
        else:
            for v in values:
                c.number(v, f"{where}.{key}[]")
    c.done()


def validate_adapter_response(doc: Any, verb: str, protocol: str) -> None:
    """공통 응답 필드와, status=ok일 때 동사별 필수 필드를 검사한다."""
    c = _Checker()
    if not c.require(doc, ("protocol", "status"), "response"):
        c.done()
    if doc.get("protocol") != protocol:
        c.errors.append(f"response.protocol: {protocol}여야 합니다")
    c.enum(doc.get("status"), RESPONSE_STATUSES, "response.status")
    if doc.get("status") == "ok":
        if verb == "convert":
            c.require(doc, ("trajectory_path", "sha256", "fps", "n_frames"), "response")
        elif verb == "run":
            c.require(doc, ("run_dir", "result_path", "hardware_accessed"), "response")
        elif verb == "judge" and c.require(
            doc,
            ("verdict", "judgement_basis", "assembly", "checks", "failure_code"),
            "response",
        ):
            c.enum(doc.get("verdict"), VERDICTS, "response.verdict")
            checks = doc.get("checks")
            if not isinstance(checks, list):
                c.errors.append("response.checks: 목록이어야 합니다")
            else:
                for i, check in enumerate(checks):
                    c.require(
                        check,
                        ("id", "pass", "value", "threshold", "unit"),
                        f"response.checks[{i}]",
                    )
    c.done()


def validate_regress_result(doc: Any) -> None:
    c = _Checker()
    keys = ("schema", "regress_id", "profile", "scenario_id", "conditions", "determinism", "provenance")
    if not c.require(doc, keys, "regress"):
        c.done()
    if doc.get("schema") != REGRESS_SCHEMA:
        c.errors.append(f"regress.schema: {REGRESS_SCHEMA}여야 합니다")
    conditions = doc.get("conditions")
    if isinstance(conditions, dict):
        for name, cond in conditions.items():
            where = f"regress.conditions.{name}"
            if c.require(cond, ("judgement_basis", "cells", "pass", "valid", "invalid"), where):
                for i, cell in enumerate(cond.get("cells") or []):
                    cw = f"{where}.cells[{i}]"
                    if c.require(cell, ("x_mm", "y_mm", "status", "verdict", "run_id"), cw):
                        c.enum(cell.get("status"), CELL_STATUSES, f"{cw}.status")
                        if cell.get("status") == "ok":
                            c.enum(cell.get("verdict"), VERDICTS, f"{cw}.verdict")
    else:
        c.errors.append("regress.conditions: 객체여야 합니다")
    c.require(
        doc.get("determinism"),
        ("points", "repeats", "verdict_agreement", "max_metric_spread"),
        "regress.determinism",
    )
    c.done()
