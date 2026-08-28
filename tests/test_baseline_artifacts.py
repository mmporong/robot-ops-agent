from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
DATASET = PROJECT_ROOT / "evaluations" / "datasets" / "robot_diagnostics_dev_v0.1.0.json"
BASELINE = (
    PROJECT_ROOT
    / "evaluations"
    / "baselines"
    / "2026-08-28_x86_64_cpu_qwen3_0.6b_q8.json"
)
EVIDENCE = (
    PROJECT_ROOT
    / "evaluations"
    / "evidence"
    / "2026-08-28_qwen3_0.6b_q8_cpu_dev_raw.json"
)
RETRIEVAL_EVIDENCE = (
    PROJECT_ROOT
    / "evaluations"
    / "evidence"
    / "2026-08-28_keyword_dev_top5_raw.json"
)

from robot_ops.model_benchmark import _percentile, score_answer  # noqa: E402


class BaselineArtifactTest(unittest.TestCase):
    def test_baseline_is_bound_to_current_dataset_and_keeps_edge_claim_closed(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        dataset_hash = hashlib.sha256(DATASET.read_bytes()).hexdigest()

        self.assertEqual(
            baseline["retrieval_development_set"]["dataset_sha256"],
            dataset_hash,
        )
        self.assertEqual(baseline["status"], "development_baseline")
        self.assertTrue(
            baseline["edge_devices"]["raspberry_pi"].startswith("not_measured")
        )
        self.assertTrue(baseline["edge_devices"]["jetson"].startswith("not_measured"))
        self.assertIn("not_verified", baseline["network_isolation"]["os_level_block"])

    def test_retrieval_claims_recompute_from_tracked_ranked_hits(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        evidence = json.loads(RETRIEVAL_EVIDENCE.read_text(encoding="utf-8"))
        measured = baseline["retrieval_development_set"]

        self.assertEqual(evidence["dataset_sha256"], measured["dataset_sha256"])
        self.assertEqual(evidence["case_count"], measured["case_count"])
        self.assertEqual(evidence["k"], measured["k"])

        reciprocal_ranks = []
        recalls = []
        source_precisions = []
        chunk_precisions = []
        for case in evidence["cases"]:
            hits = case["top_hits"]
            self.assertEqual(
                [hit["rank"] for hit in hits],
                list(range(1, len(hits) + 1)),
            )
            self.assertEqual(
                case["top_paths"],
                [hit["path"] for hit in hits],
            )
            expected = set(case["expected_paths"])
            relevant_ranks = [
                hit["rank"] for hit in hits if hit["path"] in expected
            ]
            reciprocal_ranks.append(
                1.0 / min(relevant_ranks) if relevant_ranks else 0.0
            )
            retrieved_expected = {hit["path"] for hit in hits} & expected
            recalls.append(len(retrieved_expected) / len(expected))
            unique_paths = {hit["path"] for hit in hits}
            source_precisions.append(
                len(retrieved_expected) / len(unique_paths) if unique_paths else 0.0
            )
            chunk_precisions.append(
                sum(hit["path"] in expected for hit in hits) / evidence["k"]
            )

        case_count = evidence["case_count"]
        self.assertAlmostEqual(
            measured["hit_rate_at_5"],
            sum(rank > 0.0 for rank in reciprocal_ranks) / case_count,
        )
        self.assertAlmostEqual(measured["recall_at_5"], sum(recalls) / case_count)
        self.assertAlmostEqual(measured["mrr"], sum(reciprocal_ranks) / case_count)
        self.assertAlmostEqual(
            measured["source_precision_at_5"],
            sum(source_precisions) / case_count,
        )
        self.assertAlmostEqual(
            measured["chunk_precision_at_5"],
            sum(chunk_precisions) / case_count,
        )

    def test_generation_claims_recompute_from_tracked_raw_answers(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        fixtures = {case["id"]: case for case in dataset["cases"]}

        recomputed = []
        for case in evidence["cases"]:
            fixture = fixtures[case["id"]]
            quality = score_answer(
                case["answer"],
                fixture["answer_rubric"],
                retrieved_paths=case["retrieved_paths"],
                expected_paths=fixture["expected_paths"],
            )
            self.assertEqual(quality, case["quality"])
            recomputed.append(quality)

        measured = baseline["generation_development_set"]
        concept_recalls = [item["concept_recall"] for item in recomputed]
        self.assertAlmostEqual(
            measured["required_concept_recall"],
            sum(concept_recalls) / len(concept_recalls),
        )
        self.assertAlmostEqual(
            measured["rubric_case_accuracy"],
            sum(item["rubric_pass"] for item in recomputed) / len(recomputed),
        )
        self.assertAlmostEqual(
            measured["citation_precision"],
            sum(item["citation_precision"] for item in recomputed) / len(recomputed),
        )
        first_runs = [case for case in evidence["cases"] if case["round"] == 1]
        first_latencies = [case["wall_latency_ms"] for case in first_runs]
        self.assertAlmostEqual(
            measured["first_run_wall_latency_ms_p50"],
            _percentile(first_latencies, 0.5),
        )
        self.assertAlmostEqual(
            measured["first_run_wall_latency_ms_p95"],
            _percentile(first_latencies, 0.95),
        )
        self.assertAlmostEqual(
            measured["max_server_rss_mib"],
            max(case["max_rss_kib"] for case in evidence["cases"]) / 1024.0,
        )


if __name__ == "__main__":
    unittest.main()
