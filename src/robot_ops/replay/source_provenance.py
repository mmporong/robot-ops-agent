"""시뮬레이션 계획에 기록된 원천 파일 해시를 고정 소스와 대조한다."""
from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath

from .artifacts import sha256_file


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _safe_relative_path(value: object, *, key: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"원천 파일 {key}의 상대경로가 올바르지 않습니다")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"원천 파일 {key}의 상대경로가 올바르지 않습니다")
    if path.name != key:
        raise ValueError(f"원천 파일 {key}의 경로 basename이 해시 키와 다릅니다")
    return path


def _regular_file_without_symlinks(root: Path, relative: PurePosixPath, *, key: str) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("고정 소스 루트가 실제 디렉터리가 아닙니다")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"원천 파일 {key} 경로에 심링크가 포함되어 있습니다")
    try:
        mode = current.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"원천 파일 {key}을 고정 소스에서 찾을 수 없습니다") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"원천 파일 {key}이 일반 파일이 아닙니다")
    return current


def validate_tool_sources(source_root: Path, plan: dict) -> dict[str, str]:
    """계획의 도구 매니페스트를 검증하고 실제 사용한 저장소 상대경로를 반환한다."""
    hashes = plan.get("tool_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("실행 도구 해시는 비어 있지 않은 객체여야 합니다")
    for key, digest in hashes.items():
        if (not isinstance(key, str) or not key or key in (".", "..") or "\\" in key or "\0" in key
            or PurePosixPath(key).name != key
            or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None):
            raise ValueError("실행 도구 해시는 파일명 키와 64자리 소문자 SHA-256이어야 합니다")

    has_explicit = "tool_source_paths" in plan
    explicit = plan.get("tool_source_paths")
    if has_explicit:
        if not isinstance(explicit, dict) or not explicit or set(explicit) != set(hashes):
            raise ValueError("명시 원천 경로는 모든 실행 도구 해시 키와 정확히 대응해야 합니다")
        selected = {key: _safe_relative_path(explicit[key], key=key) for key in hashes}
    else:
        selected = {}
        for key in hashes:
            matches: list[PurePosixPath] = []
            for folder in ("tools", "simulation"):
                relative = PurePosixPath(folder, key)
                candidate = source_root / Path(*relative.parts)
                if candidate.exists() or candidate.is_symlink():
                    path = _regular_file_without_symlinks(source_root, relative, key=key)
                    if sha256_file(path) == hashes[key]:
                        matches.append(relative)
            if not matches:
                raise ValueError(f"실행 도구 {key} 해시가 고정 소스 파일과 다릅니다")
            selected[key] = matches[0]

    result: dict[str, str] = {}
    for key, relative in selected.items():
        path = _regular_file_without_symlinks(source_root, relative, key=key)
        if sha256_file(path) != hashes[key]:
            raise ValueError(f"실행 도구 {key} 해시가 고정 소스 파일과 다릅니다")
        result[key] = relative.as_posix()
    return result


def require_same_tool_manifest(plan: dict, baseline: dict) -> None:
    """두 실행이 해시와 명시 원천 경로까지 같은 소스를 사용했는지 확인한다."""
    if (plan.get("tool_sha256") != baseline.get("tool_sha256")
        or plan.get("tool_source_paths") != baseline.get("tool_source_paths")):
        raise ValueError("실행 도구 해시 또는 원천 경로가 기준 실행과 다릅니다")
