from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from replay_support import write_profile
from robot_ops.replay.doctor import check_profile


def _completed(argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class ReplayDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = write_profile(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _profile(self, **changes):
        return replace(self.profile, source_repo=None, **changes)

    def test_ready_contract_and_no_write_side_effects(self) -> None:
        profile = self._profile()
        self.assertFalse(profile.artifacts_root.exists())
        adapter_mtime = profile.adapter_entry.stat().st_mtime_ns

        result = check_profile(profile, task="drive_kinematic")

        self.assertEqual(result["schema"], "robot-ops-doctor/1")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["scope"], "replay_runtime_prerequisites")
        self.assertEqual(result["profile"], "fake")
        self.assertEqual(result["tasks"], ["drive_kinematic"])
        self.assertEqual(result["summary"]["blocked"], 0)
        self.assertIn("패키지 import", result["boundaries"]["not_verified"])
        self.assertFalse(profile.artifacts_root.exists())
        self.assertEqual(profile.adapter_entry.stat().st_mtime_ns, adapter_mtime)

    def test_unknown_task_is_clear_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "프로파일에 없는 과제.*unknown"):
            check_profile(self._profile(), task="unknown")

    def test_missing_interpreter_blocks(self) -> None:
        missing = self.root / "missing-python"
        profile = self._profile(interpreters={"fake": missing})

        result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "interpreters")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(check["status"], "blocked")
        self.assertEqual(check["details"]["missing"][0]["path"], str(missing))

    def test_missing_source_repo_blocks(self) -> None:
        result = check_profile(self.profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "source_repo")
        self.assertEqual(check["status"], "blocked")
        self.assertIn("찾을 수 없습니다", check["message_ko"])

    def test_dirty_repo_blocks_and_probe_is_read_only(self) -> None:
        repo = self.root / "team"
        repo.mkdir()
        profile = replace(self.profile, source_repo=repo)

        def fake_run(argv, **kwargs):
            if argv[-2:] == ["rev-parse", "HEAD"]:
                return _completed(argv, stdout="0000000abcdef\n")
            return _completed(argv, stdout=" M simulation/run.py\n")

        with patch("robot_ops.replay.doctor.subprocess.run", side_effect=fake_run) as run:
            result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "source_repo")
        self.assertEqual(check["status"], "blocked")
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[:2], ["git", "--no-optional-locks"])
            self.assertIn(argv[-2:], (["rev-parse", "HEAD"], ["status", "--porcelain"]))
            self.assertFalse(call.kwargs["shell"])
            self.assertLessEqual(call.kwargs["timeout"], 5)
            self.assertEqual(call.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_pinned_commit_mismatch_is_warning(self) -> None:
        repo = self.root / "team"
        repo.mkdir()
        profile = replace(self.profile, source_repo=repo)

        def fake_run(argv, **_kwargs):
            stdout = "abcdef123456\n" if argv[-2:] == ["rev-parse", "HEAD"] else ""
            return _completed(argv, stdout=stdout)

        with patch("robot_ops.replay.doctor.subprocess.run", side_effect=fake_run):
            result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "source_repo")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(check["status"], "warning")
        self.assertEqual(result["summary"]["warning"], 1)

    def test_git_failure_blocks_and_redacts_error(self) -> None:
        repo = self.root / "team"
        repo.mkdir()
        profile = replace(self.profile, source_repo=repo)
        failure = _completed([], returncode=128, stderr="API_TOKEN=secret-value-that-must-hide")

        with patch("robot_ops.replay.doctor.subprocess.run", return_value=failure):
            result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "source_repo")
        self.assertEqual(check["status"], "blocked")
        self.assertNotIn("secret-value", check["details"]["error"])
        self.assertIn("[REDACTED]", check["details"]["error"])

    def test_disk_shortage_blocks_without_creating_artifact_root(self) -> None:
        profile = self._profile(limits={**self.profile.limits, "min_free_disk_gb": 2})
        usage = SimpleNamespace(total=10 * 1024**3, used=9 * 1024**3, free=1 * 1024**3)

        with patch("robot_ops.replay.doctor.shutil.disk_usage", return_value=usage):
            result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "artifact_disk")
        self.assertEqual(check["status"], "blocked")
        self.assertFalse(profile.artifacts_root.exists())
        self.assertEqual(check["details"]["checked_path"], str(self.root))

    def test_artifact_ancestor_file_blocks(self) -> None:
        occupied = self.root / "occupied"
        occupied.write_text("file", encoding="utf-8")
        profile = self._profile(artifacts_root=occupied / "runs")

        result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "artifact_disk")
        self.assertEqual(check["status"], "blocked")
        self.assertIn("디렉터리가 아닙니다", check["message_ko"])

    def test_cpu_auto_fallback_is_warning(self) -> None:
        profile = self._profile(limits={**self.profile.limits, "isolation": "auto"})
        with patch("robot_ops.replay.doctor.shutil.which", return_value=None):
            result = check_profile(profile, task="drive_kinematic")

        check = next(item for item in result["checks"] if item["id"] == "isolation")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(check["status"], "warning")
        self.assertEqual(check["details"]["fallback"], "pgid")

    def test_gpu_systemd_no_user_bus_blocks(self) -> None:
        profile = self._profile(
            limits={**self.profile.limits, "isolation": "auto", "gpu_tasks": ["cup_contact"]}
        )

        with (
            patch("robot_ops.replay.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "robot_ops.replay.doctor.subprocess.run",
                return_value=_completed([], returncode=1, stderr="Failed to connect to bus"),
            ),
        ):
            result = check_profile(profile, task="cup_contact")

        check = next(item for item in result["checks"] if item["id"] == "isolation")
        self.assertEqual(check["status"], "blocked")
        self.assertIn("systemd", check["message_ko"])

    def test_gpu_driver_failure_blocks_and_redacts_error(self) -> None:
        profile = self._profile(
            limits={**self.profile.limits, "isolation": "auto", "gpu_tasks": ["cup_contact"]}
        )

        def fake_run(argv, **_kwargs):
            if argv[0] == "systemctl":
                return _completed(argv, stdout="running\n")
            return _completed(
                argv,
                returncode=1,
                stdout="Failed to initialize NVML: Driver/library version mismatch\n",
                stderr="API_TOKEN=secret-value-that-must-hide",
            )

        with (
            patch("robot_ops.replay.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("robot_ops.replay.doctor.subprocess.run", side_effect=fake_run),
        ):
            result = check_profile(profile, task="cup_contact")

        check = next(item for item in result["checks"] if item["id"] == "gpu_probe")
        self.assertEqual(check["status"], "blocked")
        self.assertIn("Driver/library version mismatch", check["details"]["error"])
        self.assertNotIn("secret-value", check["details"]["error"])
        self.assertIn("[REDACTED]", check["details"]["error"])

    def test_gpu_ready_with_fixed_nvidia_probe(self) -> None:
        profile = self._profile(
            limits={**self.profile.limits, "isolation": "systemd", "gpu_tasks": ["cup_contact"]}
        )
        seen: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            if argv[0] == "systemctl":
                return _completed(argv, returncode=1, stdout="degraded\n")
            return _completed(argv, stdout="100\n200\n")

        with (
            patch("robot_ops.replay.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("robot_ops.replay.doctor.subprocess.run", side_effect=fake_run),
        ):
            result = check_profile(profile, task="cup_contact")

        self.assertEqual(result["status"], "ready")
        self.assertIn(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], seen
        )


if __name__ == "__main__":
    unittest.main()
