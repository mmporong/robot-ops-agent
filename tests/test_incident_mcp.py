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


class NoCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        self.calls.append(tuple(argv))
        return ProcessResult(0, "", "", 0.0)


@unittest.skipIf(Client is None, "mcp optional dependency is not installed")
class IncidentMCPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from robot_ops.mcp_server import create_server

        self.temp_dir = tempfile.TemporaryDirectory()
        self.outside_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.audit_log = self.root / "audit.jsonl"
        self.runner = NoCommandRunner()
        service = DiagnosticToolService(
            ToolPolicy(
                audit_log=self.audit_log,
                allowed_roots=(self.root,),
                max_output_chars=20_000,
            ),
            self.runner,
        )
        self.server = create_server(service)

    async def asyncTearDown(self) -> None:
        self.outside_dir.cleanup()
        self.temp_dir.cleanup()

    def _write_trajectory(
        self,
        path: Path,
        *,
        observation_follows_command: bool = False,
    ) -> Path:
        header = {
            "schema": "robot-ops-trajectory/1",
            "robot_id": "so101",
            "arm": "left",
            "joint_names": ["shoulder"],
            "units": "rad",
            "fps": 10,
            "source": {
                "kind": "fixture",
                "path": "incident-fixture",
                "episode": 0,
                "sha256": "0" * 64,
            },
            "origin": "real_recording",
        }
        rows = []
        for index in range(8):
            command = math.radians(index * 1.2)
            rows.append(
                {
                    "t": index / 10,
                    "q_cmd": [command],
                    "q_obs": [command if observation_follows_command else 0.0],
                }
            )
        path.write_text(
            "\n".join(json.dumps(item) for item in [header, *rows]) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_events(
        self,
        path: Path,
        trajectory: Path,
        *,
        trajectory_sha256: str | None = None,
    ) -> Path:
        document = {
            "schema": "robot-ops-incident-events/1",
            "trajectory_sha256": trajectory_sha256
            or hashlib.sha256(trajectory.read_bytes()).hexdigest(),
            "time_basis": "trajectory",
            "events": [
                {
                    "t": 0.4,
                    "level": "error",
                    "message": "controller reported a stored fault",
                }
            ],
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _audit_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
        ]

    async def test_investigate_recording_returns_bounded_review_and_audits(self) -> None:
        path = self._write_trajectory(self.root / "incident.jsonl")

        async with Client(self.server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            result = await client.call_tool(
                "investigate_recording", {"path": str(path)}
            )

        self.assertIn("investigate_recording", {tool.name for tool in listed.tools})
        self.assertFalse(result.is_error)
        self.assertEqual(self.runner.calls, [])
        review = result.structured_content
        self.assertEqual(review["schema"], "robot-ops-incident-review/1")
        self.assertEqual(review["cause_status"], "undetermined")
        self.assertLessEqual(len(review["windows"]), 3)
        self.assertEqual(
            review["source"],
            {
                "label": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        encoded = json.dumps(review, ensure_ascii=False)
        self.assertLessEqual(len(encoded), 20_000)
        self.assertNotIn('"rows"', encoded)
        self.assertNotIn('"series"', encoded)

        matching = [
            record
            for record in self._audit_records()
            if record.get("tool") == "investigate_recording"
        ]
        self.assertEqual(
            [(record["layer"], record["phase"]) for record in matching],
            [
                ("mcp", "started"),
                ("core", "started"),
                ("core", "completed"),
                ("mcp", "completed"),
            ],
        )
        self.assertTrue(all(record.get("ok", True) for record in matching))

    async def test_investigate_recording_blocks_path_outside_allowlist_and_audits(self) -> None:
        outside = self._write_trajectory(
            Path(self.outside_dir.name) / "outside.jsonl"
        )

        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "investigate_recording", {"path": str(outside)}
            )

        self.assertTrue(result.is_error)
        self.assertEqual(self.runner.calls, [])
        matching = [
            record
            for record in self._audit_records()
            if record.get("tool") == "investigate_recording"
        ]
        self.assertEqual(
            [(record["layer"], record["phase"]) for record in matching],
            [
                ("mcp", "started"),
                ("core", "started"),
                ("core", "completed"),
                ("mcp", "completed"),
            ],
        )
        self.assertEqual(matching[2]["status"], "rejected")
        self.assertEqual(matching[3]["status"], "tool_error")

    async def test_investigate_recording_uses_aligned_logs_and_after_recording(self) -> None:
        before = self._write_trajectory(self.root / "before.jsonl")
        after = self._write_trajectory(
            self.root / "after.jsonl",
            observation_follows_command=True,
        )
        events = self._write_events(self.root / "events.json", before)

        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "investigate_recording",
                {
                    "path": str(before),
                    "events_path": str(events),
                    "after_path": str(after),
                },
            )

        self.assertFalse(result.is_error)
        self.assertEqual(self.runner.calls, [])
        review = result.structured_content
        self.assertEqual(
            review["source"]["events_sha256"],
            hashlib.sha256(events.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            review["comparison"]["after_sha256"],
            hashlib.sha256(after.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            review["comparison"]["status"],
            "warnings_removed_in_scope",
        )
        self.assertTrue(
            any(
                check["id"] == "inspect_reported_error"
                for check in review["next_checks"]
            )
        )
        self.assertTrue(any(window["logs"] for window in review["windows"]))

    async def test_optional_evidence_paths_keep_the_same_read_boundary(self) -> None:
        trajectory = self._write_trajectory(self.root / "incident.jsonl")
        wrong_hash = self._write_events(
            self.root / "wrong-hash.json",
            trajectory,
            trajectory_sha256="f" * 64,
        )
        outside_events = self._write_events(
            Path(self.outside_dir.name) / "outside-events.json",
            trajectory,
        )
        real_after = self._write_trajectory(
            self.root / "real-after.jsonl",
            observation_follows_command=True,
        )
        linked_after = self.root / "linked-after.jsonl"
        linked_after.symlink_to(real_after)

        cases = (
            {"path": str(trajectory), "events_path": str(wrong_hash)},
            {"path": str(trajectory), "events_path": str(outside_events)},
            {"path": str(trajectory), "after_path": str(linked_after)},
        )
        async with Client(self.server, raise_exceptions=True) as client:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool(
                        "investigate_recording",
                        arguments,
                    )
                    self.assertTrue(result.is_error)

        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
