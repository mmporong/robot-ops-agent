from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = PROJECT_ROOT / "adapters" / "bimanual"
sys.path.insert(0, str(ADAPTER_DIR))

import run_cup_contact  # noqa: E402


class CupPlanInterventionTest(unittest.TestCase):
    SOURCE_CONFIG = {
        "schema": "cup_contact_experiment_v1",
        "cup_center_m": [0.39, 0.17, 0.78],
        "cup_spawn_offset_m": [0.0, 0.0, 0.004],
        "cup_mass_kg": 0.01,
        "minimum_contact_force_n": 0.02,
        "controller": {"gain": 12.0},
    }

    def _repo(self, root: Path, *, config_symlink: bool = False) -> tuple[Path, Path]:
        repo = root / "team"
        script = repo / "simulation" / "simulate_cup_contact.py"
        script.parent.mkdir(parents=True)
        script.write_text("# fake simulator\n", encoding="utf-8")
        config = repo / "config" / "simulation" / "cup_contact_experiment.json"
        config.parent.mkdir(parents=True)
        if config_symlink:
            target = root / "real-config.json"
            target.write_text(json.dumps(self.SOURCE_CONFIG), encoding="utf-8")
            config.symlink_to(target)
        else:
            config.write_text(
                json.dumps(self.SOURCE_CONFIG, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return repo, config

    @staticmethod
    def _mock_run(calls: list[list[str]]):
        def invoke(argv, *, cwd, env, stdout, stderr, check):
            calls.append(argv)
            out = Path(argv[argv.index("--output-dir") + 1])
            out.mkdir(parents=True)
            (out / "result.json").write_text(
                json.dumps({
                    "hardware_accessed": False,
                    "samples": [{"time_s": 0.25}],
                    "stop_reason": "completed",
                }),
                encoding="utf-8",
            )
            (out / "plan.json").write_text(
                json.dumps({"tool_sha256": {}}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0)

        return invoke

    def _run_intervention(
        self,
        root: Path,
        correction: list[float],
        grid_point: tuple[float, float] = (5.0, -3.0),
    ) -> tuple[dict, Path, Path, list[str]]:
        repo, source_config = self._repo(root)
        workdir = root / "work"
        workdir.mkdir()
        calls: list[list[str]] = []
        request = {
            "source_repo": str(repo),
            "grid_point": {"x_mm": grid_point[0], "y_mm": grid_point[1]},
            "params": {"plan_correction_mm": correction},
        }
        with mock.patch.object(
            run_cup_contact.subprocess, "run", side_effect=self._mock_run(calls)
        ):
            response = run_cup_contact.run(request, workdir)
        return response, source_config, workdir / "intervention_config.json", calls[0]

    def test_changes_only_planned_xy_and_preserves_actual_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response, source_path, generated_path, argv = self._run_intervention(
                root, [8.0, -4.0]
            )

            source_before = source_path.read_bytes()
            source_hash = hashlib.sha256(source_before).hexdigest()
            original = json.loads(source_before)
            generated = json.loads(generated_path.read_text(encoding="utf-8"))
            metadata = response["plan_intervention"]

            self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(metadata["source_config_sha256"], source_hash)
            self.assertEqual(
                metadata["generated_config_sha256"],
                hashlib.sha256(generated_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(generated["cup_center_m"], [0.398, 0.166, 0.78])
            expected = json.loads(json.dumps(original))
            expected["cup_center_m"][0] = 0.398
            expected["cup_center_m"][1] = 0.166
            self.assertEqual(generated, expected)
            self.assertEqual(
                metadata["changed_config_keys"], ["cup_center_m[0]", "cup_center_m[1]"]
            )
            self.assertEqual(metadata["original_grid_point_mm"], [5.0, -3.0])
            self.assertEqual(metadata["effective_grid_point_mm"], [-3.0, 1.0])
            self.assertEqual(metadata["actual_spawn_center_m"], [0.395, 0.167, 0.784])
            self.assertEqual(argv[argv.index("--config") + 1], str(generated_path))
            self.assertEqual(argv[argv.index("--spawn-x-offset-mm") + 1], "-3")
            self.assertEqual(argv[argv.index("--spawn-y-offset-mm") + 1], "1")

    def test_opposite_sign_correction_also_preserves_actual_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response, _, _, _ = self._run_intervention(
                Path(tmp), [-7.0, 6.0], grid_point=(-2.0, 9.0)
            )
            intervention = response["plan_intervention"]
            original_actual = [0.39 - 0.002, 0.17 + 0.009, 0.78 + 0.004]
            corrected_actual = [
                intervention["corrected_planned_center_m"][0]
                + intervention["effective_grid_point_mm"][0] / 1000,
                intervention["corrected_planned_center_m"][1]
                + intervention["effective_grid_point_mm"][1] / 1000,
                intervention["corrected_planned_center_m"][2] + 0.004,
            ]
            for expected, actual in zip(original_actual, corrected_actual):
                self.assertTrue(math.isclose(expected, actual, rel_tol=0.0, abs_tol=1e-12))

    def test_tiny_negative_offsets_are_plain_decimal_and_parse_as_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response, _, _, argv = self._run_intervention(
                Path(tmp), [0.0000751019, 0.00000002003], grid_point=(0.0, 0.0)
            )
            parser = argparse.ArgumentParser()
            parser.add_argument("--headless", action="store_true")
            parser.add_argument("--output-dir", required=True)
            parser.add_argument("--config", required=True)
            parser.add_argument("--spawn-x-offset-mm", type=float, required=True)
            parser.add_argument("--spawn-y-offset-mm", type=float, required=True)
            parsed = parser.parse_args(argv[2:])

            self.assertEqual(argv[argv.index("--spawn-x-offset-mm") + 1], "-0.0000751019")
            self.assertEqual(argv[argv.index("--spawn-y-offset-mm") + 1], "-0.00000002003")
            self.assertNotIn("e", argv[argv.index("--spawn-x-offset-mm") + 1].lower())
            self.assertNotIn("e", argv[argv.index("--spawn-y-offset-mm") + 1].lower())
            self.assertAlmostEqual(parsed.spawn_x_offset_mm, -0.0000751019)
            self.assertAlmostEqual(parsed.spawn_y_offset_mm, -0.00000002003)
            self.assertEqual(
                response["plan_intervention"]["effective_grid_point_mm"],
                [-0.0000751019, -0.00000002003],
            )

    def test_offset_arguments_preserve_round_trip_precision(self) -> None:
        values = (-0.0000751019, 0.00000002003, -9.9999785423, 9.9999785423, -3.0, 1.0)
        for value in values:
            with self.subTest(value=value):
                encoded = run_cup_contact._number_arg(value)
                self.assertNotIn("e", encoded.lower())
                self.assertEqual(float(encoded), value)
        self.assertEqual(run_cup_contact._number_arg(-3.0), "-3")
        self.assertEqual(run_cup_contact._number_arg(1.0), "1")

    def test_near_ten_mm_correction_remains_invariant_after_argv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            response, _, _, argv = self._run_intervention(
                Path(tmp), [9.9999785423, -9.9999785423], grid_point=(0.0, 0.0)
            )
            intervention = response["plan_intervention"]
            x_arg = float(argv[argv.index("--spawn-x-offset-mm") + 1])
            y_arg = float(argv[argv.index("--spawn-y-offset-mm") + 1])
            corrected = intervention["corrected_planned_center_m"]
            actual_after_argv = [
                corrected[0] + x_arg / 1000,
                corrected[1] + y_arg / 1000,
            ]

            self.assertEqual(x_arg, -9.9999785423)
            self.assertEqual(y_arg, 9.9999785423)
            self.assertTrue(math.isclose(actual_after_argv[0], 0.39, rel_tol=0.0, abs_tol=1e-12))
            self.assertTrue(math.isclose(actual_after_argv[1], 0.17, rel_tol=0.0, abs_tol=1e-12))

    def test_invalid_corrections_are_rejected_before_subprocess(self) -> None:
        bad_values = ([1.0], [1.0, 2.0, 3.0], [True, 0.0], [math.nan, 0.0], "1,2")
        for index, value in enumerate(bad_values):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo, _ = self._repo(root)
                workdir = root / "work"
                workdir.mkdir()
                request = {
                    "source_repo": str(repo),
                    "params": {"plan_correction_mm": value},
                }
                with (
                    mock.patch.object(run_cup_contact.subprocess, "run") as mocked,
                    self.assertRaisesRegex(ValueError, "유한한 숫자 2개"),
                ):
                    run_cup_contact.run(request, workdir)
                mocked.assert_not_called()
                self.assertFalse((workdir / "intervention_config.json").exists(), index)

    def test_recovery_and_plan_intervention_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self._repo(root)
            workdir = root / "work"
            workdir.mkdir()
            with (
                mock.patch.object(run_cup_contact.subprocess, "run") as mocked,
                self.assertRaisesRegex(ValueError, "함께 사용할 수 없습니다"),
            ):
                run_cup_contact.run(
                    {
                        "source_repo": str(repo),
                        "params": {"plan_correction_mm": [1.0, 2.0], "recover": True},
                    },
                    workdir,
                )
            mocked.assert_not_called()

    def test_fixed_source_config_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self._repo(root, config_symlink=True)
            workdir = root / "work"
            workdir.mkdir()
            with self.assertRaisesRegex(ValueError, "심볼릭 링크"):
                run_cup_contact.run(
                    {
                        "source_repo": str(repo),
                        "params": {"plan_correction_mm": [1.0, 2.0]},
                    },
                    workdir,
                )

    def test_no_correction_keeps_legacy_arguments_and_needs_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "team"
            script = repo / "simulation" / "simulate_cup_contact.py"
            script.parent.mkdir(parents=True)
            script.write_text("# fake simulator\n", encoding="utf-8")
            workdir = root / "work"
            workdir.mkdir()
            calls: list[list[str]] = []
            with mock.patch.object(
                run_cup_contact.subprocess, "run", side_effect=self._mock_run(calls)
            ):
                response = run_cup_contact.run(
                    {
                        "source_repo": str(repo),
                        "grid_point": {"x_mm": 5.0, "y_mm": 0.0},
                    },
                    workdir,
                )
            self.assertNotIn("--config", calls[0])
            self.assertNotIn("plan_intervention", response)
            self.assertFalse((workdir / "intervention_config.json").exists())


if __name__ == "__main__":
    unittest.main()
