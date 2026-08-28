from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.config import IndexSettings  # noqa: E402
from robot_ops.indexer import _secure_database_path, index_stats, sync_index  # noqa: E402


class CountingEmbedder:
    def __init__(self, *, fail_token: str | None = None) -> None:
        self.calls = 0
        self.fail_token = fail_token

    @property
    def identifier(self) -> str:
        return "counting-v1:4"

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail_token and self.fail_token in text:
            raise RuntimeError("의도한 테스트 실패")
        return [float(len(text)), 1.0, 0.0, -1.0]


class BatchCountingEmbedder:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    @property
    def identifier(self) -> str:
        return "batch-counting-v1:4"

    def embed(self, text: str) -> list[float]:
        raise AssertionError("배치 경로를 사용해야 합니다")

    def embed_many(self, texts: tuple[str, ...] | list[str]) -> list[list[float]]:
        self.batches.append(tuple(texts))
        return [[float(len(text)), 1.0, 0.0, -1.0] for text in texts]


class IndexerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.include = self.root / "01_wiki"
        self.include.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.settings = IndexSettings(
            root=self.root,
            db_path=self.db_path,
            include_dirs=("01_wiki",),
            chunk_size=32,
            chunk_overlap=4,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, name: str, content: str) -> Path:
        path = self.include / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _indexed_text(self, relative_path: str) -> str:
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute(
                """
                SELECT chunks.text
                FROM chunks
                JOIN documents ON documents.id = chunks.document_id
                WHERE documents.path = ?
                ORDER BY chunks.idx
                """,
                (relative_path,),
            ).fetchall()
            return "".join(row[0] for row in rows)
        finally:
            con.close()

    def test_repeated_incremental_run_does_not_reembed(self) -> None:
        self._write("tf.md", "TF 트리 연결 상태를 확인한다.")
        embedder = CountingEmbedder()

        first = sync_index(self.settings, embedder)
        calls_after_first = embedder.calls
        second = sync_index(self.settings, embedder)

        self.assertEqual(first.added, 1)
        self.assertGreater(calls_after_first, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(embedder.calls, calls_after_first)

    def test_document_chunks_use_one_batch_embedding_call(self) -> None:
        self._write("long.md", "로봇 상태 진단 기록 " * 20)
        embedder = BatchCountingEmbedder()

        report = sync_index(self.settings, embedder)

        self.assertEqual(report.added, 1)
        self.assertEqual(len(embedder.batches), 1)
        self.assertGreater(len(embedder.batches[0]), 1)
        self.assertEqual(report.embedded_chunks, len(embedder.batches[0]))

    def test_touch_without_content_change_updates_metadata_without_embedding(self) -> None:
        path = self._write("odom.md", "바퀴 오도메트리와 실측을 비교한다.")
        embedder = CountingEmbedder()
        sync_index(self.settings, embedder)
        calls_after_first = embedder.calls

        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        report = sync_index(self.settings, embedder)

        self.assertEqual(report.skipped, 1)
        self.assertEqual(embedder.calls, calls_after_first)

    def test_modified_deleted_and_renamed_files_are_reflected(self) -> None:
        old_path = self._write("old.md", "이전 진단 기록")
        embedder = CountingEmbedder()
        sync_index(self.settings, embedder)

        old_path.unlink()
        self._write("renamed.md", "수정된 진단 기록")
        report = sync_index(self.settings, embedder)
        stats = index_stats(self.settings)

        self.assertEqual(report.deleted, 1)
        self.assertEqual(report.added, 1)
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(self._indexed_text("01_wiki/old.md"), "")
        self.assertIn("수정된 진단 기록", self._indexed_text("01_wiki/renamed.md"))

    def test_embedding_failure_preserves_previous_document(self) -> None:
        path = self._write("safety.md", "기존 안전 진단")
        initial_embedder = CountingEmbedder()
        sync_index(self.settings, initial_embedder)

        path.write_text("FAIL 새 안전 진단", encoding="utf-8")
        failing_embedder = CountingEmbedder(fail_token="FAIL")
        report = sync_index(self.settings, failing_embedder)

        self.assertEqual(report.failed, 1)
        self.assertIn("기존 안전 진단", self._indexed_text("01_wiki/safety.md"))
        self.assertNotIn("FAIL", self._indexed_text("01_wiki/safety.md"))

    def test_sensitive_paths_and_contents_are_not_indexed(self) -> None:
        self._write("credentials.md", "평문 자격증명 문서")
        self._write("private.md", "-----BEGIN PRIVATE KEY-----\nnot-a-real-key")
        self._write("safe.md", "공개 가능한 진단 문서")
        embedder = CountingEmbedder()

        report = sync_index(self.settings, embedder)
        stats = index_stats(self.settings)

        self.assertEqual(report.sensitive_skipped, 2)
        self.assertEqual(stats["documents"], 1)
        self.assertIn("공개 가능한 진단 문서", self._indexed_text("01_wiki/safe.md"))

    def test_excluded_directories_are_ignored(self) -> None:
        self._write(".git/leak.md", "인덱싱하면 안 되는 문서")
        self._write("visible.md", "인덱싱할 문서")
        embedder = CountingEmbedder()

        report = sync_index(self.settings, embedder)

        self.assertEqual(report.discovered, 1)
        self.assertEqual(report.added, 1)

    def test_partial_include_does_not_prune_other_scope(self) -> None:
        second = self.root / "second"
        second.mkdir()
        self._write("first.md", "첫 범위")
        (second / "second.md").write_text("둘째 범위", encoding="utf-8")
        embedder = CountingEmbedder()
        combined = IndexSettings(
            root=self.root,
            db_path=self.db_path,
            include_dirs=("01_wiki", "second"),
            chunk_size=32,
            chunk_overlap=4,
        )
        sync_index(combined, embedder)

        report = sync_index(self.settings, embedder)

        self.assertEqual(report.deleted, 0)
        self.assertEqual(index_stats(self.settings)["documents"], 2)

    def test_missing_include_preserves_existing_index(self) -> None:
        self._write("safe.md", "보존할 내용")
        embedder = CountingEmbedder()
        sync_index(self.settings, embedder)
        self.include.rename(self.root / "offline")

        with self.assertRaisesRegex(ValueError, "기존 인덱스를 보존"):
            sync_index(self.settings, embedder)

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        finally:
            con.close()

    def test_symlink_to_excluded_path_is_not_indexed(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir()
        target = ssh_dir / "notes.md"
        target.write_text("비밀 경로", encoding="utf-8")
        (self.include / "link.md").symlink_to(target)
        self._write("visible.md", "공개 경로")

        report = sync_index(self.settings, CountingEmbedder())

        self.assertEqual(report.discovered, 1)
        self.assertEqual(index_stats(self.settings)["documents"], 1)

    def test_common_credentials_are_filtered(self) -> None:
        self._write("bearer.md", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        self._write("database.md", "DATABASE_URL=postgres://user:password@example/db")
        self._write("aws.md", "AWS_SECRET_ACCESS_KEY=not-a-real-secret-value")
        self._write("visible.md", "공개 문서")

        report = sync_index(self.settings, CountingEmbedder())

        self.assertEqual(report.sensitive_skipped, 3)
        self.assertEqual(index_stats(self.settings)["documents"], 1)

    def test_symlink_include_root_is_rejected(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (real / "safe.md").write_text("문서", encoding="utf-8")
        (self.root / "alias").symlink_to(real, target_is_directory=True)
        settings = IndexSettings(
            root=self.root,
            db_path=self.db_path,
            include_dirs=("alias",),
        )

        with self.assertRaisesRegex(ValueError, "symlink"):
            sync_index(settings, CountingEmbedder())

    def test_database_cannot_be_reused_with_a_different_source_root(self) -> None:
        self._write("safe.md", "첫 소스")
        sync_index(self.settings, CountingEmbedder())
        other_root = self.root / "other-root"
        (other_root / "docs").mkdir(parents=True)
        (other_root / "docs" / "other.md").write_text("둘째 소스", encoding="utf-8")
        other = IndexSettings(
            root=other_root,
            db_path=self.db_path,
            include_dirs=("docs",),
        )

        with self.assertRaisesRegex(ValueError, "소스 루트가 현재 설정과 다릅니다"):
            sync_index(other, CountingEmbedder())

        self.assertIn("첫 소스", self._indexed_text("01_wiki/safe.md"))

    def test_unrelated_sqlite_database_is_rejected_without_new_tables(self) -> None:
        self.db_path.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE user_data(value TEXT)")
        con.execute("INSERT INTO user_data VALUES('keep-me')")
        con.commit()
        con.close()
        os.chmod(self.db_path, 0o644)
        self._write("safe.md", "문서")

        with self.assertRaisesRegex(ValueError, "Robot Ops meta가 없습니다"):
            sync_index(self.settings, CountingEmbedder())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT value FROM user_data").fetchone()[0], "keep-me"
            )
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone()
            )
        finally:
            con.close()
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o644)

    def test_uppercase_markdown_extension_is_indexed(self) -> None:
        self._write("UPPER.MD", "대문자 확장자")

        report = sync_index(self.settings, CountingEmbedder())

        self.assertEqual(report.added, 1)

    def test_database_permissions_are_private(self) -> None:
        self._write("safe.md", "권한 확인")
        sync_index(self.settings, CountingEmbedder())

        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_database_parent_permissions_are_preserved(self) -> None:
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        settings = IndexSettings(
            root=self.root,
            db_path=shared / "index.db",
            include_dirs=("01_wiki",),
        )
        self._write("safe.md", "권한 확인")

        sync_index(settings, CountingEmbedder())

        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        self.assertEqual(settings.db_path.stat().st_mode & 0o777, 0o600)

    def test_new_database_file_is_private_before_sqlite_connects(self) -> None:
        shared = self.root / "shared-precreate"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        db_path = shared / "index.db"

        created = _secure_database_path(db_path)

        self.assertTrue(created)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    def test_database_directory_target_is_rejected_without_chmod(self) -> None:
        bad_target = self.root / "directory.db"
        bad_target.mkdir(mode=0o755)
        os.chmod(bad_target, 0o755)
        settings = IndexSettings(
            root=self.root,
            db_path=bad_target,
            include_dirs=("01_wiki",),
        )
        self._write("safe.md", "문서")

        with self.assertRaisesRegex(ValueError, "일반 파일이 아닙니다"):
            sync_index(settings, CountingEmbedder())

        self.assertEqual(bad_target.stat().st_mode & 0o777, 0o755)

    def test_new_database_uses_wal_mode(self) -> None:
        self._write("safe.md", "WAL 확인")
        sync_index(self.settings, CountingEmbedder())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            con.close()

    def test_stats_does_not_create_missing_database(self) -> None:
        missing = IndexSettings(
            root=self.root,
            db_path=self.root / "missing" / "index.db",
            include_dirs=("01_wiki",),
        )

        with self.assertRaisesRegex(ValueError, "DB가 없습니다"):
            index_stats(missing)

        self.assertFalse(missing.db_path.exists())

    def test_newer_schema_is_rejected_without_overwrite(self) -> None:
        self.db_path.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO meta VALUES('schema_version', '99')")
        con.commit()
        con.close()
        self._write("safe.md", "스키마 확인")

        with self.assertRaisesRegex(ValueError, "새로운 DB schema"):
            sync_index(self.settings, CountingEmbedder())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                "99",
            )
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
                ).fetchone()
            )
        finally:
            con.close()

    def test_module_cli_returns_nonzero_for_invalid_root(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "robot_ops",
                "update",
                "--root",
                str(self.root / "missing-root"),
            ],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)

    def test_failure_report_keeps_stage_and_error_type(self) -> None:
        self._write("failure.md", "FAIL content")

        report = sync_index(self.settings, CountingEmbedder(fail_token="FAIL"))

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0]["path"], "01_wiki/failure.md")
        self.assertEqual(report.failures[0]["stage"], "embedding")
        self.assertEqual(report.failures[0]["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
