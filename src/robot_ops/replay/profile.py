"""프로파일(`profiles/<id>.toml`) 로드와 검증.

경로 확장 규칙
- 선두 `~` 또는 `~/` → `Path.home()` (`~user` 형식은 거부)
- `$USER`, `${USER}`만 현재 사용자 이름으로 치환하고, 그 밖의 `$` 변수는 거부
- 상대경로는 프로젝트 루트(기본: 프로파일 파일이 든 디렉터리의 부모) 기준으로 절대경로화

(동사, 과제) → 인터프리터 규칙
- 동사는 `convert | run | judge` 셋뿐이다.
- `core`는 예약어로, 코어의 stdlib 인터프리터(`sys.executable`)를 뜻한다.
  `[interpreters]`에 `core`를 정의할 수 없다.
- `core`는 `judge`에만 지정할 수 있다. `convert`·`run`은 팀 모듈·서드파티를
  import하는 동사로 간주하므로 `core` 지정 시 로드가 실패한다.
- 과제 표에 `imports_team = true`가 있으면 그 과제는 `judge`까지 포함해 모든 동사에
  `core`를 쓸 수 없다.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ADAPTER_PROTOCOL, VERBS

CORE_INTERPRETER = "core"
TEAM_IMPORT_VERBS = frozenset({"convert", "run"})
ISOLATION_MODES = ("auto", "systemd", "pgid")

_USER_VAR = re.compile(r"\$\{USER\}|\$USER(?![A-Za-z0-9_])")


class ProfileError(ValueError):
    """프로파일이 규칙을 어겼을 때."""


def current_user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def expand_path(value: str, base_dir: Path | None = None) -> Path:
    """`~`·`$USER`만 확장한 절대경로를 돌려준다."""
    if not isinstance(value, str) or not value:
        raise ProfileError(f"경로 값이 비어 있거나 문자열이 아닙니다: {value!r}")
    text = value
    if text.startswith("~"):
        if text != "~" and not text.startswith("~/"):
            raise ProfileError(f"'~user' 형식은 지원하지 않습니다: {value}")
        text = str(Path.home()) + text[1:]
    text = _USER_VAR.sub(current_user(), text)
    if "$" in text:
        raise ProfileError(f"$USER 외 환경변수는 허용하지 않습니다: {value}")
    path = Path(text)
    if not path.is_absolute():
        if base_dir is None:
            raise ProfileError(f"확장 후 절대경로가 아닙니다: {value}")
        path = base_dir / path
    return Path(os.path.normpath(path))


def display_path(value: str | Path) -> str:
    """기록·리포트용 경로 정규화: 홈 → `~`, `/data/<user>` → `/data/$USER`."""
    text = str(value)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home) :]
    data_user = f"/data/{current_user()}"
    if text == data_user or text.startswith(data_user + "/"):
        return "/data/$USER" + text[len(data_user) :]
    return text


@dataclass(frozen=True)
class Profile:
    path: Path
    base_dir: Path
    project: dict[str, Any]
    adapter_entry: Path
    interpreters: dict[str, Path]
    tasks: dict[str, dict[str, str]]
    allowlist_scripts: tuple[str, ...]
    allowlist_imports: tuple[str, ...]
    limits: dict[str, Any]
    artifacts_root: Path
    report_dir: Path
    delete_patterns: tuple[str, ...]
    source_repo: Path | None
    mirror: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    public_exclude_status: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(self.project.get("id", self.path.stem))

    @property
    def lock_file(self) -> Path:
        return expand_path(str(self.limits.get("lock_file", "/tmp/robot-ops.lock")), self.base_dir)

    @property
    def isolation(self) -> str:
        return str(self.limits.get("isolation", "auto"))

    def interpreter_for(self, task: str, verb: str) -> list[str]:
        """(동사, 과제)에 매핑된 인터프리터 argv 앞부분."""
        if verb not in VERBS:
            raise ProfileError(f"지원하지 않는 동사입니다: {verb}")
        verbs = self.tasks.get(task)
        if verbs is None:
            raise ProfileError(f"프로파일에 없는 과제입니다: {task}")
        name = verbs.get(verb)
        if name is None:
            raise ProfileError(f"과제 {task}에 동사 {verb} 매핑이 없습니다")
        if name == CORE_INTERPRETER:
            return [sys.executable]
        return [str(self.interpreters[name])]


def _table(data: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        if required:
            raise ProfileError(f"[{key}] 표가 없습니다")
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"[{key}]는 표여야 합니다")
    return value


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProfileError(f"{where}는 문자열 목록이어야 합니다")
    return tuple(value)


def load_profile(path: str | Path, base_dir: Path | None = None) -> Profile:
    profile_path = Path(path).expanduser().resolve()
    with profile_path.open("rb") as handle:
        data = tomllib.load(handle)
    base = base_dir.resolve() if base_dir is not None else profile_path.parent.parent

    project = _table(data, "project")
    adapter = _table(data, "adapter")
    protocol = adapter.get("protocol")
    if protocol != ADAPTER_PROTOCOL:
        raise ProfileError(f"adapter.protocol은 {ADAPTER_PROTOCOL}여야 합니다: {protocol!r}")
    entry = adapter.get("entry")
    if not isinstance(entry, str):
        raise ProfileError("adapter.entry가 없습니다")

    interpreters_raw = _table(data, "interpreters")
    if CORE_INTERPRETER in interpreters_raw:
        raise ProfileError("'core'는 예약어라 [interpreters]에 정의할 수 없습니다")
    interpreters = {
        name: expand_path(str(value), base) for name, value in interpreters_raw.items()
    }

    tasks_raw = adapter.get("tasks", {})
    if not isinstance(tasks_raw, dict) or not tasks_raw:
        raise ProfileError("[adapter.tasks.<task>]가 하나 이상 필요합니다")
    tasks: dict[str, dict[str, str]] = {}
    for task, table in tasks_raw.items():
        if not isinstance(table, dict):
            raise ProfileError(f"[adapter.tasks.{task}]는 표여야 합니다")
        imports_team = bool(table.get("imports_team", False))
        mapping: dict[str, str] = {}
        for verb, name in table.items():
            if verb == "imports_team":
                continue
            if verb not in VERBS:
                raise ProfileError(f"과제 {task}: 지원하지 않는 동사 {verb} (convert|run|judge만)")
            if not isinstance(name, str):
                raise ProfileError(f"과제 {task}.{verb}: 인터프리터 이름은 문자열이어야 합니다")
            if name == CORE_INTERPRETER:
                if verb in TEAM_IMPORT_VERBS or imports_team:
                    raise ProfileError(
                        f"과제 {task}.{verb}는 팀 모듈을 import하므로 core를 지정할 수 없습니다"
                    )
            elif name not in interpreters:
                raise ProfileError(f"과제 {task}.{verb}: 정의되지 않은 인터프리터 {name}")
            mapping[verb] = name
        tasks[task] = mapping

    allowlist = adapter.get("allowlist", {})
    if not isinstance(allowlist, dict):
        raise ProfileError("[adapter.allowlist]는 표여야 합니다")

    limits = _table(data, "limits", required=False)
    isolation = limits.get("isolation", "auto")
    if isolation not in ISOLATION_MODES:
        raise ProfileError(f"limits.isolation은 {ISOLATION_MODES} 중 하나여야 합니다")

    artifacts = _table(data, "artifacts")
    if "root" not in artifacts:
        raise ProfileError("artifacts.root가 없습니다")
    public = _table(data, "public", required=False)
    source_repo = project.get("source_repo")

    return Profile(
        path=profile_path,
        base_dir=base,
        project=project,
        adapter_entry=expand_path(entry, base),
        interpreters=interpreters,
        tasks=tasks,
        allowlist_scripts=_str_list(allowlist.get("scripts"), "allowlist.scripts"),
        allowlist_imports=_str_list(allowlist.get("imports"), "allowlist.imports"),
        limits=limits,
        artifacts_root=expand_path(str(artifacts["root"]), base),
        report_dir=expand_path(str(artifacts.get("report_dir", ".local/report")), base),
        delete_patterns=_str_list(artifacts.get("delete_patterns"), "artifacts.delete_patterns"),
        source_repo=expand_path(source_repo, base) if isinstance(source_repo, str) else None,
        mirror=_table(data, "mirror", required=False),
        replay=_table(data, "replay", required=False),
        public_exclude_status=_str_list(public.get("exclude_status"), "public.exclude_status"),
        raw=data,
    )
