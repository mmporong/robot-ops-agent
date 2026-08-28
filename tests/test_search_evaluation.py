from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.config import IndexSettings  # noqa: E402
from robot_ops.embedding import HashEmbedder  # noqa: E402
from robot_ops.evaluation import evaluate_retrieval  # noqa: E402
from robot_ops.indexer import sync_index  # noqa: E402
from robot_ops.search import keyword_search, tokenize, vector_search  # noqa: E402
from robot_ops.search import SearchHit  # noqa: E402


class SearchEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        source = self.root / "docs"
        source.mkdir()
        (source / "rotation.md").write_text(
            "제자리 회전을 위치 변화만 보는 검사기가 정지로 잘못 판단했다.",
            encoding="utf-8",
        )
        (source / "gripper.md").write_text(
            "로그 성공과 달리 큐브 실좌표는 바닥에 남아 파지 실패였다.",
            encoding="utf-8",
        )
        self.db_path = self.root / "index.db"
        self.settings = IndexSettings(
            root=self.root,
            db_path=self.db_path,
            include_dirs=("docs",),
            chunk_size=200,
            chunk_overlap=20,
        )
        self.embedder = HashEmbedder(32)
        sync_index(self.settings, self.embedder)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tokenizer_adds_korean_character_trigrams(self) -> None:
        tokens = tokenize("회전했다")
        self.assertIn("회전했다", tokens)
        self.assertIn("#회전했", tokens)

    def test_keyword_search_returns_relevant_document_first(self) -> None:
        hits = keyword_search(self.db_path, "제자리 회전을 정지로 오판", k=2)
        self.assertEqual(hits[0].path, "docs/rotation.md")

    def test_vector_search_rejects_mismatched_embedder(self) -> None:
        with self.assertRaisesRegex(ValueError, "임베더가 다릅니다"):
            vector_search(self.db_path, "회전", HashEmbedder(64), k=2)

    def test_vector_search_rejects_changed_dimension_under_same_identifier(self) -> None:
        identifier = self.embedder.identifier

        class ChangedDimensionEmbedder:
            @property
            def identifier(self) -> str:
                return identifier

            def embed(self, text: str) -> list[float]:
                return [1.0] * 16

        with self.assertRaisesRegex(ValueError, "인덱스 차원이 다릅니다"):
            vector_search(self.db_path, "회전", ChangedDimensionEmbedder(), k=2)

    def test_evaluation_records_hash_and_metrics(self) -> None:
        dataset_path = self.root / "dataset.json"
        dataset_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dataset_id": "fixture-v1",
                    "split": "development",
                    "cases": [
                        {
                            "id": "rotation",
                            "robot": "fixture",
                            "category": "diagnosis",
                            "question": "제자리 회전 정지 오판",
                            "expected_paths": ["docs/rotation.md"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = evaluate_retrieval(
            dataset_path,
            lambda query, k: keyword_search(self.db_path, query, k=k),
            k=1,
            method="keyword-test",
        )

        self.assertEqual(report["case_count"], 1)
        self.assertEqual(report["metrics"]["recall_at_1"], 1.0)
        self.assertEqual(report["metrics"]["hit_rate_at_1"], 1.0)
        self.assertEqual(len(report["dataset_sha256"]), 64)
        self.assertEqual(report["cases"][0]["top_hits"][0]["rank"], 1)
        self.assertEqual(report["cases"][0]["top_hits"][0]["path"], "docs/rotation.md")
        self.assertNotIn("text", report["cases"][0]["top_hits"][0])

    def test_recall_counts_all_expected_paths_not_only_one_hit(self) -> None:
        dataset_path = self.root / "multi-path-dataset.json"
        dataset_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dataset_id": "multi-path-v1",
                    "split": "development",
                    "cases": [
                        {
                            "id": "multi",
                            "robot": "fixture",
                            "category": "diagnosis",
                            "question": "두 근거",
                            "expected_paths": [
                                "docs/rotation.md",
                                "docs/gripper.md",
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        one_expected_hit = SearchHit(
            path="docs/rotation.md",
            chunk_index=0,
            score=1.0,
            text="근거",
            method="fixture",
        )

        report = evaluate_retrieval(
            dataset_path,
            lambda _query, _k: [one_expected_hit],
            k=2,
            method="fixture",
        )

        self.assertEqual(report["metrics"]["hit_rate_at_2"], 1.0)
        self.assertEqual(report["metrics"]["recall_at_2"], 0.5)
        self.assertEqual(report["metrics"]["source_precision_at_2"], 1.0)
        self.assertEqual(report["metrics"]["chunk_precision_at_2"], 0.5)
        self.assertEqual(report["cases"][0]["retrieved_expected_count"], 1)


if __name__ == "__main__":
    unittest.main()
