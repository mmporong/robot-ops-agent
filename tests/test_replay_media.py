from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_support  # noqa: E402,F401

from robot_ops.replay import media  # noqa: E402


class MediaCommandTest(unittest.TestCase):
    def test_h264_args(self) -> None:
        self.assertEqual(
            media.h264_args(10),
            ["-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
             "-g", "10", "-movflags", "+faststart"],
        )

    def test_even_dimensions(self) -> None:
        self.assertEqual(media.scale_filter(480), "scale=-2:480:flags=lanczos")
        with self.assertRaises(ValueError):
            media.scale_filter(481)

    def test_side_by_side_graph(self) -> None:
        argv = media.side_by_side_command(
            Path("a.mp4"), Path("b.mp4"), Path("o.mp4"), fps=10,
            labels=("실측 손목 카메라", "명령(반투명)·관측"),
            font_path="/usr/share/fonts/NotoSansCJK-Regular.ttc",
            incident=(2.5, 4.0), duration_s=12.0,
        )
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("hstack=inputs=2", graph)
        self.assertIn("text='실측 손목 카메라'", graph)
        self.assertIn("fontfile='/usr/share/fonts/NotoSansCJK-Regular.ttc'", graph)
        self.assertIn("enable='between(t,2.5,4)'", graph)
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", graph)
        self.assertEqual(argv[argv.index("-map") + 1], "[v]")
        self.assertEqual(argv[-1], "o.mp4")
        self.assertIn("-g", argv)

    def test_labels_require_font(self) -> None:
        with self.assertRaises(ValueError):
            media.side_by_side_command(Path("a"), Path("b"), Path("o"), fps=10, labels=("a", "b"))

    def test_drawtext_escaping(self) -> None:
        self.assertEqual(media.escape_drawtext("a:b%c'd"), "a\\:b\\%c’d")

    def test_poster_and_probe(self) -> None:
        argv = media.poster_command(Path("v.mp4"), Path("p.webp"), at_s=1.5)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libwebp")
        self.assertEqual(argv[argv.index("-quality") + 1], "80")
        self.assertEqual(argv[argv.index("-ss") + 1], "1.5")
        self.assertEqual(media.ffprobe_command(Path("v.mp4"))[0], "ffprobe")

    def test_font_discovery_uses_fc_match(self) -> None:
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "")

        self.assertTrue(media.find_cjk_font(fake).endswith("NotoSansCJK-Regular.ttc"))
        self.assertEqual(calls[0][:3], ["fc-match", "-f", "%{file}"])

        def fallback(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "/usr/share/fonts/DejaVuSans.ttf", "")

        self.assertIsNone(media.find_cjk_font(fallback))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg 없음")
    def test_real_encode_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            media.run_media_command(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=351x287:rate=10:duration=1", str(src)]
            )
            out = Path(tmp) / "out.mp4"
            media.run_media_command(media.transcode_command(src, out, fps=10, height=240))
            probe = media.run_media_command(media.ffprobe_command(out)).stdout
            self.assertIn('"codec_name": "h264"', probe)
            self.assertIn('"height": 240', probe)
            media.run_media_command(media.decode_check_command(out))


if __name__ == "__main__":
    unittest.main()
