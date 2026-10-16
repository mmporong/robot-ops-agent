from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_support import write_profile  # noqa: E402

from robot_ops.agent_demo import SCHEMA, TOOLS, verified_trace  # noqa: E402
from robot_ops.replay.artifacts import sha256_file  # noqa: E402
from robot_ops.replay import media  # noqa: E402
from robot_ops.replay.report import _agent_hero, build  # noqa: E402


class VerifiedTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = write_profile(self.root)
        self.trajectory = self.root / "trajectory.jsonl"
        self.ghost = self.root / "ghost.mp4"
        self.audit = self.root / "audit.jsonl"
        self.responses = self.root / "responses.json"
        self.trace_path = self.root / "trace.json"
        self.readme = self.root / "README.md"
        self.trajectory.write_text('{"time_s": 0}\n', encoding="utf-8")
        self.ghost.write_bytes(b"recorded-ghost-video")
        self.readme.write_text("# 공개 Robot Ops Agent 문서\n", encoding="utf-8")
        self.incident = {
            "kind": "freeze",
            "frame_start": 44,
            "frame_end": 69,
            "n_frames": 26,
            "t_start_s": 4.4,
            "t_end_s": 6.9,
        }
        self.receipts = []
        self.steps = []
        arguments = [
            {"query": "명령 관측 동결 기록", "k": 2},
            {"path": str(self.trajectory), "lines": 2},
            {"path": str(self.trajectory)},
        ]
        for index, tool in enumerate(TOOLS):
            trace_id = f"trace-{index}"
            duration = float(index + 1)
            self.receipts.extend([
                {"layer": "mcp", "trace_id": trace_id, "tool": tool, "phase": "started",
                 "arguments": arguments[index], "timestamp": f"2026-10-16T00:00:0{index}Z"},
                {"layer": "mcp", "trace_id": trace_id, "tool": tool, "phase": "completed",
                 "ok": True, "status": "ok", "duration_ms": duration,
                 "timestamp": f"2026-10-16T00:00:1{index}Z"},
            ])
            self.steps.append({
                "tool": tool,
                "arguments": arguments[index],
                "trace_id": trace_id,
                "status": "ok",
                "duration_ms": duration,
                "summary_ko": "위조된 설명",
                "private_debug": "공개 결과에 포함되면 안 됨",
            })
        self.steps[0]["citations"] = [{"path": "forged.md", "sha256": "f" * 64}]
        self.audit.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.receipts),
            encoding="utf-8",
        )
        self.responses.write_text(json.dumps([
            {"tool": "search_documents", "result": [{"path": "README.md"}, {"path": "README.md"}]},
            {"tool": "log_tail", "result": ["frame 1", "frame 2"]},
            {"tool": "analyze_trajectory", "result": {
                "frame_count": 248,
                "source_sha256": sha256_file(self.trajectory),
                "incidents": [self.incident],
                "tcp": {"max_mm": 170.0},
            }},
        ]), encoding="utf-8")
        self.trace = {
            "schema": SCHEMA,
            "scenario_id": "freeze-a",
            "condition": "teleop_bench_ep0",
            "question_ko": "명령은 움직이는데 관측 기록이 멈춘 구간을 찾아줘",
            "recorded_at": "2026-10-16T00:00:00+00:00",
            "execution": "actual_mcp_calls",
            "retrieval_method": "keyword",
            "source": {
                "trajectory_path": str(self.trajectory),
                "trajectory_sha256": sha256_file(self.trajectory),
                "ghost_sha256": sha256_file(self.ghost),
                "run_id": "run-1",
            },
            "steps": self.steps,
            "analysis": {"frame_count": 248, "incident": self.incident, "max_tcp_mm": 170.0},
            "audit": {"path": str(self.audit), "sha256": sha256_file(self.audit)},
            "responses": {"path": str(self.responses), "sha256": sha256_file(self.responses)},
            "note_ko": "실제 MCP 도구 실행 기록 재생",
            "private_debug": "공개 결과에 포함되면 안 됨",
        }
        self.view = {
            "condition": "teleop_bench_ep0",
            "run_id": "run-1",
            "incident": self.incident,
            "provenance": {"items": [
                {"label_ko": "궤적(trajectory/1)", "path": str(self.trajectory),
                 "sha256": sha256_file(self.trajectory)},
                {"label_ko": "고스트 렌더", "path": str(self.ghost), "sha256": sha256_file(self.ghost)},
            ]},
        }
        self._write_trace()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_trace(self) -> None:
        self.trace_path.write_text(json.dumps(self.trace, ensure_ascii=False), encoding="utf-8")

    def test_returns_trace_when_execution_and_sources_are_verified(self) -> None:
        result = verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

        self.assertEqual(result["execution"], "actual_mcp_calls")
        self.assertEqual([step["tool"] for step in result["steps"]], list(TOOLS))
        self.assertEqual(result["steps"][0]["summary_ko"], "공개 문서에서 관련 근거 1건 검색")
        self.assertEqual(result["steps"][0]["citations"], [
            {"path": "README.md", "sha256": sha256_file(self.readme)}
        ])
        self.assertEqual(result["steps"][1]["summary_ko"], "저장된 궤적 로그의 마지막 2프레임 읽기")
        self.assertEqual(
            result["steps"][2]["summary_ko"],
            "관측 동결 4.4–6.9초 · 26프레임 검출",
        )
        self.assertNotIn("private_debug", result)
        self.assertTrue(all("private_debug" not in step for step in result["steps"]))

    def test_rejects_trace_with_wrong_schema(self) -> None:
        self.trace["schema"] = "robot-ops-agent-demo/forged"
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "실제 MCP 실행 기록 형식"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_trace_for_another_condition(self) -> None:
        self.trace["condition"] = "another-condition"
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "영상 사례가 다릅니다"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_trace_for_another_run(self) -> None:
        self.trace["source"]["run_id"] = "run-forged"
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "재생 실행 ID"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_changed_trajectory_source(self) -> None:
        self.trajectory.write_text('{"time_s": 1}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "영상 출처 해시"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_changed_ghost_source(self) -> None:
        self.ghost.write_bytes(b"changed-video")

        with self.assertRaisesRegex(ValueError, "영상 출처 해시"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_mcp_receipt_without_started_completed_pair(self) -> None:
        forged = [row for row in self.receipts if not (
            row["tool"] == "log_tail" and row["phase"] == "started"
        )]
        self.audit.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in forged), encoding="utf-8"
        )
        self.trace["audit"]["sha256"] = sha256_file(self.audit)
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "시작·완료 감사 쌍"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_displayed_arguments_that_disagree_with_started_receipt(self) -> None:
        self.trace["steps"][1]["arguments"]["lines"] = 200
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "arguments|호출 인자"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_trajectory_hash_that_disagrees_with_analysis_response(self) -> None:
        responses = json.loads(self.responses.read_text(encoding="utf-8"))
        responses[2]["result"]["source_sha256"] = "f" * 64
        self.responses.write_text(json.dumps(responses), encoding="utf-8")
        self.trace["responses"]["sha256"] = sha256_file(self.responses)
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "source_sha256|궤적.*해시|원본.*해시"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_responses_in_wrong_tool_order(self) -> None:
        responses = json.loads(self.responses.read_text(encoding="utf-8"))
        responses[0], responses[1] = responses[1], responses[0]
        self.responses.write_text(json.dumps(responses), encoding="utf-8")
        self.trace["responses"]["sha256"] = sha256_file(self.responses)
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "응답.*순서|도구.*순서"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)

    def test_rejects_analysis_that_disagrees_with_recorded_response(self) -> None:
        self.trace["analysis"]["frame_count"] = 247
        self._write_trace()

        with self.assertRaisesRegex(ValueError, "도구 응답·재생 영상"):
            verified_trace(self.trace_path, self.profile, "freeze-a", self.view)


class AgentDemoMediaCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.demo = {
            "question_ko": "관측 기록이 멈춘 구간을 찾아줘",
            "steps": [
                {"tool": tool, "duration_ms": float(index + 1), "trace_id": f"trace-{index}",
                 **({"citations": [{"path": "README.md", "sha256": "a" * 64}]} if index == 0 else {})}
                for index, tool in enumerate(TOOLS)
            ],
            "analysis": {
                "frame_count": 248,
                "incident": {"t_start_s": 4.4, "t_end_s": 6.9, "n_frames": 26},
                "max_tcp_mm": 170.0,
            },
        }

    def test_keeps_simulation_visible_while_showing_real_tool_results(self) -> None:
        argv = media.agent_demo_command(
            Path("real.mp4"), Path("ghost.mp4"), Path("hero.mp4"), demo=self.demo,
            length_s=12.0, font_path="/f.ttc",
        )

        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("[0:v]scale=960:720", graph)
        self.assertIn("[2:v][g]overlay=24:128:shortest=1", graph)
        self.assertIn("실제 MCP 실행 기록 재생", graph)
        for tool in TOOLS:
            self.assertIn(tool, graph)
        self.assertIn("248프레임 분석", graph)
        self.assertIn("동결 4.4–6.9 s · 26프레임", graph)
        self.assertEqual(argv[argv.index("-t") + 1], "12")

    def test_rejects_invalid_demo_length(self) -> None:
        for length in (9.0, 0.0, math.nan):
            with self.subTest(length=length):
                with self.assertRaisesRegex(ValueError, "9초보다 길어야"):
                    media.agent_demo_command(
                        Path("real.mp4"), Path("ghost.mp4"), Path("hero.mp4"), demo=self.demo,
                        length_s=length, font_path="/f.ttc",
                    )


class _FakeMediaBuilder:
    def __init__(self, root: Path) -> None:
        self.enabled = True
        self.font = "/f.ttc"
        self.dir = root / "media"
        self.dir.mkdir()
        self.warnings: list[str] = []

    def make(self, *args, **kwargs):
        raise AssertionError("검증 실패한 trace로 미디어를 생성하면 안 됩니다")


class AgentHeroFallbackTest(unittest.TestCase):
    def test_returns_none_and_warning_when_public_trace_is_forged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile(root, extra='''
[report.agent_demo]
trace = "trace.json"
''')
            (root / "trace.json").write_text(json.dumps({"schema": "forged"}), encoding="utf-8")
            view = {"condition": "ep0", "run_id": "run-1", "provenance": {"items": []}}
            builder = _FakeMediaBuilder(root)

            hero = _agent_hero(profile, {"scenario_id": "freeze-a"}, view, builder)

        self.assertIsNone(hero)
        self.assertEqual(len(builder.warnings), 1)
        self.assertIn("Agent 실행 기록 영상을 사용할 수 없습니다", builder.warnings[0])

    def test_auto_build_fails_when_configured_agent_media_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile(root, extra='''
[report.agent_demo]
trace = "missing-trace.json"
''')

            with self.assertRaisesRegex(ValueError, "agent_replay 영상을 만들 수 없습니다"):
                build(profile, out_dir=root / "report-auto", media=True, hero_source="auto")

    def test_explicit_agent_build_fails_when_configured_media_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile(root, extra='''
[report.agent_demo]
trace = "missing-trace.json"
''')

            with self.assertRaisesRegex(ValueError, "agent_replay 영상을 만들 수 없습니다"):
                build(profile, out_dir=root / "report-agent", media=True, hero_source="agent_replay")


if __name__ == "__main__":
    unittest.main()
