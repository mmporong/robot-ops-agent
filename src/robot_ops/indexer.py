from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from array import array
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .config import IndexSettings
from .embedding import Embedder
from .secrets import contains_sensitive_text, path_is_sensitive


SCHEMA_VERSION = 2


@dataclass(slots=True)
class IndexReport:
    mode: str
    discovered: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    sensitive_skipped: int = 0
    failed: int = 0
    embedded_chunks: int = 0
    duration_seconds: float = 0.0
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    chunks: list[str] = []
    offset = 0
    while offset < len(stripped):
        end = min(offset + chunk_size, len(stripped))
        value = stripped[offset:end]
        if value.strip():
            chunks.append(value)
        if end == len(stripped):
            break
        offset = end - overlap
    return chunks


def vector_to_blob(values: Sequence[float]) -> bytes:
    return array("f", values).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


def _secure_database_path(db_path: Path) -> bool:
    parent_existed = db_path.parent.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(db_path.parent, 0o700)
    if db_path.exists():
        if not db_path.is_file():
            raise ValueError(f"DB 경로가 일반 파일이 아닙니다: {db_path}")
        return False
    try:
        file_descriptor = os.open(
            db_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        if not db_path.is_file():
            raise ValueError(f"DB 경로가 일반 파일이 아닙니다: {db_path}")
        return False
    else:
        os.close(file_descriptor)
        return True


def _validate_schema_version(
    con: sqlite3.Connection,
    source_root: Path | None,
    *,
    create_if_missing: bool,
) -> None:
    table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if table_exists is None:
        existing_tables = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if existing_tables:
            names = ",".join(str(row[0]) for row in existing_tables)
            raise ValueError(
                f"기존 SQLite DB에 Robot Ops meta가 없습니다: tables={names}"
            )
        if not create_if_missing or source_root is None:
            raise ValueError("Robot Ops 인덱스 DB 형식이 아닙니다")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.execute("INSERT INTO meta VALUES('source_root', ?)", (str(source_root),))
        return
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise ValueError("DB meta에 schema_version이 없습니다")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as error:
        raise ValueError("DB schema_version이 올바른 정수가 아닙니다") from error
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"현재 코드보다 새로운 DB schema입니다: db={version}, supported={SCHEMA_VERSION}"
        )
    if version < SCHEMA_VERSION:
        raise ValueError(
            f"DB migration이 필요합니다: db={version}, supported={SCHEMA_VERSION}"
        )

    root_row = con.execute("SELECT value FROM meta WHERE key = 'source_root'").fetchone()
    if root_row is None:
        raise ValueError("DB meta에 source_root가 없습니다")
    stored_root = Path(str(root_row[0])).expanduser().resolve()
    if source_root is not None and stored_root != source_root:
        raise ValueError(
            "인덱스 DB의 소스 루트가 현재 설정과 다릅니다: "
            f"db={stored_root}, current={source_root}"
        )


