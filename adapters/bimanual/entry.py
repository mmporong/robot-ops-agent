"""양팔 로봇 프로젝트 어댑터 진입점 (`robot-ops-adapter/1`).

호출: `<interp> entry.py <convert|run|judge> --task T --request req.json --response resp.json --workdir D`

- 응답 파일만 진실이다. stdout/stderr는 로그다.
- 동사·과제별 모듈은 지연 import한다. `judge`는 stdlib만 쓰므로 core 인터프리터
  (`python -S` 포함)에서 서드파티 모듈을 로드하지 않는다.
- 종료 코드: 0 ok, 2 unsupported, 3 infra_error. 그 밖은 crash로 취급된다.
- 팀 파일 allowlist: 요청의 `scripts`·`imports`가 아래 목록 밖이면 거부(unsupported).
  이 목록은 profiles/bimanual.toml `[adapter.allowlist]`와 같아야 하고, 각 run 모듈이 여는 팀 파일
  상수(`TEAM_SCRIPT`·`TEAM_FILE`)가 이 목록 안에 있어야 한다(tests/test_replay_profile.py가 확인).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True

PROTOCOL = "robot-ops-adapter/1"
VERBS = ("convert", "run", "judge")
EXIT = {"ok": 0, "unsupported": 2, "infra_error": 3}
TEAM_SCRIPTS = frozenset({"tools/simulate_cup_contact.py"})
TEAM_IMPORTS = frozenset({"tools/mobile_service_control.py"})

# (동사, 과제) → 모듈 이름. 모듈은 handle(verb, task, request, workdir) -> dict 를 제공한다.
HANDLERS = {
    ("convert", "lerobot_episode"): "convert_lerobot",
    ("run", "lerobot_episode"): "run_mujoco_episode",
    ("run", "drive_kinematic"): "run_drive_kinematic",
    ("run", "cup_contact"): "run_cup_contact",
    ("judge", "cup_contact"): "judge",
    ("judge", "drive_kinematic"): "judge",
}

HERE = Path(__file__).resolve().parent


class Unsupported(Exception):
    pass


def check_allowlist(request: dict) -> None:
    for key, allowed in (("scripts", TEAM_SCRIPTS), ("imports", TEAM_IMPORTS)):
        values = request.get(key, [])
        if not isinstance(values, list):
            raise Unsupported(f"request.{key}는 목록이어야 합니다")
        for value in values:
            path = PurePosixPath(str(value))
            if path.is_absolute() or ".." in path.parts or path.as_posix() not in allowed:
                raise Unsupported(f"allowlist 밖 {key} 요청 거부: {value}")


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dispatch(verb: str, task: str, request: dict, workdir: Path) -> dict:
    if verb not in VERBS:
        raise Unsupported(f"지원하지 않는 동사입니다: {verb} (convert|run|judge만)")
    check_allowlist(request)
    name = HANDLERS.get((verb, task))
    if name is None:
        raise Unsupported(f"이 어댑터는 ({verb}, {task})를 지원하지 않습니다")
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    module = importlib.import_module(name)
    return module.handle(verb, task, request, workdir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb")
    parser.add_argument("--task", required=True)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request는 JSON 객체여야 합니다")
        args.workdir.mkdir(parents=True, exist_ok=True)
        body = dispatch(args.verb, args.task, request, args.workdir.resolve())
        status = str(body.pop("status", "ok"))
    except Unsupported as error:
        status, body = "unsupported", {"error": str(error)}
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 infra_error 응답으로 남긴다
        traceback.print_exc()
        status, body = "infra_error", {"error": f"{type(error).__name__}: {error}"}
    _write(args.response, {"protocol": PROTOCOL, "status": status, **body})
    return EXIT.get(status, 3)


if __name__ == "__main__":
    raise SystemExit(main())
