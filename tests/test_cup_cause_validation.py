from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops import cup_cause_experiment  # noqa: E402
from tests.test_cup_cause_experiment import (  # noqa: E402
    _config,
    _passing_experiment,
    _result,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ValidateExperimentEvidenceTest(unittest.TestCase):
    COMMIT = "c" * 40

    def _base_fixture(self, root: Path) -> tuple[dict, dict, dict, Path]:
        source_config = _config()
        source_path = root / "source/config/simulation/cup_contact_experiment.json"
        _write_json(source_path, source_config)
        source_sha = _sha256(source_path)
        tool_path = root / "source/simulation/simulate_cup_contact.py"
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_text("# fixture source\n", encoding="utf-8")
        tool_hashes = {"simulate_cup_contact.py": _sha256(tool_path)}

        case_result = _result()
        case_plan = {
            "config": copy.deepcopy(source_config),
            "tool_sha256": copy.deepcopy(tool_hashes),
            "source_config_sha256": source_sha,
        }
        case_path = root / "input-case/result.json"
        _write_json(case_path, case_result)
        _write_json(case_path.with_name("plan.json"), case_plan)

        base_plan = {
            "config": copy.deepcopy(source_config),
            "tool_sha256": copy.deepcopy(tool_hashes),
            "source_config_sha256": source_sha,
            "config_sha256": source_sha,
        }
        proposal = cup_cause_experiment.diagnose(case_result, source_config)
        experiment = {
            "source_commit": self.COMMIT,
            "case_result_sha256": _sha256(case_path),
            "case_plan_sha256": _sha256(case_path.with_name("plan.json")),
            "proposal": proposal,
            "core": {"original": ["core_original_0"]},
            "regression": [],
            "runs": [],
        }
        return experiment, case_result, base_plan, case_path

    def _git_result(self, argv, **_kwargs):
        stdout = self.COMMIT + "\n" if "rev-parse" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def _validate_early(self, root: Path, experiment: dict, result: dict, plan: dict) -> None:
        with (
            mock.patch.object(
                cup_cause_experiment,
                "_load_run",
                return_value=(result, plan, root / "unused-run"),
            ),
            mock.patch.object(
                cup_cause_experiment.subprocess,
                "run",
                side_effect=self._git_result,
            ),
        ):
            cup_cause_experiment.validate_experiment(root, experiment, root / "mcp-audit.jsonl")

    def test_rejects_case_when_recomputed_proposal_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment, result, plan, _ = self._base_fixture(root)
            experiment["proposal"] = copy.deepcopy(experiment["proposal"])
            experiment["proposal"]["correction_mm"][1] += 1.0

            with self.assertRaisesRegex(ValueError, "다시 계산한 제안"):
                self._validate_early(root, experiment, result, plan)

    def test_rejects_modified_case_result_or_plan_hash(self) -> None:
        for key in ("case_result_sha256", "case_plan_sha256"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                experiment, result, plan, _ = self._base_fixture(root)
                experiment[key] = "0" * 64

                with self.assertRaisesRegex(ValueError, "입력 사례의 원본 해시"):
                    self._validate_early(root, experiment, result, plan)

    def test_rejects_baseline_source_config_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment, result, plan, _ = self._base_fixture(root)
            plan["config_sha256"] = "1" * 64

            with self.assertRaisesRegex(ValueError, "기준 실행의 설정 해시"):
                self._validate_early(root, experiment, result, plan)

    def _deep_fixture(
        self, root: Path, *, forged: str
    ) -> tuple[dict, dict, dict, Path, dict]:
        experiment, base_result, base_plan, _ = self._base_fixture(root)
        source_config = copy.deepcopy(base_plan["config"])
        source_sha = base_plan["source_config_sha256"]
        proposal = experiment["proposal"]
        correction = list(proposal["correction_mm"])
        core_position = [
            source_config["cup_spawn_offset_m"][0] * 1000,
            source_config["cup_spawn_offset_m"][1] * 1000,
        ]

        protocol = _passing_experiment()
        expected_core = {
            "original": None,
            "sham": [0.0, 0.0],
            "corrected": correction,
            "opposite": [-value for value in correction],
        }
        entries = {entry["name"]: entry for entry in protocol["runs"]}
        for kind, names in protocol["core"].items():
            for name in names:
                entries[name].update(
                    position_mm=list(core_position),
                    correction_mm=copy.deepcopy(expected_core[kind]),
                    diagnosis=copy.deepcopy(proposal),
                )
        for cell in protocol["regression"]:
            position = [cell["x_mm"], cell["y_mm"]]
            entries[cell["baseline"]].update(
                position_mm=position,
                correction_mm=None,
                diagnosis=copy.deepcopy(proposal),
            )
            entries[cell["corrected"]].update(
                position_mm=position,
                correction_mm=copy.deepcopy(correction),
                diagnosis=copy.deepcopy(proposal),
            )

        target_name = "core_corrected_0"
        protocol["runs"] = [entries[target_name]] + [
            entry for entry in protocol["runs"] if entry["name"] != target_name
        ]
        experiment.update(protocol)
        experiment.update(
            source_commit=self.COMMIT,
            proposal=proposal,
            success=True,
        )

        target = root / "target-run"
        change = correction
        corrected_center = [
            source_config["cup_center_m"][0] + change[0] / 1000,
            source_config["cup_center_m"][1] + change[1] / 1000,
            source_config["cup_center_m"][2],
        ]
        effective_grid = [core_position[index] - change[index] for index in range(2)]
        corrected_config = copy.deepcopy(source_config)
        corrected_config["cup_center_m"] = corrected_center
        corrected_config["cup_spawn_offset_m"] = [
            effective_grid[0] / 1000,
            effective_grid[1] / 1000,
            source_config["cup_spawn_offset_m"][2],
        ]
        generated_path = target / "intervention_config.json"
        generated_doc = copy.deepcopy(source_config)
        generated_doc["cup_center_m"] = corrected_center
        _write_json(generated_path, generated_doc)
        generated_sha = _sha256(generated_path)

        request = {
            "grid_point": {"x_mm": core_position[0], "y_mm": core_position[1]},
            "params": {"recover": False, "plan_correction_mm": correction},
        }
        metadata = {
            "source_config_sha256": source_sha,
            "generated_config_sha256": "2" * 64 if forged == "generated" else generated_sha,
            "correction_mm": correction,
            "original_grid_point_mm": core_position,
            "effective_grid_point_mm": effective_grid,
            "original_planned_center_m": source_config["cup_center_m"],
            "corrected_planned_center_m": corrected_center,
            "actual_spawn_center_m": [
                source_config["cup_center_m"][0] + core_position[0] / 1000,
                source_config["cup_center_m"][1] + core_position[1] / 1000,
                source_config["cup_center_m"][2] + source_config["cup_spawn_offset_m"][2],
            ],
        }
        _write_json(target / "run.request.json", request)
        _write_json(target / "run.response.json", {"plan_intervention": metadata})
        target_plan = {
            "config": corrected_config,
            "tool_sha256": copy.deepcopy(base_plan["tool_sha256"]),
            "source_config_sha256": "3" * 64 if forged == "plan" else metadata["generated_config_sha256"],
        }
        return experiment, base_result, base_plan, target, target_plan

    def _validate_deep_hash(self, root: Path, forged: str) -> None:
        experiment, base_result, base_plan, target, target_plan = self._deep_fixture(
            root, forged=forged
        )
        base_name = experiment["core"]["original"][0]

        def load_run(_root: Path, name: str):
            if name == base_name:
                return base_result, base_plan, root / "base-run"
            if name == "core_corrected_0":
                return copy.deepcopy(base_result), target_plan, target
            raise AssertionError(f"해시 거부 전에 예상하지 않은 실행을 읽었습니다: {name}")

        case_analysis = {
            "runtime": {
                "hardware_accessed": False,
                "observation_source": "simulator_ground_truth_not_rgb",
            },
            "source": {
                "result_sha256": experiment["case_result_sha256"],
                "plan_sha256": experiment["case_plan_sha256"],
            },
        }

        def inspect(_path, _allowed, _audit, *, tool_name="analyze_simulation"):
            if tool_name == "analyze_cup_alignment":
                return {
                    "proposal": copy.deepcopy(experiment["proposal"]),
                    "source": copy.deepcopy(case_analysis["source"]),
                }
            return copy.deepcopy(case_analysis)

        with (
            mock.patch.object(cup_cause_experiment, "_load_run", side_effect=load_run),
            mock.patch.object(
                cup_cause_experiment.subprocess,
                "run",
                side_effect=self._git_result,
            ),
            mock.patch.object(
                cup_cause_experiment,
                "inspect_via_mcp",
                new=mock.AsyncMock(side_effect=inspect),
            ),
        ):
            cup_cause_experiment.validate_experiment(root, experiment, root / "mcp-audit.jsonl")

    def test_rejects_forged_generated_config_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "원천·생성·실행 설정 파일 해시"):
                self._validate_deep_hash(Path(tmp), "generated")

    def test_rejects_forged_plan_source_config_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "원천·생성·실행 설정 파일 해시"):
                self._validate_deep_hash(Path(tmp), "plan")


if __name__ == "__main__":
    unittest.main()
