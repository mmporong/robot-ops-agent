from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import PROJECT_ROOT, write_profile  # noqa: E402

from robot_ops.replay.profile import (  # noqa: E402
    ProfileError,
    current_user,
    display_path,
    expand_path,
    load_profile,
)


class ExpandPathTest(unittest.TestCase):
    def test_tilde_and_user_expand(self) -> None:
        self.assertEqual(expand_path("~/x"), Path.home() / "x")
        self.assertEqual(expand_path("/data/$USER/a"), Path(f"/data/{current_user()}/a"))
        self.assertEqual(expand_path("/data/${USER}/a"), Path(f"/data/{current_user()}/a"))

    def test_other_variables_rejected(self) -> None:
        for value in ("/data/$HOME2/a", "$HOME/x", "/data/$USERNAME/a", "/x/${HOME}"):
            with self.subTest(value=value), self.assertRaises(ProfileError):
                expand_path(value)

    def test_tilde_user_and_relative_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            expand_path("~root/x")
        with self.assertRaises(ProfileError):
            expand_path("relative/path")
        self.assertEqual(expand_path("rel/x", Path("/base")), Path("/base/rel/x"))

    def test_display_path_normalizes_home_and_data_user(self) -> None:
        self.assertEqual(display_path(Path.home() / "a"), "~/a")
        self.assertEqual(display_path(f"/data/{current_user()}/r"), "/data/$USER/r")


def _module_constants(path: Path) -> dict:
    """어댑터 모듈을 import하지 않고(서드파티·팀 의존 없이) 최상위 상수만 읽는다."""
    consts = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.startswith("TEAM_") and name != "TEAM_REPO":
                value = node.value
                if isinstance(value, ast.Call):  # frozenset({...})
                    value = value.args[0]
                consts[name] = ast.literal_eval(value)
    return consts


class AdapterAllowlistTest(unittest.TestCase):
    """팀 파일 allowlist는 profile이 원본이다. 어댑터 쪽 사본과 run 모듈 상수가 어긋나면 실패한다."""

    def test_entry_allowlist_matches_profile_and_run_modules(self) -> None:
        profile = load_profile(PROJECT_ROOT / "profiles" / "bimanual.toml")
        adapters = PROJECT_ROOT / "adapters" / "bimanual"
        entry = _module_constants(adapters / "entry.py")
        self.assertEqual(set(entry["TEAM_SCRIPTS"]), set(profile.allowlist_scripts))
        self.assertEqual(set(entry["TEAM_IMPORTS"]), set(profile.allowlist_imports))
        self.assertIn(_module_constants(adapters / "run_cup_contact.py")["TEAM_SCRIPT"], profile.allowlist_scripts)
        self.assertIn(_module_constants(adapters / "run_drive_kinematic.py")["TEAM_FILE"], profile.allowlist_imports)


class LoadProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repository_bimanual_profile_loads(self) -> None:
        profile = load_profile(PROJECT_ROOT / "profiles" / "bimanual.toml")
        self.assertEqual(profile.id, "bimanual")
        self.assertEqual(profile.base_dir, PROJECT_ROOT)
        self.assertEqual(profile.interpreter_for("cup_contact", "judge"), [sys.executable])
        self.assertEqual(profile.artifacts_root, Path(f"/data/{current_user()}/robot-ops/bimanual"))
        self.assertEqual(profile.interpreters["rlwalk"], Path.home() / "miniforge3/envs/rlwalk/bin/python")
        self.assertEqual(
            profile.delete_patterns,
            ("scene.usda", "frames/*.png", "isaac/scene.usda", "isaac/frames/*.png"),
        )
        self.assertEqual(profile.public_exclude_status, ("after_fix_regression",))

    def test_fake_profile_loads(self) -> None:
        profile = write_profile(self.root)
        self.assertEqual(profile.interpreter_for("cup_contact", "run"), [sys.executable])

    def _variant(self, old: str, new: str) -> Path:
        write_profile(self.root)
        path = self.root / "profiles" / "fake.toml"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return path

    def test_core_on_team_import_verb_fails(self) -> None:
        for old, new in (
            ('[adapter.tasks.cup_contact]\nrun = "fake"', '[adapter.tasks.cup_contact]\nrun = "core"'),
            ('convert = "fake"', 'convert = "core"'),
        ):
            with self.subTest(new=new):
                path = self._variant(old, new)
                with self.assertRaisesRegex(ProfileError, "core"):
                    load_profile(path)

    def test_imports_team_task_forbids_core_judge(self) -> None:
        path = self._variant(
            '[adapter.tasks.drive_kinematic]\nrun = "fake"',
            '[adapter.tasks.drive_kinematic]\nimports_team = true\nrun = "fake"',
        )
        with self.assertRaisesRegex(ProfileError, "core"):
            load_profile(path)

    def test_core_reserved_in_interpreters(self) -> None:
        path = self._variant("[interpreters]\n", '[interpreters]\ncore = "/usr/bin/python3"\n')
        with self.assertRaisesRegex(ProfileError, "예약어"):
            load_profile(path)

    def test_unknown_verb_and_interpreter_rejected(self) -> None:
        path = self._variant('judge = "core"', 'describe = "core"')
        with self.assertRaisesRegex(ProfileError, "동사"):
            load_profile(path)
        path = self._variant('convert = "fake"', 'convert = "ghost"')
        with self.assertRaisesRegex(ProfileError, "인터프리터"):
            load_profile(path)

    def test_non_user_variable_in_profile_rejected(self) -> None:
        path = self._variant("[interpreters]\n", '[interpreters]\nbad = "/data/$HOME2/python"\n')
        with self.assertRaisesRegex(ProfileError, "환경변수"):
            load_profile(path)


if __name__ == "__main__":
    unittest.main()
