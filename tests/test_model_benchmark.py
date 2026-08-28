from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.model_benchmark import _percentile, score_answer  # noqa: E402


class ModelBenchmarkTest(unittest.TestCase):
    def test_score_answer_separates_concepts_and_citation_grounding(self) -> None:
        score = score_answer(
            "위치만 봐서 회전을 놓쳤다. [출처: docs/right.md] [출처: fake.md]",
            {"required_concepts": [["위치 변화", "위치만"], ["회전"]]},
            retrieved_paths=("docs/right.md", "docs/other.md"),
            expected_paths=("docs/right.md",),
        )

        self.assertTrue(score["rubric_pass"])
        self.assertEqual(score["concept_recall"], 1.0)
        self.assertEqual(score["citation_precision"], 0.5)
        self.assertEqual(score["citation_format_compliance"], 1.0)
        self.assertEqual(score["expected_source_citation_recall"], 1.0)

    def test_score_answer_accepts_path_label_but_marks_format_noncompliant(self) -> None:
        score = score_answer(
            "근거다. [출처: 경로: docs/right.md]",
            {"required_concepts": [["근거"]]},
            retrieved_paths=("docs/right.md",),
            expected_paths=("docs/right.md",),
        )

        self.assertEqual(score["citation_precision"], 1.0)
        self.assertEqual(score["expected_source_citation_recall"], 1.0)
        self.assertEqual(score["citation_format_compliance"], 0.0)

    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(_percentile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)
        self.assertEqual(_percentile([], 0.95), None)

    def test_score_answer_rejects_malformed_required_concepts(self) -> None:
        malformed = (
            {},
            {"required_concepts": "회전"},
            {"required_concepts": []},
            {"required_concepts": [[]]},
            {"required_concepts": [["회전", 123]]},
        )

        for rubric in malformed:
            with self.subTest(rubric=rubric):
                with self.assertRaisesRegex(ValueError, "required_concepts"):
                    score_answer(
                        "회전했다.",
                        rubric,
                        retrieved_paths=("docs/right.md",),
                        expected_paths=("docs/right.md",),
                    )


if __name__ == "__main__":
    unittest.main()
