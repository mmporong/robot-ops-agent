from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_support  # noqa: E402,F401

from robot_ops.replay.artifacts import (  # noqa: E402
    DeletionRefused,
    InsufficientDisk,
    check_free_disk,
    delete_artifact,
    delete_recorded_matches,
    load_manifest,
    matches_pattern,
    write_manifest,
)

PATTERNS = ("scene.usda", "frames/*.png")


class ArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.run = self.root / "runs" / "r1"
        (self.run / "frames").mkdir(parents=True)
        (self.run / "scene.usda").write_text("usd")
        (self.run / "frames" / "0001.png").write_bytes(b"png")
        (self.run / "result.json").write_text("{}")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _delete(self, target: Path) -> None:
        delete_artifact(target, root=self.root, run_dir=self.run, patterns=PATTERNS)

    def test_manifest_records_files(self) -> None:
        manifest = write_manifest(self.run, run_id="r1", grid_offset={"x_mm": 2.5, "y_mm": -5})
        self.assertEqual(
            sorted(f["path"] for f in manifest["files"]),
            ["frames/0001.png", "result.json", "scene.usda"],
        )
        entry = next(f for f in manifest["files"] if f["path"] == "result.json")
        self.assertEqual(entry["size"], 2)
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(load_manifest(self.run)["grid_offset"], {"x_mm": 2.5, "y_mm": -5})

    def test_pattern_matching_is_exact_depth(self) -> None:
        self.assertTrue(matches_pattern("scene.usda", PATTERNS))
        self.assertTrue(matches_pattern("frames/a.png", PATTERNS))
        self.assertFalse(matches_pattern("sub/scene.usda", PATTERNS))
        self.assertFalse(matches_pattern("frames/x/a.png", PATTERNS))
        self.assertFalse(matches_pattern("frames/a.jpg", PATTERNS))

    def test_recorded_pattern_files_deleted(self) -> None:
        write_manifest(self.run, run_id="r1")
        deleted = delete_recorded_matches(self.run, root=self.root, patterns=PATTERNS)
        self.assertEqual(sorted(deleted), ["frames/0001.png", "scene.usda"])
        self.assertTrue((self.run / "result.json").exists())
        self.assertFalse((self.run / "scene.usda").exists())

    def test_refuses_pattern_mismatch_and_unrecorded(self) -> None:
        write_manifest(self.run, run_id="r1")
        with self.assertRaisesRegex(DeletionRefused, "외부 경로 삭제 거부"):
            self._delete(self.run / "result.json")
        (self.run / "frames" / "late.png").write_bytes(b"new")
        with self.assertRaisesRegex(DeletionRefused, "manifest 미기록"):
            self._delete(self.run / "frames" / "late.png")
        self.assertTrue((self.run / "frames" / "late.png").exists())

    def test_refuses_modified_file(self) -> None:
        write_manifest(self.run, run_id="r1")
        (self.run / "scene.usda").write_text("changed")
        with self.assertRaises(DeletionRefused):
            self._delete(self.run / "scene.usda")

    def test_refuses_outside_root(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "scene.usda").write_text("keep")
        with self.assertRaisesRegex(DeletionRefused, "root 밖"):
            delete_artifact(outside / "scene.usda", root=self.root, run_dir=outside, patterns=PATTERNS)
        with self.assertRaises(DeletionRefused):
            self._delete(self.run / ".." / ".." / ".." / "outside" / "scene.usda")
        self.assertTrue((outside / "scene.usda").exists())

    def test_refuses_symlinked_file_and_directory(self) -> None:
        outside = self.base / "outside"
        (outside / "frames").mkdir(parents=True)
        (outside / "frames" / "0002.png").write_bytes(b"keep")
        (outside / "scene.usda").write_text("keep")
        # 1) run 폴더 안 frames가 바깥을 가리키는 심링크
        link_run = self.root / "runs" / "r2"
        link_run.mkdir(parents=True)
        os.symlink(outside / "frames", link_run / "frames")
        (link_run / "manifest.json").write_text(
            '{"files": [{"path": "frames/0002.png", "size": 4, "sha256": "x", "deleted": false}]}'
        )
        with self.assertRaisesRegex(DeletionRefused, "심링크"):
            delete_artifact(link_run / "frames" / "0002.png", root=self.root, run_dir=link_run, patterns=PATTERNS)
        # 2) 파일 자체가 심링크
        os.symlink(outside / "scene.usda", link_run / "scene.usda")
        with self.assertRaisesRegex(DeletionRefused, "심링크"):
            delete_artifact(link_run / "scene.usda", root=self.root, run_dir=link_run, patterns=PATTERNS)
        # 3) run 폴더 자체가 심링크
        os.symlink(outside, self.root / "runs" / "r3")
        with self.assertRaisesRegex(DeletionRefused, "심링크"):
            delete_artifact(self.root / "runs" / "r3" / "scene.usda", root=self.root,
                            run_dir=self.root / "runs" / "r3", patterns=PATTERNS)
        self.assertTrue((outside / "frames" / "0002.png").exists())
        self.assertTrue((outside / "scene.usda").exists())

    def test_manifest_skips_symlinks(self) -> None:
        os.symlink(self.base, self.run / "escape")
        manifest = write_manifest(self.run, run_id="r1")
        self.assertNotIn("escape", [f["path"] for f in manifest["files"]])

    def test_free_disk_check(self) -> None:
        check_free_disk(self.root / "not" / "yet", 0)
        with mock.patch("robot_ops.replay.artifacts.free_disk_gb", return_value=1.0), \
                self.assertRaises(InsufficientDisk):
            check_free_disk(self.root, 5)


if __name__ == "__main__":
    unittest.main()
