from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import PROJECT_ROOT, make_scenario, save_scenario, write_profile  # noqa: E402

from robot_ops.cli import main  # noqa: E402

REPLAY_MODULES = (
    "profile", "schema", "adapter", "divergence", "grid", "scenario_bank",
    "regress", "artifacts", "media", "report",
)


class ReplayCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _main(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(argv))
        return code, json.loads(buffer.getvalue())

    def test_replay_detects_freeze_with_tcp(self) -> None:
        code, out = self._main(
            "replay", "--profile", str(self.profile.path),
            "--source", f"lerobot:{self.root / 'dataset'}", "--episode", "3",
        )
        self.assertEqual(code, 0, out)
        kinds = {i["kind"] for i in out["incidents"]}
        self.assertIn("freeze", kinds)
        self.assertIsNotNone(out["tcp"])
        freeze = next(i for i in out["incidents"] if i["kind"] == "freeze")
        self.assertLessEqual(abs(freeze["frame_start"] - 30), 1)
        result = json.loads(
            (self.root / "artifacts" / "runs" / out["run_id"] / "replay_result.json").read_text()
        )
        self.assertEqual(result["source"]["episode"], 3)

    def test_replay_rejects_unknown_source_kind(self) -> None:
        code, out = self._main(
            "replay", "--profile", str(self.profile.path), "--source", "mcap:/x", "--episode", "0",
        )
        self.assertEqual(code, 2)
        self.assertIn("lerobot", out["error"])

    def test_regress_and_report_build(self) -> None:
        save_scenario(
            self.root,
            make_scenario(
                grid={"spawn_x_offset_mm": [0, 5], "spawn_y_offset_mm": [0], "repeat_points": 1, "repeats": 2},
                params={"A": {"fail_points": [[5, 0]]}},
            ),
        )
        code, out = self._main(
            "regress", "--profile", str(self.profile.path), "--scenario", "fake-cup", "--condition", "A",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(out["conditions"]["A"]["pass"], 1)
        self.assertNotIn("B", out["conditions"])
        code, out = self._main("report", "build", "--profile", str(self.profile.path))
        self.assertEqual(code, 0, out)
        self.assertEqual((out["build"], out["scenario_count"], out["regress_count"]), ("public", 1, 1))

    def test_report_build_hero_option(self) -> None:
        save_scenario(self.root, make_scenario())
        code, out = self._main(
            "report", "build", "--profile", str(self.profile.path), "--hero", "observation_freeze",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(out["hero_request"], "observation_freeze")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["report", "build", "--profile", str(self.profile.path), "--hero", "banner"])

    def test_report_serve_requires_built_report(self) -> None:
        code, out = self._main("report", "serve", "--dir", str(self.root / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("report build", out["error"])

    def test_report_serve_supports_range_requests(self) -> None:
        import http.client
        import threading

        from robot_ops.replay.report import serve_in_thread

        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (site / "clip.mp4").write_bytes(bytes(range(256)) * 4)
        httpd = serve_in_thread(site, port=0)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", "/clip.mp4", headers={"Range": "bytes=10-19"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.getheader("Content-Range"), "bytes 10-19/1024")
            self.assertEqual(resp.read(), bytes(range(10, 20)))
            conn.request("GET", "/clip.mp4")
            resp = conn.getresponse()
            self.assertEqual((resp.status, len(resp.read())), (200, 1024))
            self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")
            conn.request("GET", "/clip.mp4", headers={"Range": "bytes=5000-"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 416)
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertFalse(any(t.name == "robot-ops-report-serve" and t.is_alive() for t in threading.enumerate()))

    def test_hardware_guard_returns_error(self) -> None:
        save_scenario(self.root, make_scenario(params={"A": {"hardware_accessed": "true"}}))
        code, out = self._main(
            "regress", "--profile", str(self.profile.path), "--scenario", "fake-cup",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["type"], "HardwareGuardError")

    def test_core_modules_load_no_third_party(self) -> None:
        imports = ", ".join(f"robot_ops.replay.{m}" for m in REPLAY_MODULES)
        code = (
            "import sys; sys.path.insert(0, 'src'); "
            f"import {imports}; "
            "bad = {'numpy', 'pyarrow', 'mcp', 'mujoco', 'isaacsim'} & "
            "{m.split('.')[0] for m in sys.modules}; print(sorted(bad))"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", code], cwd=PROJECT_ROOT, env={},
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
