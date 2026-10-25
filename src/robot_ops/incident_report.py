"""사고 후보를 영상·궤적·검사 근거로 함께 검토하는 정적 리포트를 만든다."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .replay.artifacts import write_json


ASSETS = Path(__file__).parent / "replay" / "report_assets"


def validate_output_paths(out: Path, inputs: list[Path], *, names: tuple[str, ...] = (
    "index.html", "incident.json", "incident.js", "incident.css", "mcp-audit.jsonl", "media/real.mp4", "media/ghost.mp4",
)) -> None:
    """입력과 출력의 별칭 및 기존 출력 링크를 기록 전에 거부한다."""
    root = out.expanduser().absolute()
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ValueError("출력 폴더의 심볼릭 링크를 허용하지 않습니다")
    originals = [p.expanduser().absolute() for p in inputs]
    for name in names:
        target = root / name
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError("출력 파일의 심볼릭 링크를 허용하지 않습니다")
        if target.exists() and target.stat().st_nlink > 1:
            raise ValueError("다른 경로와 연결된 출력 파일을 덮어쓸 수 없습니다")
        if any(target.resolve() == p.resolve() or target.exists() and p.exists() and target.samefile(p) for p in originals):
            raise ValueError("입력 파일과 출력 파일 경로가 겹칩니다")


def _safe_media_path(value: object) -> str | None:
    """리포트 폴더 안의 상대 미디어 경로만 브라우저에 전달한다."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("media 경로는 비어 있지 않은 상대경로여야 합니다")
    raw = value.strip()
    if "\\" in raw or "\x00" in raw:
        raise ValueError("media 경로에는 역슬래시나 NUL을 사용할 수 없습니다")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("media 경로는 리포트 폴더 안의 정규화된 상대경로여야 합니다")
    if ":" in path.parts[0]:
        raise ValueError("media 경로에는 URL 스킴을 사용할 수 없습니다")
    return path.as_posix()


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError("data는 객체여야 합니다")
    result = dict(data)
    media = result.get("media")
    if media is None:
        media = {}
    if not isinstance(media, dict):
        raise TypeError("media는 객체여야 합니다")
    result["media"] = {
        **media,
        "real": _safe_media_path(media.get("real")),
        "ghost": _safe_media_path(media.get("ghost")),
    }
    return result


def write_report(data: dict[str, Any], out: Path) -> None:
    """검토 데이터와 오프라인 HTML 화면을 ``out``에 기록한다."""
    payload = _normalized(data)
    out = Path(out)
    validate_output_paths(out, [])
    out.mkdir(parents=True, exist_ok=True)
    for name in ("incident.js", "incident.css"):
        shutil.copy2(ASSETS / name, out / name)

    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", r"\u003c")
    page = f'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>로봇 사고 검토</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="incident.css">
<script src="incident.js" defer></script>
</head>
<body>
<main id="incident-app"></main>
<script type="application/json" id="incident-data">{encoded}</script>
<noscript>사고 검토 데이터는 incident.json에서 확인할 수 있습니다.</noscript>
</body>
</html>'''
    (out / "index.html").write_text(page, encoding="utf-8")
    write_json(out / "incident.json", payload)
