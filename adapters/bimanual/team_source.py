"""팀 저장소에서 허용된 소스 후보를 안전하게 선택한다."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable


def _candidate_paths(candidates: Iterable[str]) -> tuple[str, ...]:
    checked = []
    for value in candidates:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != value:
            raise ValueError(f"잘못된 팀 파일 후보: {value}")
        checked.append(value)
    if not checked:
        raise ValueError("팀 파일 후보가 비어 있습니다")
    return tuple(checked)


def resolve_team_file(repo: Path, candidates: Iterable[str]) -> tuple[Path, str]:
    """현재 worktree에서 첫 일반 파일 후보를 고르되 symlink는 전부 거부한다."""
    root = repo.resolve(strict=True)
    checked = _candidate_paths(candidates)
    for candidate in checked:
        selected = root
        missing = False
        for part in PurePosixPath(candidate).parts:
            selected /= part
            if selected.is_symlink():
                raise ValueError(f"팀 파일 후보의 심볼릭 링크를 거부합니다: {candidate}")
            if not selected.exists():
                missing = True
                break
        if missing:
            continue
        if not selected.is_file():
            raise ValueError(f"팀 파일 후보가 일반 파일이 아닙니다: {candidate}")
        return selected, candidate
    raise FileNotFoundError(f"팀 파일 후보를 찾지 못했습니다: {', '.join(checked)}")


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def snapshot_git_file(
    repo: Path,
    rev: str,
    candidates: Iterable[str],
    destination: Path,
) -> tuple[Path, str, str]:
    """해석된 exact commit tree에서 첫 일반 blob 후보를 destination에 복사한다."""
    checked = _candidate_paths(candidates)
    commit = _git(repo, ["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"])
    commit_hex = commit.stdout.decode("ascii").strip()

    for candidate in checked:
        tree = _git(repo, ["ls-tree", "-z", commit_hex, "--", candidate]).stdout
        if not tree:
            continue
        metadata, found = tree[:-1].split(b"\t", 1)
        mode, kind, _object_id = metadata.split(b" ", 2)
        if found.decode("utf-8", "surrogateescape") != candidate:
            raise ValueError(f"git tree 경로가 후보와 다릅니다: {candidate}")
        if mode == b"120000":
            raise ValueError(f"git tree의 심볼릭 링크 후보를 거부합니다: {candidate}")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"git tree 후보가 일반 파일이 아닙니다: {candidate}")
        source = _git(repo, ["cat-file", "blob", f"{commit_hex}:{candidate}"]).stdout
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source)
        return destination, commit_hex, candidate

    raise FileNotFoundError(
        f"commit {commit_hex[:12]}에서 팀 파일 후보를 찾지 못했습니다: {', '.join(checked)}"
    )
