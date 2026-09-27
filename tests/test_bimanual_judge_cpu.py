"""양팔 어댑터 judge CPU 검사 (계획 M2).

- 컵 계열 과거 결과(~30~33개, realpath 중복 제거)에 judge를 돌려 저장된 판정과 일치하는지 본다.
  samples 재계산이 저장값과 다른 경우는 '발견'으로 출력만 하고 실패시키지 않는다.
- judge는 stdlib만 쓴다: `python -S`로 entry.py judge를 돌린 뒤 sys.modules에 서드파티가 없어야 한다.
- drive_kinematic judge는 임시 결과 파일로 통과·실패 분기를 확인한다(팀 저장소 불필요).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO_ROOT / "adapters" / "bimanual"
ENTRY = ADAPTER_DIR / "entry.py"
RESULT_ROOTS = (
    Path.home() / "bimanual-robot" / "logs",
    Path("/data") / os.environ.get("USER", "") / "robot-artifacts",
)
CUP_PREFIX = "cup_"
THIRD_PARTY = {"numpy", "pyarrow", "mujoco", "mcp", "isaacsim", "PIL", "scipy", "torch"}


def _load_judge():
    spec = importlib.util.spec_from_file_location("bimanual_judge", ADAPTER_DIR / "judge.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def find_cup_results() -> list[Path]:
    found: dict[str, Path] = {}
    for root in RESULT_ROOTS:
        if not root.is_dir():
            continue
        for current, _dirs, files in os.walk(root, followlinks=True):
            if "result.json" not in files:
                continue
            path = Path(current) / "result.json"
            if path.relative_to(root).parts[0].startswith(CUP_PREFIX):
                found.setdefault(os.path.realpath(path), path)
    return [Path(p) for p in sorted(found)]


class CupJudgeAgreementTest(unittest.TestCase):
    def test_stored_verdict_agreement(self) -> None:
        results = find_cup_results()
        if not results:
            self.skipTest("양팔 로봇 프로젝트 컵 결과가 이 기기에 없습니다")
        judge = _load_judge()
        findings = []
        no_recheck = []
        for path in results:
            stored_doc = json.loads(path.read_text(encoding="utf-8"))
            key = "task_pass" if "task_pass" in stored_doc else "rigid_proxy_lift_pass"
            out = judge.judge_cup(path)
            with self.subTest(result=str(path)):
                self.assertEqual(out["verdict"], "pass" if stored_doc[key] else "fail")
                self.assertEqual(out["assembly"], "left_arm_proxy")
                self.assertIn(out["judgement_basis"], ("rigid_proxy_lift", "oracle_recovery_sim_coordinates"))
                if out["verdict"] == "fail":
                    self.assertIsNotNone(out["failure_code"])
                for check in out["checks"]:
                    base = {"id", "pass", "value", "threshold", "unit"}
                    self.assertEqual(set(check) - {"t_s"}, base)
                    self.assertIn(check["pass"], (True, False, None))
                if out["verdict"] == "pass":
                    self.assertNotIn(None, [c["pass"] for c in out["checks"]])
            recheck = out["sample_recheck"]
            if recheck is None:
                no_recheck.append(str(path))
            elif not recheck["agrees"]:
                findings.append({"result": str(path), **recheck})
        print(f"\n[judge] 컵 계열 결과 {len(results)}개, 저장 판정 일치 {len(results)}/{len(results)}")
        print(f"[judge] samples 재계산 불가(옛 config 스키마·samples 없음) {len(no_recheck)}개")
        print(f"[judge] 발견(samples 재계산 ≠ 저장값) {len(findings)}건")
        for item in findings:
            print("  -", json.dumps(item, ensure_ascii=False))
        self.assertGreaterEqual(len(results), 30)


class CupEarlyStopTest(unittest.TestCase):
    """조기 종료(실패 이벤트로 LIFT_HOLD 샘플 없음) → 측정 안 된 검사는 pass=None, 원인은 첫 행."""

    CONFIG = {
        "physics_dt_s": 0.1, "minimum_lift_m": 0.04, "minimum_contact_force_n": 0.02,
        "maximum_tracking_error_rad": 0.12, "maximum_midbody_height_error_m": 0.015,
        "maximum_contact_center_error_m": 0.015, "maximum_cup_lateral_drift_m": 0.02,
        "maximum_cup_tilt_deg": 15.0, "cup_center_m": [0.0, 0.0, 0.0],
    }

    def setUp(self) -> None:
        self.judge = _load_judge()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "plan.json").write_text(json.dumps({"config": self.CONFIG, "recovery_enabled": False}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _sample(phase: str, z: float) -> dict:
        return {"attempt": 0, "phase": phase, "cup_position_m": [0.0, 0.0, z], "contact_force_n": [0.5, 0.5],
                "arm_error_rad": 0.01, "midbody_height_error_m": 0.001, "contact_center_error_m": 0.001,
                "cup_tilt_deg": 1.0}

    def _judge(self, **fields) -> dict:
        path = self.dir / "result.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        return self.judge.judge_cup(path)

    def test_early_stop_marks_unmeasured_checks_none(self) -> None:
        out = self._judge(
            completed=False, task_pass=False, lift_stage_pass=False, stop_reason="premature_cup_contact",
            samples=[self._sample("APPROACH", 0.0)],
            events=[{"type": "preclose_failure", "time_s": 6.425, "failure_code": "premature_cup_contact"}],
        )
        self.assertEqual(out["verdict"], "fail")
        first = out["checks"][0]
        self.assertEqual((first["id"], first["pass"], first["value"], first["t_s"]),
                         ("failure_event", False, "premature_cup_contact", 6.425))
        by_id = {c["id"]: c for c in out["checks"]}
        self.assertIs(by_id["stored_task_pass"]["pass"], False)
        for cid in ("hold_window_samples", "samples_finite", "proxy_lift_height", "cup_tilt_max"):
            self.assertIsNone(by_id[cid]["pass"], cid)
        # 재계산 비교는 내부 bool로 한다 → 저장값(False)과 일치
        self.assertTrue(out["sample_recheck"]["agrees"])

    def test_pass_result_has_no_none(self) -> None:
        hold = [self._sample("LIFT_HOLD", 0.05) for _ in range(10)]
        out = self._judge(completed=True, task_pass=True, lift_stage_pass=True, samples=hold, events=[])
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["checks"][0]["id"], "stored_task_pass")
        self.assertTrue(all(c["pass"] is True for c in out["checks"]))

    def test_completed_failure_keeps_measured_false(self) -> None:
        hold = [self._sample("LIFT_HOLD", 0.01) for _ in range(10)]
        out = self._judge(completed=True, task_pass=False, lift_stage_pass=False, samples=hold, events=[])
        by_id = {c["id"]: c for c in out["checks"]}
        self.assertNotIn("failure_event", by_id)
        self.assertIs(by_id["proxy_lift_height"]["pass"], False)
        self.assertIs(by_id["hold_window_samples"]["pass"], True)


class JudgeStdlibOnlyTest(unittest.TestCase):
    def _run_isolated(self, task: str, result_path: Path, workdir: Path) -> dict:
        request = workdir / "judge.request.json"
        response = workdir / "judge.response.json"
        request.write_text(json.dumps({"result_path": str(result_path), "task": task}), encoding="utf-8")
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ADAPTER_DIR)!r})\n"
            "import entry\n"
            f"rc = entry.main(['judge', '--task', {task!r}, '--request', {str(request)!r}, "
            f"'--response', {str(response)!r}, '--workdir', {str(workdir)!r}])\n"
            "mods = sorted({m.split('.')[0] for m in sys.modules})\n"
            "print(json.dumps({'rc': rc, 'modules': mods}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            capture_output=True, text=True, check=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                 "HOME": str(Path.home()), "USER": os.environ.get("USER", "")},
        )
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        bad = THIRD_PARTY & set(report["modules"])
        self.assertFalse(bad, f"judge가 서드파티를 로드했습니다: {bad}")
        self.assertEqual(report["rc"], 0, proc.stderr)
        return json.loads(response.read_text(encoding="utf-8"))

    def test_drive_judge_under_isolated_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            result = workdir / "result.json"
            result.write_text(json.dumps({"arrived": True, "elapsed_s": 24.96, "reversal_rate_per_s": 2.2837,
                                          "horizon_s": 360.0, "hardware_accessed": False}), encoding="utf-8")
            response = self._run_isolated("drive_kinematic", result, workdir)
            self.assertEqual(response["protocol"], "robot-ops-adapter/1")
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["verdict"], "pass")
            self.assertEqual(response["judgement_basis"], "kinematic_drive")

    def test_cup_judge_under_isolated_core(self) -> None:
        results = find_cup_results()
        if not results:
            self.skipTest("양팔 로봇 프로젝트 컵 결과가 이 기기에 없습니다")
        with tempfile.TemporaryDirectory() as tmp:
            response = self._run_isolated("cup_contact", results[0], Path(tmp))
            self.assertEqual(response["status"], "ok")
            self.assertIn(response["verdict"], ("pass", "fail"))


class DriveJudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.judge = _load_judge()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _judge(self, **fields) -> dict:
        doc = {"arrived": True, "elapsed_s": 10.0, "reversal_rate_per_s": 0.0, "horizon_s": 360.0, **fields}
        path = self.dir / "result.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return self.judge.judge_drive(path, self.judge.F1004_THRESHOLDS)

    def test_not_arrived_fails(self) -> None:
        out = self._judge(arrived=False, elapsed_s=360.0)
        self.assertEqual((out["verdict"], out["failure_code"]), ("fail", "not_arrived_within_horizon"))

    def test_reversal_rate_above_threshold_fails(self) -> None:
        out = self._judge(reversal_rate_per_s=2.46)
        self.assertEqual((out["verdict"], out["failure_code"]), ("fail", "reversal_rate_exceeded"))

    def test_at_threshold_passes(self) -> None:
        out = self._judge(reversal_rate_per_s=2.45)
        self.assertEqual(out["verdict"], "pass")

    def test_horizon_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._judge(horizon_s=60.0)


class EntryProtocolTest(unittest.TestCase):
    def _call(self, verb: str, task: str, request: dict) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            req = workdir / "req.json"
            resp = workdir / "resp.json"
            req.write_text(json.dumps(request), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-S", str(ENTRY), verb, "--task", task, "--request", str(req),
                 "--response", str(resp), "--workdir", str(workdir)],
                capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                     "HOME": str(Path.home())},
            )
            return proc.returncode, json.loads(resp.read_text(encoding="utf-8"))

    def test_unknown_verb_unsupported(self) -> None:
        code, resp = self._call("describe", "cup_contact", {})
        self.assertEqual((code, resp["status"]), (2, "unsupported"))

    def test_unmapped_task_unsupported(self) -> None:
        code, resp = self._call("judge", "lerobot_episode", {"result_path": "x"})
        self.assertEqual((code, resp["status"]), (2, "unsupported"))

    def test_allowlist_rejects_other_team_import(self) -> None:
        code, resp = self._call("run", "drive_kinematic", {"imports": ["tools/fk.py"], "params": {}})
        self.assertEqual((code, resp["status"]), (2, "unsupported"))
        self.assertIn("allowlist", resp["error"])

    def test_missing_result_is_infra_error(self) -> None:
        code, resp = self._call("judge", "cup_contact", {"result_path": "/nonexistent/result.json"})
        self.assertEqual((code, resp["status"]), (3, "infra_error"))


if __name__ == "__main__":
    unittest.main()
