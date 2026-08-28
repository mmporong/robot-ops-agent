from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_INCLUDE_DIRS = (
    "01_wiki",
    "02_MOC",
    "03_QA",
    "04_Prompts",
    "06_Ideas",
    "07_Career",
    "08_Portfolio",
    "09_Memo",
)

DEFAULT_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".obsidian",
        ".omc",
        ".omx",
        ".robot_ops",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".trash",
        "__pycache__",
        "credentials",
        "secrets",
        "node_modules",
    }
)


@dataclass(frozen=True, slots=True)
class IndexSettings:
    root: Path
    db_path: Path
    include_dirs: tuple[str, ...] = DEFAULT_INCLUDE_DIRS
    chunk_size: int = 800
    chunk_overlap: int = 120
    excluded_dir_names: frozenset[str] = DEFAULT_EXCLUDED_DIR_NAMES

    def normalized(self) -> "IndexSettings":
        root = self.root.expanduser().resolve()
        db_path = self.db_path.expanduser().resolve()
        include_dirs = tuple(dict.fromkeys(part.strip("/\\") or "." for part in self.include_dirs))

        if not root.is_dir():
            raise ValueError(f"소스 루트가 디렉터리가 아닙니다: {root}")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size는 1 이상이어야 합니다")
        if self.chunk_overlap < 0:
            raise ValueError("chunk_overlap은 0 이상이어야 합니다")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap은 chunk_size보다 작아야 합니다")

        return IndexSettings(
            root=root,
            db_path=db_path,
            include_dirs=include_dirs,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            excluded_dir_names=self.excluded_dir_names,
        )

    @property
    def chunker_id(self) -> str:
        return f"chars-v1:{self.chunk_size}:{self.chunk_overlap}"
