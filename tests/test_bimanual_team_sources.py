from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = PROJECT_ROOT / "adapters" / "bimanual"
sys.path.insert(0, str(ADAPTER_DIR))

import entry  # noqa: E402
import run_cup_contact  # noqa: E402
import team_source  # noqa: E402


class CupTeamSourceTest(unittest.TestCase):
    @staticmethod
    def _write_fake_script(path: Path, marker: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"""\
import argparse
import json
from pathlib import Path
from simulation import __name__ as simulation_name
from src.robot_pkg import VALUE

p = argparse.ArgumentParser()
p.add_argument('--headless', action='store_true')
p.add_argument('--output-dir', required=True)
p.add_argument('--spawn-x-offset-mm')
p.add_argument('--spawn-y-offset-mm')
p.add_argument('--recover', action='store_true')
p.add_argument('--record', action='store_true')
a = p.parse_args()
out = Path(a.output_dir)
out.mkdir(parents=True)
(out / 'result.json').write_text(json.dumps({{
    'hardware_accessed': False,
    'samples': [{{'time_s': 0.25}}],
    'stop_reason': 'completed',
    'cwd': str(Path.cwd()),
    'simulation_name': simulation_name,
    'value': VALUE,
    'marker': {marker},
}}), encoding='utf-8')
(out / 'plan.json').write_text(json.dumps({{'tool_sha256': {{}}}}), encoding='utf-8')
""",
            encoding="utf-8",
        )

    @staticmethod
    def _make_packages(team: Path) -> None:
        (team / "simulation").mkdir(parents=True)
        (team / "src" / "robot_pkg").mkdir(parents=True)
        (team / "simulation" / "__init__.py").write_text("", encoding="utf-8")
        (team / "src" / "__init__.py").write_text("", encoding="utf-8")
        (team / "src" / "robot_pkg" / "__init__.py").write_text("VALUE = 17\n", encoding="utf-8")

    @staticmethod
    def _mock_team_run(marker: int, calls: list[dict]):
        def invoke(argv, *, cwd, env, stdout, stderr, check):
            calls.append({"argv": argv, "cwd": cwd, "env": env})
            out = Path(argv[argv.index("--output-dir") + 1])
            out.mkdir(parents=True)
            (out / "result.json").write_text(
                json.dumps({
                    "hardware_accessed": False,
                    "samples": [{"time_s": 0.25}],
                    "stop_reason": "completed",
                    "marker": marker,
                }),
                encoding="utf-8",
            )
            (out / "plan.json").write_text(
                json.dumps({"tool_sha256": {}}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0)

        return invoke

    def test_requested_source_repo_selects_new_script_and_pythonpath_without_repo_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            team = root / "team"
            workdir = root / "work"
            self._make_packages(team)
            self._write_fake_script(team / "simulation" / "simulate_cup_contact.py", 1)
            self._write_fake_script(team / "tools" / "simulate_cup_contact.py", 2)
            before = {p.relative_to(team) for p in team.rglob("*")}
            workdir.mkdir()
            calls: list[dict] = []
            with (
                mock.patch.object(run_cup_contact, "TEAM_REPO", root / "missing-default"),
                mock.patch.object(
                    run_cup_contact.subprocess,
                    "run",
                    side_effect=self._mock_team_run(1, calls),
                ),
            ):
                response = run_cup_contact.run(
                    {
                        "source_repo": str(team),
                        "grid_point": {"x_mm": 0, "y_mm": 0},
                    },
                    workdir,
                )

            result = json.loads((workdir / "isaac" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["marker"], 1)
            self.assertEqual(response["team_script"], "simulation/simulate_cup_contact.py")
            self.assertEqual(calls[0]["argv"][1], str(team / "simulation" / "simulate_cup_contact.py"))
            self.assertEqual(calls[0]["cwd"], str(workdir))
            self.assertEqual(calls[0]["env"]["PYTHONPATH"].split(os.pathsep)[0], str(team))
            self.assertEqual(calls[0]["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual({p.relative_to(team) for p in team.rglob("*")}, before)

    def test_missing_source_repo_uses_default_and_legacy_script_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            team = root / "team"
            workdir = root / "work"
            self._make_packages(team)
            self._write_fake_script(team / "tools" / "simulate_cup_contact.py", 2)
            workdir.mkdir()
            calls: list[dict] = []
            with (
                mock.patch.object(run_cup_contact, "TEAM_REPO", team),
                mock.patch.object(
                    run_cup_contact.subprocess,
                    "run",
                    side_effect=self._mock_team_run(2, calls),
                ),
            ):
                response = run_cup_contact.run({}, workdir)
            result = json.loads((workdir / "isaac" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["marker"], 2)
            self.assertEqual(response["team_script"], "tools/simulate_cup_contact.py")
            self.assertEqual(calls[0]["env"]["PYTHONPATH"].split(os.pathsep)[0], str(team))

    def test_requested_source_repo_rejects_relative_symlink_and_non_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            team = root / "team"
            team.mkdir()
            linked = root / "linked-team"
            linked.symlink_to(team, target_is_directory=True)
            regular_file = root / "repo.txt"
            regular_file.write_text("not a directory\n", encoding="utf-8")

            cases = (
                ("relative/team", "절대 경로"),
                (str(linked), "심볼릭 링크"),
                (str(regular_file), "일반 디렉터리"),
            )
            for value, message in cases:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                    run_cup_contact.run({"source_repo": value}, root / "work")

    def test_missing_and_symlink_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            team = root / "team"
            team.mkdir()
            with self.assertRaisesRegex(FileNotFoundError, "후보"):
                team_source.resolve_team_file(team, ("simulation/missing.py",))
            (team / "simulation").mkdir()
            real = team / "real.py"
            real.write_text("pass\n", encoding="utf-8")
            (team / "simulation" / "linked.py").symlink_to(real)
            with self.assertRaisesRegex(ValueError, "심볼릭 링크"):
                team_source.resolve_team_file(team, ("simulation/linked.py",))
            (team / "simulation" / "linked.py").unlink()
            (team / "simulation" / "linked.py").symlink_to(team / "missing.py")
            with self.assertRaisesRegex(ValueError, "심볼릭 링크"):
                team_source.resolve_team_file(team, ("simulation/linked.py", "real.py"))

    def test_symlinked_parent_is_rejected_even_when_target_stays_inside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            team = Path(tmp) / "team"
            (team / "real").mkdir(parents=True)
            (team / "real" / "script.py").write_text("pass\n", encoding="utf-8")
            (team / "simulation").symlink_to(team / "real", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "심볼릭 링크"):
                team_source.resolve_team_file(team, ("simulation/script.py",))


class DriveSnapshotTest(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_snapshot_selects_path_from_each_exact_commit_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "team"
            repo.mkdir()
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.invalid")
            (repo / "tools").mkdir()
            (repo / "tools" / "mobile_service_control.py").write_bytes(b"OLD = True\n")
            self._git(repo, "add", "tools/mobile_service_control.py")
            self._git(repo, "commit", "-qm", "old")
            old = self._git(repo, "rev-parse", "HEAD")
            (repo / "simulation").mkdir()
            (repo / "tools" / "mobile_service_control.py").rename(
                repo / "simulation" / "mobile_service_control.py"
            )
            (repo / "simulation" / "mobile_service_control.py").write_bytes(b"NEW = True\n")
            self._git(repo, "add", "tools/mobile_service_control.py", "simulation/mobile_service_control.py")
            self._git(repo, "commit", "-qm", "new")
            new = self._git(repo, "rev-parse", "HEAD")

            candidates = ("tools/mobile_service_control.py", "simulation/mobile_service_control.py")
            old_snap, old_commit, old_path = team_source.snapshot_git_file(
                repo, old, candidates, root / "old-run" / "snapshot.py"
            )
            new_snap, new_commit, new_path = team_source.snapshot_git_file(
                repo, new, candidates, root / "new-run" / "snapshot.py"
            )

            self.assertEqual((old_commit, old_path, old_snap.read_bytes()),
                             (old, "tools/mobile_service_control.py", b"OLD = True\n"))
            self.assertEqual((new_commit, new_path, new_snap.read_bytes()),
                             (new, "simulation/mobile_service_control.py", b"NEW = True\n"))

    def test_snapshot_rejects_missing_file_and_git_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "team"
            repo.mkdir()
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.invalid")
            (repo / "simulation").mkdir()
            (repo / "simulation" / "mobile_service_control.py").symlink_to("../outside.py")
            self._git(repo, "add", "simulation/mobile_service_control.py")
            self._git(repo, "commit", "-qm", "symlink")
            commit = self._git(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ValueError, "심볼릭 링크"):
                team_source.snapshot_git_file(
                    repo, commit, ("simulation/mobile_service_control.py",), root / "symlink.py"
                )
            with self.assertRaisesRegex(FileNotFoundError, "후보"):
                team_source.snapshot_git_file(repo, commit, ("tools/missing.py",), root / "missing.py")


class TeamAllowlistTest(unittest.TestCase):
    def test_legacy_and_new_paths_are_allowed_but_traversal_is_rejected(self) -> None:
        entry.check_allowlist({
            "scripts": ["tools/simulate_cup_contact.py", "simulation/simulate_cup_contact.py"],
            "imports": ["tools/mobile_service_control.py", "simulation/mobile_service_control.py"],
        })
        for path in ("../simulation/simulate_cup_contact.py", "/simulation/simulate_cup_contact.py"):
            with self.subTest(path=path), self.assertRaises(entry.Unsupported):
                entry.check_allowlist({"scripts": [path]})


if __name__ == "__main__":
    unittest.main()
