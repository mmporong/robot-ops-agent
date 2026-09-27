from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import write_profile  # noqa: E402

from robot_ops.replay.adapter import (  # noqa: E402
    AllowlistRejected,
    HardwareGuardError,
    LockBusy,
    VerbRejected,
    build_scope_argv,
    call_adapter,
    run_lock,
    systemd_available,
)


def _processes_with_marker(marker: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker.encode() in cmdline:
            found.append(int(entry.name))
    return found


class AdapterCallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, params: dict, **kwargs):
        request = {"task": "cup_contact", "params": params, "grid_point": {"x_mm": 0, "y_mm": 0}, "repeat": None}
        request.update(kwargs.pop("extra", {}))
        return call_adapter(
            self.profile, kwargs.pop("verb", "run"), "cup_contact", request,
            self.root / "work" / uuid.uuid4().hex[:8], run_id=kwargs.pop("run_id", "t-" + uuid.uuid4().hex[:8]),
            **kwargs,
        )

    def test_ok_run_and_env_has_no_bytecode_flag(self) -> None:
        outcome = self._run({"dump_env": True})
        self.assertEqual(outcome.status, "ok")
        env = json.loads((outcome.workdir / "env.json").read_text())
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["ROBOT_OPS_RUN_ID"], outcome.run_id)

    def test_unknown_verb_rejected_before_launch(self) -> None:
        with self.assertRaises(VerbRejected):
            self._run({}, verb="describe")

    def test_allowlist(self) -> None:
        ok = self._run({}, extra={"scripts": ["tools/simulate_cup_contact.py"], "imports": ["tools/mobile_service_control.py"]})
        self.assertEqual(ok.status, "ok")
        for extra in (
            {"scripts": ["tools/other.py"]},
            {"imports": ["tools/cup_contact_model.py"]},
            {"scripts": ["../bimanual-robot/tools/simulate_cup_contact.py"]},
            {"scripts": ["/home/x/tools/simulate_cup_contact.py"]},
        ):
            with self.subTest(extra=extra), self.assertRaises(AllowlistRejected):
                self._run({}, extra=extra)

    def test_hardware_guard(self) -> None:
        for params in (
            {"hardware_accessed": "true"},
            {"hardware_accessed": "missing"},
            {"result_hardware": "true"},
            {"result_hardware": "missing"},
        ):
            with self.subTest(params=params), self.assertRaises(HardwareGuardError):
                self._run(params)

    def test_infra_exit_code(self) -> None:
        outcome = self._run({"infra_points": [[0, 0]]})
        self.assertEqual(outcome.status, "infra_error")
        self.assertEqual(outcome.exit_code, 3)

    def _assert_timeout_leaves_no_survivors(self, isolation: str) -> None:
        marker = "robot-ops-test-" + uuid.uuid4().hex
        started = time.monotonic()
        outcome = self._run(
            {"sleep_s": 60, "spawn_setsid_child": True, "marker": marker},
            timeout_s=1, kill_after_s=1, isolation=isolation,
        )
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(outcome.status, "timeout")
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.isolation, isolation)
        deadline = time.monotonic() + 3
        while _processes_with_marker(marker) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(_processes_with_marker(marker), [])

    def test_timeout_pgid_kills_setsid_grandchild(self) -> None:
        self._assert_timeout_leaves_no_survivors("pgid")

    @unittest.skipUnless(systemd_available(), "systemd --user 없음")
    def test_timeout_systemd_scope_kills_setsid_grandchild(self) -> None:
        self._assert_timeout_leaves_no_survivors("systemd")

    def test_scope_argv(self) -> None:
        argv = build_scope_argv(["py", "entry.py", "run"], run_id="r1", memory_max="20G", timeout_s=600, kill_after_s=30)
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertIn("robot-ops-r1", argv)
        self.assertIn("MemoryMax=20G", argv)
        i = argv.index("timeout")
        self.assertEqual(argv[i : i + 3], ["timeout", "--kill-after=30", "600"])
        self.assertEqual(argv[-3:], ["py", "entry.py", "run"])
        self.assertNotIn("pkill", " ".join(argv))

    def test_lock_is_exclusive(self) -> None:
        lock = self.root / "x.lock"
        with run_lock(lock), self.assertRaises(LockBusy), run_lock(lock):
            pass
        with run_lock(lock):
            pass


if __name__ == "__main__":
    unittest.main()
