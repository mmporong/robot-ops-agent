from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import make_scenario, save_scenario, write_profile  # noqa: E402

from robot_ops.replay import media as mediacmd  # noqa: E402
from robot_ops.replay.profile import current_user  # noqa: E402
from robot_ops.replay.report import (  # noqa: E402
    OUT_OF_SCOPE,
    build,
    check_no_private_paths,
    failed_lines,
    grid_patterns,
    latest_regress_results,
)
from robot_ops.replay.schema import validate_regress_result  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "regress_result_cup_fixture.json"
PRIVATE = [str(Path.home()), f"/data/{current_user()}"]


def _lerobot_scenario() -> dict:
    return make_scenario(
        scenario_id="freeze-a",
        title_ko="관측 동결",
        origin="real_recording",
        judgement_basis="physical",
        task="lerobot_episode",
        replayable={"value": True, "layer": "control_runtime", "reason": "test"},
        incident={"t_start_s": 1.0, "t_end_s": 1.5, "code": "observation_freeze", "detected_by": "divergence"},
        conditions={
            "ep0": {
                "label_ko": "에피소드 0",
                "params": {"source": "lerobot:~/fake_ds_for_report_test", "episode": 0},
                "judgement_basis": "physical",
            }
        },
        grid={"spawn_x_offset_mm": [0], "spawn_y_offset_mm": [0], "repeat_points": 0, "repeats": 0},
    )


def _write_replay_run(runs: Path, *, real: Path | None = None, ghost: Path | None = None, n: int = 30) -> Path:
    run = runs / "replay-fixture"
    run.mkdir(parents=True)
    t = [round(i / 10, 3) for i in range(n)]
    series = {
        "t": t,
        "joint_err_deg": [[0.2 * i if 10 <= i <= 15 else 0.1, 0.0, 0.0, 0.3, 0.1] for i in range(n)],
        "tcp_mm": [25.0 if 10 <= i <= 15 else 2.0 for i in range(n)],
    }
    doc = {
        "schema": "robot-ops-replay/1",
        "run_id": "replay-fixture",
        "source": {"path": "~/fake_ds_for_report_test", "episode": 0, "sha256": "a" * 64},
        # 실제 replay_result처럼 개인 절대경로가 섞여 있어도 빌드 산출물에는 남지 않아야 한다.
        "trajectory_path": str(Path.home() / "fake_ds_for_report_test/trajectory.jsonl"),
        "video_path": str(ghost) if ghost else f"/data/{current_user()}/nowhere/ghost.mp4",
        "convert_video": {
            "path": str(real) if real else f"/data/{current_user()}/nowhere/cam.mp4",
            "camera": "third_person",
            "label_ko": "실측 3인칭 카메라",
            "height": 480,
            "source": {"path": str(Path.home() / "fake_ds_for_report_test/videos/x.mp4"), "sha256": "b" * 64},
        },
        "divergence": {
            "joint_names": ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"],
            "thresholds": {"joint_deg": 3.0, "tcp_mm": 20.0},
            "tcp": {"max_mm": 25.0, "first_exceed_s": 1.0, "calibration_suspect": False},
            "incidents": [
                {"kind": "freeze", "frame_start": 3, "frame_end": 4, "n_frames": 2, "t_start_s": 0.3, "t_end_s": 0.4},
                {"kind": "freeze", "frame_start": 10, "frame_end": 15, "n_frames": 6, "t_start_s": 1.0, "t_end_s": 1.5},
            ],
            "series": series,
        },
        "warnings": [f"{Path.home()}/somewhere 경고"],
        "created_at": "2026-09-27T00:00:00+0900",
    }
    (run / "replay_result.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (run / "mujoco_result.json").write_text(
        json.dumps({"tool_sha256": {"sim_core.py": "c" * 64}, "physics_steps": 0, "ghost": {"alpha": 0.4}}),
        encoding="utf-8",
    )
    return run


class ReportBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        save_scenario(self.root, make_scenario(scenario_id="cup-a"))
        save_scenario(
            self.root,
            make_scenario(
                scenario_id="drive-sample",
                task="drive_kinematic",
                status="after_fix_regression",
                model_note="not_reproducible_in_model",
                sources=[{"path": str(Path.home() / "bimanual-robot/logs/x.json"), "sha256": "0" * 64, "role": "incident_run"}],
            ),
        )
        for sid in ("cup-a", "drive-sample"):
            out = self.root / "artifacts" / "runs" / f"rg-{sid}"
            out.mkdir(parents=True)
            (out / "regress_result.json").write_text(
                json.dumps({"scenario_id": sid, "created_at": "2026-09-27T00:00:00", "conditions": {}})
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @property
    def out(self) -> Path:
        return self.root / "report"

    def _data(self) -> tuple[list, str]:
        data = self.out / "data"
        cards = json.loads((data / "scenarios.json").read_text())
        blob = "".join(p.read_text() for p in sorted(data.rglob("*.json")))
        return cards, blob

    def _json(self, rel: str):
        return json.loads((self.out / rel).read_text(encoding="utf-8"))

    def _use_fixture(self) -> None:
        run = self.root / "artifacts" / "runs" / "fixture-regress"
        run.mkdir(parents=True)
        shutil.copyfile(FIXTURE, run / "regress_result.json")

    # --- 공개/비공개 -----------------------------------------------------------------

    def test_public_build_excludes_after_fix_regression_entirely(self) -> None:
        summary = build(self.profile, media=False)
        cards, blob = self._data()
        self.assertEqual([c["scenario_id"] for c in cards], ["cup-a"])
        self.assertEqual(summary["build"], "public")
        self.assertNotIn("drive-sample", blob)
        self.assertNotIn("after_fix_regression", blob)
        self.assertFalse((self.out / "data" / "regress" / "drive-sample.json").exists())
        self.assertFalse((self.out / "data" / "scenario" / "drive-sample.json").exists())
        for path in self.out.rglob("*"):
            if path.is_file():
                text = path.read_bytes()
                self.assertNotIn(b"drive-sample", text, path)
                self.assertNotIn(b"after_fix_regression", text, path)

    def test_private_includes_all_and_preserves_model_note(self) -> None:
        build(self.profile, private=True, media=False, out_dir=self.out)
        cards, _ = self._data()
        by_id = {c["scenario_id"]: c for c in cards}
        self.assertEqual(set(by_id), {"cup-a", "drive-sample"})
        self.assertEqual(by_id["drive-sample"]["status"], "after_fix_regression")
        self.assertEqual(by_id["drive-sample"]["model_note"], "not_reproducible_in_model")
        self.assertTrue(by_id["drive-sample"]["sources"][0]["path"].startswith("~/"))
        self.assertTrue((self.out / "data" / "scenario" / "drive-sample.json").exists())
        self.assertEqual(self._json("data/build.json")["build"], "private")

    def test_private_default_output_is_separate_folder(self) -> None:
        build(self.profile, media=False)
        summary = build(self.profile, private=True, media=False)
        private_out = self.root / "report-private"
        self.assertTrue(summary["out_dir"].endswith("report-private"))
        self.assertTrue((private_out / "data" / "scenario" / "drive-sample.json").exists())
        # 공개 폴더는 비공개 빌드에 덮이지 않는다.
        self.assertEqual(self._json("data/build.json")["build"], "public")
        self.assertFalse((self.out / "data" / "scenario" / "drive-sample.json").exists())

    def test_public_build_fails_when_excluded_id_leaks(self) -> None:
        # 공개 카드 본문에 제외 시나리오 id가 섞이면 빌드가 실패해야 한다.
        save_scenario(self.root, make_scenario(scenario_id="cup-a", title_ko="drive-sample 참고"))
        with self.assertRaisesRegex(RuntimeError, "제외 시나리오 흔적"):
            build(self.profile, media=False)

    def test_public_build_fails_when_excluded_status_leaks(self) -> None:
        save_scenario(self.root, make_scenario(scenario_id="cup-a", title_ko="after_fix_regression 메모"))
        with self.assertRaisesRegex(RuntimeError, "제외 시나리오 흔적"):
            build(self.profile, media=False)

    def test_public_rebuild_removes_private_leftovers(self) -> None:
        build(self.profile, private=True, media=False, out_dir=self.out)
        build(self.profile, media=False)
        _, blob = self._data()
        self.assertNotIn("drive-sample", blob)

    def test_regress_derived_status_excludes_drive_scenario(self) -> None:
        save_scenario(self.root, make_scenario(scenario_id="drive-sample", task="drive_kinematic", status="active"))
        out = self.root / "artifacts" / "runs" / "rg-drive-sample" / "regress_result.json"
        out.write_text(json.dumps({
            "scenario_id": "drive-sample", "created_at": "2026-09-27T01:00:00", "conditions": {},
            "scenario_status": {"status": "after_fix_regression", "model_note": None},
        }))
        build(self.profile, media=False)
        _, blob = self._data()
        self.assertNotIn("drive-sample", blob)

    # --- 경로 정규화 -----------------------------------------------------------------

    def test_no_private_absolute_paths_in_any_built_file(self) -> None:
        save_scenario(self.root, _lerobot_scenario())
        _write_replay_run(self.root / "artifacts" / "runs")
        self._use_fixture()
        build(self.profile, private=True, media=False, out_dir=self.out)
        self.assertEqual(check_no_private_paths(self.out), [])
        for path in self.out.rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                for prefix in PRIVATE:
                    self.assertNotIn(prefix.encode(), blob, path)
        detail = self._json("data/scenario/freeze-a.json")
        items = {i["label_ko"]: i for i in detail["views"][0]["provenance"]["items"]}
        self.assertEqual(items["궤적(trajectory/1)"]["path"], "~/fake_ds_for_report_test/trajectory.jsonl")
        self.assertTrue(items["고스트 렌더"]["path"].startswith("/data/$USER/"))

    # --- 데이터 스키마 ---------------------------------------------------------------

    def test_data_schema_sanity(self) -> None:
        save_scenario(self.root, _lerobot_scenario())
        _write_replay_run(self.root / "artifacts" / "runs")
        build(self.profile, media=False)
        build_doc = self._json("data/build.json")
        for key in ("schema", "build", "profile", "scenario_count", "regress_count", "hero_source", "built_at"):
            self.assertIn(key, build_doc)
        cards = self._json("data/scenarios.json")
        for card in cards:
            for key in ("scenario_id", "title_ko", "origin", "judgement_basis", "assembly", "task", "status",
                        "replayable", "poster", "has_regress", "sources"):
                self.assertIn(key, card)
        view = self._json("data/scenario/freeze-a.json")["views"][0]
        self.assertEqual(view["condition"], "ep0")
        self.assertEqual(len(view["series"]["t"]), 30)
        self.assertEqual(len(view["series"]["joint_err_deg"][0]), 5)
        self.assertEqual(len(view["series"]["tcp_mm"]), 30)
        # 대표 사고 = 가장 긴 동결 구간
        self.assertEqual((view["incident"]["t_start_s"], view["incident"]["t_end_s"]), (1.0, 1.5))
        self.assertEqual(view["metrics"]["incident_t_s"], 1.0)
        self.assertAlmostEqual(view["metrics"]["incident_joint_err_deg"], 3.0)
        self.assertEqual(view["metrics"]["max_tcp_mm"], 25.0)
        self.assertIn("robot-ops replay --profile profiles/fake.toml --source lerobot:~/fake_ds_for_report_test --episode 0 --ghost",
                      view["provenance"]["reproduce"])
        oos = self._json("data/out_of_scope.json")
        self.assertEqual(len(oos), len(OUT_OF_SCOPE))
        self.assertTrue(all({"title_ko", "reason_ko"} <= set(o) for o in oos))

    def test_unmeasured_cup_grid_shows_no_numbers(self) -> None:
        # setUp의 cup-a 결과는 칸이 없는 부분 결과다 → 측정 전
        build(self.profile, media=False)
        hero = self._json("data/hero.json")
        cup = hero["cup_grid"]
        self.assertFalse(cup["measured"])
        for cond in cup["conditions"].values():
            self.assertNotIn("pass", cond)
            self.assertNotIn("valid", cond)
        self.assertIsNone(hero["source"])

    def test_fixture_regress_result_feeds_eval_and_hero_numbers(self) -> None:
        validate_regress_result(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self._use_fixture()
        build(self.profile, media=False)
        reg = self._json("data/regress/cup-a.json")
        self.assertTrue(reg["complete"])
        self.assertEqual(reg["grid"], {"x_mm": [-5, -2.5, 0, 2.5, 5], "y_mm": [-5, -2.5, 0, 2.5, 5]})
        self.assertEqual(len(reg["conditions"]["A"]["cells"]), 25)
        cup = self._json("data/hero.json")["cup_grid"]
        self.assertTrue(cup["measured"])
        self.assertEqual((cup["conditions"]["A"]["pass"], cup["conditions"]["A"]["valid"]), (14, 25))
        self.assertEqual((cup["conditions"]["B"]["pass"], cup["conditions"]["B"]["valid"]), (24, 24))
        self.assertEqual(cup["grid"], {"points": 25, "range_mm": 5.0})
        card = next(c for c in self._json("data/scenarios.json") if c["scenario_id"] == "cup-a")
        self.assertTrue(card["has_regress"])

    def test_grid_patterns_from_data(self) -> None:
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        g = [-5.0, -2.5, 0.0, 2.5, 5.0]
        a, b = doc["conditions"]["A"], doc["conditions"]["B"]
        self.assertEqual(failed_lines(a, g, g), ([-5.0, -2.5], []))
        self.assertEqual(
            grid_patterns(a, g, g),
            ["y −5 mm 행 5점 전부 조기 접촉", "y −2.5 mm 행 5점 전부 조기 접촉", "그 밖 실패 1점(조기 접촉)"],
        )
        self.assertEqual(grid_patterns(b, g, g), ["실패 없음"])
        self._use_fixture()
        build(self.profile, media=False)
        reg = self._json("data/regress/cup-a.json")
        self.assertEqual(reg["conditions"]["A"]["failed_rows_mm"], [-5.0, -2.5])
        self.assertEqual(self._json("data/hero.json")["cup_grid"]["measured"], True)

    def test_partial_and_record_results_do_not_replace_complete_grid(self) -> None:
        self._use_fixture()
        runs = self.root / "artifacts" / "runs"
        later = {"scenario_id": "cup-a", "created_at": "2026-09-28T00:00:00",
                 "conditions": {"A": {"cells": [{"x_mm": 0, "y_mm": 0, "status": "ok", "verdict": "pass"}]}}}
        (runs / "partial").mkdir()
        (runs / "partial" / "regress_result.json").write_text(json.dumps(later))
        record = dict(later, mode="record", created_at="2026-09-29T00:00:00")
        (runs / "record").mkdir()
        (runs / "record" / "regress_result.json").write_text(json.dumps(record))
        self.assertEqual(latest_regress_results(self.profile.artifacts_root)["cup-a"]["regress_id"], "fixture-regress")

    def test_assets_are_self_contained(self) -> None:
        build(self.profile, media=False)
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", html)
        self.assertIn('lang="ko"', html)
        for name in ("index.html", "app.js", "style.css"):
            text = (self.out / name).read_text(encoding="utf-8")
            for bad in ('src="http', "href=\"http", "url(http", "@import", "fetch(\"http", "<script>"):
                self.assertNotIn(bad, text, name)

    def test_invalid_hero_source_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(self.profile, media=False, hero_source="banner")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and mediacmd.find_cjk_font(), "ffmpeg·CJK 글꼴 없음"
)
class ReportMediaBuildTest(unittest.TestCase):
    """합성 영상으로 실제 인코딩까지: 나란히·히어로·통과 지도·포스터."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = write_profile(self.root)
        save_scenario(self.root, make_scenario(scenario_id="cup-a"))
        save_scenario(self.root, _lerobot_scenario())
        src = self.root / "src"
        src.mkdir()
        self.real = self._synth(src / "real.mp4", "testsrc", "352x288", 3)
        self.ghost = self._synth(src / "ghost.mp4", "testsrc2", "640x480", 3)
        runs = self.root / "artifacts" / "runs"
        _write_replay_run(runs, real=self.real, ghost=self.ghost)
        fixture = runs / "fixture-regress"
        fixture.mkdir()
        shutil.copyfile(FIXTURE, fixture / "regress_result.json")
        for cond in ("A", "B"):
            cell_run = runs / f"fixture-regress_{cond}_x0_y-5"
            cell_run.mkdir()
            self._synth(cell_run / "cup_contact.mp4", "smptebars", "640x400", 4)
            (cell_run / "run.response.json").write_text(json.dumps({"sim_time_s": 3.6, "video_path": None}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _synth(path: Path, pattern: str, size: str, seconds: int) -> Path:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=10:duration={seconds}",
             "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
        return path

    def _probe(self, path: Path) -> dict:
        out = mediacmd.run_media_command(mediacmd.ffprobe_command(path)).stdout
        mediacmd.run_media_command(mediacmd.decode_check_command(path))
        return json.loads(out)

    def test_media_build_auto_prefers_cup_grid_hero(self) -> None:
        summary = build(self.profile)
        out = self.root / "report"
        hero = json.loads((out / "data" / "hero.json").read_text())
        self.assertEqual(summary["hero_source"], "cup_grid")
        self.assertEqual(hero["point"], {"x_mm": 0.0, "y_mm": -5.0})
        probe = self._probe(out / hero["video"])
        stream = probe["streams"][0]
        self.assertEqual((stream["codec_name"], stream["width"], stream["height"], stream["pix_fmt"]),
                         ("h264", 1200, 600, "yuv420p"))
        # A 실패 4초 + 판정 순간 정지 1초, B 통과 4초(마지막 6초 이내), 통과 지도 5.5초
        self.assertAlmostEqual(float(probe["format"]["duration"]), 14.5, delta=0.3)
        self.assertEqual(hero["pattern_ko"]["A"][0], "y −5 mm 행 5점 전부 조기 접촉")
        self.assertTrue((out / "media" / "cup-a--passmap.png").exists())
        reg = json.loads((out / "data" / "regress" / "cup-a.json").read_text())
        cell = next(c for c in reg["conditions"]["A"]["cells"] if (c["x_mm"], c["y_mm"]) == (0, -5))
        self.assertEqual(cell["video"], "media/cup-a--A--x0--y-5.mp4")
        view = json.loads((out / "data" / "scenario" / "freeze-a.json").read_text())["views"][0]
        for key in ("real", "ghost"):
            self.assertTrue((out / view[key]["src"]).exists())
            self.assertTrue((out / view[key]["poster"]).exists())
            self.assertEqual(self._probe(out / view[key]["src"])["streams"][0]["height"], 480)
        self.assertEqual(check_no_private_paths(out), [])
        for mp4 in (out / "media").glob("*.mp4"):
            self.assertLessEqual(mp4.stat().st_size, 6 * 1024 * 1024)

    def test_media_build_freeze_hero_and_unused_media_removed(self) -> None:
        build(self.profile)
        summary = build(self.profile, hero_source="observation_freeze")
        out = self.root / "report"
        self.assertEqual(summary["hero_source"], "observation_freeze")
        hero = json.loads((out / "data" / "hero.json").read_text())
        stream = self._probe(out / hero["video"])["streams"][0]
        self.assertEqual((stream["width"], stream["height"]), (586 + 640, 480))  # 352×288 → 높이 480
        self.assertEqual(hero["incident"]["t_start_s"], 1.0)
        # 공개 빌드에서 시나리오가 빠지면 그 미디어도 지운다
        (self.root / "scenarios" / "cup-a" / "scenario.json").unlink()
        build(self.profile, hero_source="observation_freeze")
        self.assertFalse(any((out / "media").glob("cup-a--*")))


if __name__ == "__main__":
    unittest.main()