def _connect(db_path: Path, source_root: Path) -> sqlite3.Connection:
    _secure_database_path(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        _validate_schema_version(con, source_root, create_if_missing=True)
        con.commit()
        os.chmod(db_path, 0o600)
        con.execute("PRAGMA foreign_keys = ON")
        journal_mode = str(con.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if journal_mode.casefold() != "wal":
            raise ValueError(f"SQLite WAL 모드를 활성화하지 못했습니다: {journal_mode}")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                pipeline_id TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                chunk_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dim INTEGER NOT NULL,
                UNIQUE(document_id, idx)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
            """
        )
        con.commit()
        return con
    except Exception:
        con.close()
        raise


def _connect_readonly(
    db_path: Path, expected_source_root: Path | None = None
) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ValueError(f"인덱스 DB가 없습니다: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        _validate_schema_version(
            con, expected_source_root, create_if_missing=False
        )
    except sqlite3.Error as error:
        con.close()
        raise ValueError("Robot Ops 인덱스 DB 형식이 아닙니다") from error
    except ValueError:
        con.close()
        raise
    return con


def _is_excluded(path: Path, root: Path, settings: IndexSettings) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.lower() in settings.excluded_dir_names for part in relative_parts[:-1])


def discover_markdown_files(settings: IndexSettings) -> list[Path]:
    files: set[Path] = set()
    for include in settings.include_dirs:
        unresolved_start = settings.root / include
        current = unresolved_start
        while current != settings.root:
            if current.is_symlink():
                raise ValueError(f"include 경로에 symlink를 허용하지 않습니다: {include}")
            if settings.root not in current.parents:
                break
            current = current.parent
        start = unresolved_start.resolve()
        try:
            start.relative_to(settings.root)
        except ValueError:
            raise ValueError(f"include 경로가 소스 루트 밖입니다: {include}")
        if not start.exists():
            raise ValueError(f"include 경로가 없습니다. 기존 인덱스를 보존합니다: {include}")
        candidates: Iterable[Path]
        if start.is_file():
            candidates = (start,)
        else:
            candidates = start.rglob("*")
        for path in candidates:
            if (
                not path.is_file()
                or path.suffix.casefold() != ".md"
                or path.is_symlink()
                or path.absolute() != path.resolve()
            ):
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(settings.root)
            except ValueError:
                continue
            if _is_excluded(resolved, settings.root, settings):
                continue
            files.add(resolved)
    return sorted(files)


def _active_scope_contains(relative_path: str, include_dirs: Sequence[str]) -> bool:
    path = Path(relative_path)
    for include in include_dirs:
        scope = Path(include)
        if scope == Path("."):
            return True
        try:
            path.relative_to(scope)
            return True
        except ValueError:
            continue
    return False


def _load_known_documents(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["path"]: row for row in con.execute("SELECT * FROM documents")}


def _delete_paths(con: sqlite3.Connection, paths: Iterable[str]) -> int:
    count = 0
    with con:
        for relative_path in paths:
            cursor = con.execute("DELETE FROM documents WHERE path = ?", (relative_path,))
            count += cursor.rowcount
    return count


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_index(settings: IndexSettings, embedder: Embedder, *, force: bool = False) -> IndexReport:
    started = time.perf_counter()
    normalized = settings.normalized()
    report = IndexReport(mode="full" if force else "incremental")
    pipeline_id = f"{embedder.identifier}|{normalized.chunker_id}"
    candidates = discover_markdown_files(normalized)
    con = _connect(normalized.db_path, normalized.root)

    try:
        known = _load_known_documents(con)
        report.discovered = len(candidates)
        discovered_paths = {
            path.relative_to(normalized.root).as_posix()
            for path in candidates
        }
        prune_candidates = {
            path
            for path in known
            if _active_scope_contains(path, normalized.include_dirs)
        }
        report.deleted += _delete_paths(
            con, sorted(prune_candidates - discovered_paths)
        )
        known = _load_known_documents(con)

        for path in candidates:
            relative_path = path.relative_to(normalized.root).as_posix()

            if path_is_sensitive(path, normalized.root):
                report.sensitive_skipped += 1
                report.deleted += _delete_paths(con, (relative_path,))
                known.pop(relative_path, None)
                continue

            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                report.failed += 1
                report.failures.append(
                    {
                        "path": relative_path,
                        "stage": "read",
                        "error_type": "read_or_decode_error",
                    }
                )
                continue

            if contains_sensitive_text(text):
                report.sensitive_skipped += 1
                report.deleted += _delete_paths(con, (relative_path,))
                known.pop(relative_path, None)
                continue

            stat = path.stat()
            content_sha256 = hashlib.sha256(raw).hexdigest()
            previous = known.get(relative_path)
            if (
                not force
                and previous is not None
                and previous["sha256"] == content_sha256
                and previous["pipeline_id"] == pipeline_id
            ):
                with con:
                    con.execute(
                        "UPDATE documents SET mtime_ns = ?, size_bytes = ? WHERE path = ?",
                        (stat.st_mtime_ns, stat.st_size, relative_path),
                    )
                report.skipped += 1
                continue

            chunks = chunk_text(text, normalized.chunk_size, normalized.chunk_overlap)
            try:
                prepared_chunks = []
                for chunk_index, chunk in enumerate(chunks):
                    embedding = list(embedder.embed(chunk))
                    prepared_chunks.append(
                        (
                            chunk_index,
                            chunk,
                            hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                            vector_to_blob(embedding),
                            len(embedding),
                        )
                    )
            except Exception as error:
                report.failed += 1
                report.failures.append(
                    {
                        "path": relative_path,
                        "stage": "embedding",
                        "error_type": type(error).__name__,
                    }
                )
                continue

            existed = previous is not None
            with con:
                con.execute("DELETE FROM documents WHERE path = ?", (relative_path,))
                cursor = con.execute(
                    """
                    INSERT INTO documents(
                        path, mtime_ns, size_bytes, sha256, pipeline_id, indexed_at, chunk_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relative_path,
                        stat.st_mtime_ns,
                        stat.st_size,
                        content_sha256,
                        pipeline_id,
                        _utc_now(),
                        len(prepared_chunks),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("documents INSERT가 row id를 반환하지 않았습니다")
                document_id = cursor.lastrowid
                con.executemany(
                    """
                    INSERT INTO chunks(
                        document_id, idx, text, text_sha256, embedding, embedding_dim
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (document_id, *prepared_chunk)
                        for prepared_chunk in prepared_chunks
                    ),
                )

            report.embedded_chunks += len(prepared_chunks)
            if existed:
                report.updated += 1
            else:
                report.added += 1
            known[relative_path] = con.execute(
                "SELECT * FROM documents WHERE path = ?", (relative_path,)
            ).fetchone()
    finally:
        con.close()
        report.duration_seconds = round(time.perf_counter() - started, 6)

    return report


def index_stats(settings: IndexSettings) -> dict[str, object]:
    normalized = settings.normalized()
    con = _connect_readonly(normalized.db_path, normalized.root)
    try:
        documents = int(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        chunks = int(con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        by_top_level = [
            {"directory": row[0] or ".", "documents": int(row[1])}
            for row in con.execute(
                """
                SELECT
                    CASE
                        WHEN instr(path, '/') = 0 THEN '.'
                        ELSE substr(path, 1, instr(path, '/') - 1)
                    END AS directory,
                    COUNT(*)
                FROM documents
                GROUP BY directory
                ORDER BY COUNT(*) DESC, directory ASC
                """
            )
        ]
        pipeline_ids = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT pipeline_id FROM documents ORDER BY pipeline_id"
            )
        ]
        return {
            "db_path": str(normalized.db_path),
            "documents": documents,
            "chunks": chunks,
            "pipeline_ids": pipeline_ids,
            "by_top_level": by_top_level,
        }
    finally:
        con.close()


def dump_report(report: IndexReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
