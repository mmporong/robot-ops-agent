"""산출물 manifest·디스크 여유 확인·명시 패턴 삭제.

삭제는 다음을 모두 만족하는 파일만 허용한다. 하나라도 어기면 `DeletionRefused`.
1. 경로가 profile `delete_patterns` 중 하나와 맞는다(경로 조각 수가 같아야 하며 조각별 fnmatch).
2. run manifest에 기록돼 있고, 현재 크기·SHA-256이 기록과 같다.
3. realpath가 산출물 root의 realpath 아래에 있다.
4. root 아래의 대상·상위 경로 조각 중 심링크가 없다.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "robot-ops-run-manifest/1"


class DeletionRefused(PermissionError):
    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"외부 경로 삭제 거부: {path} ({reason})")
        self.path = str(path)
        self.reason = reason


class InsufficientDisk(RuntimeError):
    pass


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def free_disk_gb(path: Path) -> float:
    probe = Path(path)
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    return shutil.disk_usage(probe).free / (1024**3)


def check_free_disk(path: Path, min_free_gb: float) -> float:
    free = free_disk_gb(path)
    if free < min_free_gb:
        raise InsufficientDisk(f"{path} 여유 {free:.1f} GB < 최소 {min_free_gb} GB, 실행 거부")
    return free


def _iter_regular_files(run_dir: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(run_dir, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            if path.parent == run_dir and name == MANIFEST_NAME:
                continue
            yield path


def write_manifest(
    run_dir: Path,
    *,
    run_id: str,
    grid_offset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """run 폴더의 일반 파일(심링크 제외)을 상대경로·크기·SHA-256으로 기록한다."""
    files = []
    for path in _iter_regular_files(run_dir):
        files.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "deleted": False,
            }
        )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "grid_offset": grid_offset,
        "files": files,
    }
    _save_manifest(run_dir, manifest)
    return manifest


def load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def _save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    target = run_dir / MANIFEST_NAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def matches_pattern(rel_path: str, patterns: Iterable[str]) -> bool:
    parts = PurePosixPath(rel_path).parts
    for pattern in patterns:
        pattern_parts = PurePosixPath(pattern).parts
        if len(pattern_parts) != len(parts):
            continue
        if all(fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pattern_parts)):
            return True
    return False


def _check_no_symlink(root: Path, target: Path) -> None:
    """root(자체 포함)부터 target까지 경로 조각에 심링크가 없는지 확인."""
    if root.is_symlink():
        raise DeletionRefused(target, "산출물 root가 심링크")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise DeletionRefused(target, f"경로 조각이 심링크: {current}")


def delete_artifact(
    target: Path | str,
    *,
    root: Path,
    run_dir: Path,
    patterns: Iterable[str],
) -> dict[str, Any]:
    """규칙을 모두 통과한 파일 하나를 삭제하고 manifest에 deleted=true로 남긴다."""
    patterns = tuple(patterns)
    root_abs = Path(os.path.abspath(root))
    run_abs = Path(os.path.abspath(run_dir))
    target_abs = Path(os.path.abspath(target))

    try:
        target_abs.relative_to(root_abs)
        run_abs.relative_to(root_abs)
    except ValueError:
        raise DeletionRefused(target, "root 밖 경로") from None
    _check_no_symlink(root_abs, target_abs)
    root_real = Path(os.path.realpath(root_abs))
    target_real = Path(os.path.realpath(target_abs))
    if not target_real.is_relative_to(root_real):
        raise DeletionRefused(target, "realpath가 root 밖")
    try:
        rel = target_abs.relative_to(run_abs).as_posix()
    except ValueError:
        raise DeletionRefused(target, "run 폴더 밖 경로") from None
    if not matches_pattern(rel, patterns):
        raise DeletionRefused(target, "delete_patterns에 맞지 않음")

    manifest = load_manifest(run_abs)
    entry = next((f for f in manifest.get("files", []) if f.get("path") == rel), None)
    if entry is None:
        raise DeletionRefused(target, "manifest 미기록")
    if entry.get("deleted"):
        raise DeletionRefused(target, "이미 삭제됨")
    if not target_abs.is_file():
        raise DeletionRefused(target, "일반 파일이 아님")
    if target_abs.stat().st_size != entry.get("size") or sha256_file(target_abs) != entry.get("sha256"):
        raise DeletionRefused(target, "manifest 기록과 내용이 다름")

    target_abs.unlink()
    entry["deleted"] = True
    _save_manifest(run_abs, manifest)
    return entry


def delete_recorded_matches(run_dir: Path, *, root: Path, patterns: Iterable[str]) -> list[str]:
    """manifest에 기록된 파일 중 패턴에 맞는 것만 지운다(scene.usda 등 실행 직후 정리)."""
    patterns = tuple(patterns)
    deleted = []
    for entry in load_manifest(run_dir).get("files", []):
        rel = entry.get("path", "")
        if entry.get("deleted") or not matches_pattern(rel, patterns):
            continue
        delete_artifact(run_dir / rel, root=root, run_dir=run_dir, patterns=patterns)
        deleted.append(rel)
    return deleted
