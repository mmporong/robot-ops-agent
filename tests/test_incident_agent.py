from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import Client
except ImportError:
    Client = None

from robot_ops.incident_agent import investigate_via_mcp
from robot_ops.incident_review import read_input, review_path, review_rows
from test_incident_review import header, samples, save


class IncidentSnapshotTests(unittest.TestCase):
    def test_events_output_collision_does_not_overwrite_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, events = root / "before.jsonl", root / "incident.json"
            save(path, samples())
            events.write_text("preserve original")
            with self.assertRaises(ValueError):
                review_path(path, root, events_path=events)
            self.assertEqual(events.read_text(), "preserve original")

    def test_output_hardlink_and_symlink_preserve_input(self):
        import os
        for link in ("hard", "symbolic"):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path, out = root / "before.jsonl", root / "report"
                save(path, samples())
                original = path.read_bytes()
                out.mkdir()
                if link == "hard": os.link(path, out / "incident.json")
                else: (out / "incident.json").symlink_to(path)
                with self.assertRaises(ValueError): review_path(path, out)
                self.assertEqual(path.read_bytes(), original)

    def test_changed_trajectory_after_tool_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "before.jsonl"
            save(path, samples())
            h, rows, digest = read_input(path)
            result = review_rows(h, rows)
            result["source"] = {"sha256": digest}
            save(path, samples(False))
            with self.assertRaisesRegex(ValueError, "MCP"):
                review_path(path, Path(temp) / "report", tool_review=result)


@unittest.skipIf(Client is None, "mcp optional dependency is not installed")
class IncidentAgentTests(unittest.TestCase):
    def test_audit_output_collision_is_rejected_before_append(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "mcp-audit.jsonl"
            save(path, samples())
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                asyncio.run(investigate_via_mcp(path, root))
            self.assertEqual(path.read_bytes(), original)

    def test_three_real_mcp_calls_include_logs_comparison_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before, after, events = root / "before.jsonl", root / "after.jsonl", root / "events.json"
            save(before, samples())
            save(after, samples(False))
            digest = read_input(before)[2]
            events.write_text(json.dumps({"schema": "robot-ops-incident-events/1", "trajectory_sha256": digest,
                                          "time_basis": "trajectory", "events": [{"t": 0.7, "level": "error", "message": "read timeout"}]}))
            result = asyncio.run(investigate_via_mcp(before, root / "report", events_path=events, after_path=after))
            self.assertEqual(result["execution"]["tool_calls"], 3)
            self.assertEqual(result["next_checks"][0]["id"], "inspect_reported_error")
            self.assertEqual(result["comparison"]["status"], "warnings_removed_in_scope")
            self.assertEqual(result["comparison"]["causal_effect"], "not_assessed")
            self.assertFalse(result["execution"]["hardware_accessed"])
            audit = [json.loads(line) for line in (root / "report/mcp-audit.jsonl").read_text().splitlines()]
            self.assertEqual(len(audit), 12)
            self.assertTrue((root / "report/index.html").exists())

    def test_minimal_record_uses_single_actual_mcp_call(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.jsonl"
            save(path, samples())
            result = asyncio.run(investigate_via_mcp(path, Path(temp) / "report"))
            self.assertEqual(result["execution"]["tool_calls"], 1)
            self.assertEqual(result["trace"][0]["tool"], "mcp:investigate_recording")


if __name__ == "__main__":
    unittest.main()
