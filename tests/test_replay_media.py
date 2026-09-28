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

    def test_incident_label_uses_incident_box_color(self) -> None:
        font = "/usr/share/fonts/NotoSansCJK-Regular.ttc"
        self.assertIn("boxcolor=black@0.55", media.drawtext_filter("a", font))
        label = media.incident_label_filter("관측 동결", font, 1.0, 2.0)
        self.assertIn("boxcolor=0xC9432B@0.9", label)
        self.assertNotIn("black@0.55", label)
        self.assertTrue(label.endswith(":enable='between(t,1,2)'"))

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

    def test_trimmed_side_by_side_shifts_incident_and_progress(self) -> None:
        argv = media.side_by_side_command(
            Path("a.mp4"), Path("b.mp4"), Path("o.mp4"), fps=10,
            labels=("실측 3인칭 카메라", "명령(반투명)·관측"), font_path="/f.ttc",
            incident=(3.4, 5.9), incident_label="관측 동결 구간", start_s=1.0, length_s=18.0,
        )
        self.assertEqual(argv[argv.index("-ss") + 1], "1")
        self.assertEqual(argv.count("-ss"), 2)
        self.assertEqual(argv.count("-t"), 2)
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("w='iw*t/18'", graph)
        self.assertIn("text='관측 동결 구간'", graph)
        self.assertIn("drawbox=x=0:y=0:w=iw:h=40", graph)

    def test_clip_command(self) -> None:
        argv = media.clip_command(Path("s.mp4"), Path("o.mp4"), fps=10, start_s=2.0, length_s=5.0, crf=26)
        self.assertEqual(argv[argv.index("-crf") + 1], "26")
        self.assertIn("scale=-2:480:flags=lanczos", argv[argv.index("-vf") + 1])
        with self.assertRaises(ValueError):
            media.clip_command(Path("s"), Path("o"), fps=10, label="x")

    def test_hero_sequence_command(self) -> None:
        argv = media.hero_sequence_command(
            [
                {"path": "a.mp4", "start_s": 1.5, "length_s": 5, "label": "조건 A", "highlight": (4.6, 5.0),
                 "highlight_label": "조기 접촉 판정"},
                {"path": "b.mp4", "start_s": 1.5, "length_s": 5, "label": "조건 B"},
                {"path": "map.png", "image": True, "length_s": 5.5, "fade_in": True},
            ],
            Path("hero.mp4"), fps=10, font_path="/f.ttc",
        )
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("concat=n=3:v=1:a=0", graph)
        self.assertIn("pad=1200:600", graph)
        self.assertIn("enable='between(t,4.6,5)'", graph)
        self.assertIn("fade=t=in", graph)
        self.assertIn("w='iw*t/15.5'", graph)
        self.assertIn("-loop", argv)
        with self.assertRaises(ValueError):
            media.hero_sequence_command([], Path("o"), fps=10, font_path="/f")

    def test_hero_sequence_detail_line_during_highlight(self) -> None:
        argv = media.hero_sequence_command(
            [{"path": "a.mp4", "start_s": 1.5, "length_s": 5, "label": "조건 A", "highlight": (4.6, 5.0),
              "highlight_label": "조기 접촉 판정 6.425 s",
              "highlight_detail": "고정 죠 패드 0.90 N 접촉 · 컵 수평 이동 0.02 mm"}],
            Path("hero.mp4"), fps=10, font_path="/f.ttc",
        )
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("text='고정 죠 패드 0.90 N 접촉 · 컵 수평 이동 0.02 mm'", graph)
        self.assertEqual(graph.count("enable='between(t,4.6,5)'"), 3)  # 테두리·판정 라벨·수치 줄

    def test_pass_map_footer_and_axis_titles(self) -> None:
        grid = [-5, -2.5, 0, 2.5, 5]
        argv = media.pass_map_command(
            [{"title": "A", "subtitle": "", "x_mm": grid, "y_mm": grid, "cells": {},
              "x_title": "x 오프셋 (mm, + = 로봇 앞)", "y_title": "y (mm, + = 로봇 왼쪽)"}],
            Path("m.png"), font_path="/f.ttc", footer="y −5 mm 행만 실패",
        )
        chain = argv[argv.index("-vf") + 1]
        self.assertIn("text='x 오프셋 (mm, + = 로봇 앞)'", chain)
        self.assertIn("text='y (mm, + = 로봇 왼쪽)'", chain)
        self.assertIn("text='y −5 mm 행만 실패'", chain)
        self.assertIn("drawbox=x=0:y=548:w=1200:h=52:color=0x161B22:t=fill", chain)

    def test_color_bbox_and_roi(self) -> None:
        w, h = 200, 100
        buf = bytearray(b"\xc8" * (w * h * 3))  # 회색 배경
        for y in range(40, 80):
            for x in range(60, 80):
                i = (y * w + x) * 3
                buf[i:i + 3] = bytes((230, 210, 60))  # 노란 컵
        bbox = media.color_bbox(bytes(buf), w, h, media.is_cup_yellow, step=1)
        self.assertEqual(bbox, (60, 40, 79, 79))
        roi = media.roi_around(bbox, (w, h))
        rw, rh, rx, ry = roi
        self.assertTrue(rw % 2 == 0 and rh % 2 == 0 and rx >= 0 and ry >= 0)
        self.assertLessEqual(rx + rw, w)
        self.assertLessEqual(ry + rh, h)
        self.assertLessEqual(rx, 60)  # 컵이 ROI 안에 들어온다
        self.assertGreaterEqual(rx + rw, 79)
        self.assertIsNone(media.color_bbox(bytes(w * h * 3), w, h, media.is_cup_yellow))
        self.assertEqual(media.crop_filter((524, 262, 444, 318)), "crop=524:262:444:318")
        with self.assertRaises(ValueError):
            media.crop_filter((523, 262, 0, 0))
        argv = media.clip_command(Path("s.mp4"), Path("o.mp4"), fps=10, crop=(524, 262, 444, 318))
        self.assertTrue(argv[argv.index("-vf") + 1].startswith("crop=524:262:444:318,scale=-2:480"))

    def test_pass_map_outlines_failed_row(self) -> None:
        grid = [-5, -2.5, 0, 2.5, 5]
        argv = media.pass_map_command(
            [{"title": "A", "subtitle": "", "x_mm": grid, "y_mm": grid, "cells": {}, "outline_rows": [-5.0]}],
            Path("m.png"), font_path="/f.ttc",
        )
        self.assertEqual(argv[argv.index("-vf") + 1].count("color=0x161B22:t=4"), 1)

    def test_pass_map_command_labels_every_cell(self) -> None:
        grid = [-5, -2.5, 0, 2.5, 5]
        cells = {(float(x), float(y)): "pass" for x in grid for y in grid}
        cells[(0.0, -5.0)] = "fail"
        cells[(5.0, 5.0)] = "infra"
        argv = media.pass_map_command(
            [{"title": "조건 A", "subtitle": "통과 23/24", "x_mm": grid, "y_mm": grid, "cells": cells},
             {"title": "조건 B", "subtitle": "", "x_mm": grid, "y_mm": grid, "cells": {}}],
            Path("m.png"), font_path="/f.ttc",
        )
        chain = argv[argv.index("-vf") + 1]
        self.assertIn("color=c=0xF7F8F6:s=1200x600", " ".join(argv))
        self.assertEqual(chain.count("text='통과'"), 23)
        self.assertEqual(chain.count("text='실패'"), 1)
        self.assertEqual(chain.count("text='infra'"), 1)
        self.assertEqual(chain.count("text='측정 전'"), 25)
        with self.assertRaises(ValueError):
            media.pass_map_command([], Path("m.png"), font_path="/f")

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
