from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    from mcp import Client
except ImportError:  # pragma: no cover - optional dependency path
    Client = None

from robot_ops.diagnostics import (  # noqa: E402
    DiagnosticToolService,
    ProcessResult,
    ToolPolicy,
)
from robot_ops.config import IndexSettings  # noqa: E402
from robot_ops.embedding import HashEmbedder  # noqa: E402
from robot_ops.indexer import sync_index  # noqa: E402


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        self.calls.append(tuple(argv))
        return ProcessResult(0, "/safe_node\n", "", 1.0)


@unittest.skipIf(Client is None, "mcp optional dependency is not installed")
class MCPServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from robot_ops.mcp_server import create_server

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runner = FakeRunner()
        self.audit_log = self.root / "audit.jsonl"
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "rotation.md").write_text(
            "제자리 회전을 위치 변화만 보는 검사기가 정지로 잘못 판단했다.",
            encoding="utf-8",
        )
        self.db_path = self.root / "index.db"
        sync_index(
            IndexSettings(
                root=self.root,
                db_path=self.db_path,
                include_dirs=("docs",),
            ),
            HashEmbedder(32),
        )
        service = DiagnosticToolService(
            ToolPolicy(
                audit_log=self.audit_log,
                allowed_roots=(self.root,),
                ros2_binary="/opt/ros/jazzy/bin/ros2",
            ),
            self.runner,
        )
        self.server = create_server(service, db_path=self.db_path)

    def _write_trajectory(self) -> Path:
        path = self.root / "trajectory.jsonl"
        header = {
            "schema": "robot-ops-trajectory/1",
            "robot_id": "so101",
            "arm": "left",
            "joint_names": ["shoulder"],
            "units": "rad",
            "fps": 10,
            "source": {
                "kind": "fixture",
                "path": "fixture",
                "episode": 0,
                "sha256": "0" * 64,
            },
            "origin": "real_recording",
        }
        rows = [
            {
                "t": index / 10,
                "q_cmd": [math.radians(index * 1.2)],
                "q_obs": [0.0],
            }
            for index in range(6)
        ]
        path.write_text(
            "\n".join(json.dumps(item) for item in [header, *rows]) + "\n",
            encoding="utf-8",
        )
        return path

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_registered_surface_contains_no_motion_tools(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            tools = await client.list_tools()

        names = {tool.name for tool in tools.tools}
        self.assertIn("ros_topic_sample", names)
        self.assertIn("search_documents", names)
        self.assertIn("analyze_trajectory", names)
        self.assertNotIn("ros_topic_publish", names)
        self.assertNotIn("shell", names)
        self.assertNotIn("restart_process", names)

    async def test_ros_nodes_returns_structured_read_only_result(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool("ros_nodes", {})

        self.assertFalse(result.is_error)
        self.assertEqual(
            self.runner.calls,
            [("/opt/ros/jazzy/bin/ros2", "node", "list")],
        )
        self.assertTrue(result.structured_content["ok"])

    async def test_analyze_trajectory_runs_through_mcp_without_command(self) -> None:
        path = self._write_trajectory()

        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "analyze_trajectory", {"path": str(path)}
            )

        self.assertFalse(result.is_error)
        self.assertEqual(self.runner.calls, [])
        summary = result.structured_content
        self.assertEqual(summary["source_path"], str(path.resolve()))
        self.assertEqual(
            summary["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(summary["frame_count"], 6)
        self.assertEqual(summary["thresholds"], {"joint_deg": 3.0, "tcp_mm": 20.0})
        self.assertNotIn("series", summary)
        records = [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [
                record["phase"]
                for record in records
                if record.get("tool") == "analyze_trajectory"
            ],
            ["started", "started", "completed", "completed"],
        )

    async def test_invalid_topic_is_tool_error_and_never_runs_command(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "ros_topic_sample", {"topic": "/tf; ros2 topic pub /cmd_vel"}
            )

        self.assertTrue(result.is_error)
        self.assertEqual(self.runner.calls, [])

    async def test_unknown_motion_tool_is_audited_at_mcp_boundary(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "ros_topic_publish",
                {"topic": "/cmd_vel", "message": "move"},
            )

        self.assertTrue(result.is_error)
        self.assertEqual(self.runner.calls, [])
        records = [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
        ]
        mcp_records = [
            record
            for record in records
            if record.get("layer") == "mcp"
            and record.get("tool") == "ros_topic_publish"
        ]
        self.assertEqual(
            [record["phase"] for record in mcp_records],
            ["started", "completed"],
        )
        self.assertEqual(mcp_records[-1]["status"], "tool_error")
        self.assertEqual(
            mcp_records[-1]["rejection_reason"],
            "tool_not_registered",
        )

    async def test_schema_rejection_is_audited_without_core_execution(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool("ros_topic_sample", {"topic": 123})

        self.assertTrue(result.is_error)
        self.assertEqual(self.runner.calls, [])
        records = [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
        ]
        matching = [
            record
            for record in records
            if record.get("layer") == "mcp"
            and record.get("tool") == "ros_topic_sample"
        ]
        self.assertEqual(matching[-1]["status"], "tool_error")
        self.assertEqual(
            matching[-1]["rejection_reason"],
            "registered_tool_error_or_schema_rejection",
        )

    async def test_search_is_audited_at_mcp_boundary(self) -> None:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "search_documents", {"query": "제자리 회전 정지 오판", "k": 1}
            )

        self.assertFalse(result.is_error)
        records = [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
        ]
        matching = [
            record
            for record in records
            if record.get("layer") == "mcp"
            and record.get("tool") == "search_documents"
        ]
        self.assertEqual(
            [record["phase"] for record in matching],
            ["started", "completed"],
        )
        self.assertTrue(matching[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
