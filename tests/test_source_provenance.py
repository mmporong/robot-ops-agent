from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.cup_cause_experiment import validate_case_source  # noqa: E402
from robot_ops.replay.source_provenance import (  # noqa: E402
    require_same_tool_manifest,
    validate_tool_sources,
)


def _write(path: Path, content: bytes = b"source\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


class SourceProvenanceTest(unittest.TestCase):
    def test_accepts_explicit_new_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = _write(root / "src/manipulation/cup_contact_model.py")
            plan = {
                "tool_sha256": {"cup_contact_model.py": digest},
                "tool_source_paths": {"cup_contact_model.py": "src/manipulation/cup_contact_model.py"},
            }

            self.assertEqual(
                validate_tool_sources(root, plan),
                {"cup_contact_model.py": "src/manipulation/cup_contact_model.py"},
            )

    def test_accepts_legacy_tools_and_simulation_layouts(self) -> None:
        for folder in ("tools", "simulation"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                digest = _write(root / folder / "simulate_cup_contact.py")
                plan = {"tool_sha256": {"simulate_cup_contact.py": digest}}

                self.assertEqual(
                    validate_tool_sources(root, plan),
                    {"simulate_cup_contact.py": f"{folder}/simulate_cup_contact.py"},
                )

    def test_rejects_invalid_or_empty_explicit_map_without_legacy_fallback(self) -> None:
        invalid = (None, {}, [], {"other.py": "simulation/tool.py"})
        for mapping in invalid:
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                digest = _write(root / "simulation/tool.py")
                plan = {"tool_sha256": {"tool.py": digest}, "tool_source_paths": mapping}
                with self.assertRaisesRegex(ValueError, "정확히 대응"):
                    validate_tool_sources(root, plan)

    def test_rejects_basename_mismatch_and_unsafe_paths(self) -> None:
        for relative, message in (
            ("src/manipulation/other.py", "basename"),
            ("../tool.py", "상대경로"),
            ("/tmp/tool.py", "상대경로"),
            ("src//tool.py", "상대경로"),
            ("src\\tool.py", "상대경로"),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan = {
                    "tool_sha256": {"tool.py": "0" * 64},
                    "tool_source_paths": {"tool.py": relative},
                }
                with self.assertRaisesRegex(ValueError, message):
                    validate_tool_sources(root, plan)

    def test_rejects_symlink_parent_and_file(self) -> None:
        for link_file in (False, True):
            with self.subTest(link_file=link_file), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "source"
                outside = Path(tmp) / "outside"
                digest = _write(outside / "tool.py")
                if link_file:
                    (root / "src").mkdir(parents=True)
                    (root / "src/tool.py").symlink_to(outside / "tool.py")
                else:
                    root.mkdir()
                    (root / "src").symlink_to(outside, target_is_directory=True)
                plan = {
                    "tool_sha256": {"tool.py": digest},
                    "tool_source_paths": {"tool.py": "src/tool.py"},
                }
                with self.assertRaisesRegex(ValueError, "심링크"):
                    validate_tool_sources(root, plan)

    def test_rejects_nonregular_file_and_tampered_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "src/tool.py"
            fifo.parent.mkdir()
            os.mkfifo(fifo)
            plan = {
                "tool_sha256": {"tool.py": "0" * 64},
                "tool_source_paths": {"tool.py": "src/tool.py"},
            }
            with self.assertRaisesRegex(ValueError, "일반 파일"):
                validate_tool_sources(root, plan)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "src/tool.py", b"changed")
            plan = {
                "tool_sha256": {"tool.py": hashlib.sha256(b"original").hexdigest()},
                "tool_source_paths": {"tool.py": "src/tool.py"},
            }
            with self.assertRaisesRegex(ValueError, "해시"):
                validate_tool_sources(root, plan)

    def test_rejects_malformed_hash_manifest(self) -> None:
        invalid = (
            {}, {"tool.py": "A" * 64}, {"path/tool.py": "0" * 64}, {"tool.py": 1},
            {"..": "0" * 64}, {"tool\\name.py": "0" * 64}, {"tool\0.py": "0" * 64},
        )
        for hashes in invalid:
            with self.subTest(hashes=hashes), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "실행 도구 해시"):
                    validate_tool_sources(Path(tmp), {"tool_sha256": hashes})

    def test_case_source_checks_tools_and_both_config_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_sha = _write(root / "src/manipulation/tool.py")
            config_path = root / "config/simulation/cup_contact_experiment.json"
            config_sha = _write(config_path, json.dumps({"test": True}).encode())
            plan = {
                "tool_sha256": {"tool.py": tool_sha},
                "tool_source_paths": {"tool.py": "src/manipulation/tool.py"},
                "source_config_sha256": config_sha,
                "config_sha256": config_sha,
            }
            validate_case_source(root, plan)
            for key in ("source_config_sha256", "config_sha256"):
                forged = dict(plan)
                forged[key] = "0" * 64
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "설정 해시"):
                    validate_case_source(root, forged)

    def test_each_run_must_keep_baseline_hashes_and_explicit_paths(self) -> None:
        baseline = {
            "tool_sha256": {"tool.py": "0" * 64},
            "tool_source_paths": {"tool.py": "src/tool.py"},
        }
        require_same_tool_manifest(dict(baseline), baseline)
        for changed in (
            {**baseline, "tool_sha256": {"tool.py": "1" * 64}},
            {**baseline, "tool_source_paths": {"tool.py": "simulation/tool.py"}},
            {"tool_sha256": dict(baseline["tool_sha256"])},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "원천 경로"):
                require_same_tool_manifest(changed, baseline)


if __name__ == "__main__":
    unittest.main()
