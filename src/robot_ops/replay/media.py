"""ffmpeg 명령 조립(순수 함수)과 실행 도우미.

인코딩 규칙은 robot-dashboard `core/video.py`의 H.264 `-g <fps>` 패턴을 따른다:
`libx264 -preset slow -crf 23 -pix_fmt yuv420p -g <fps> -movflags +faststart`, 짝수 크기.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

CJK_FONT_QUERY = "Noto Sans CJK KR"
DEFAULT_HEIGHT = 480


def h264_args(fps: int, *, crf: int = 23) -> list[str]:
    if fps <= 0:
        raise ValueError("fps는 양수여야 합니다")
    return [
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-g", str(int(fps)),
        "-movflags", "+faststart",
    ]


def _even_height(height: int) -> int:
    if height <= 0 or height % 2:
        raise ValueError(f"높이는 양의 짝수여야 합니다: {height}")
    return height


def scale_filter(height: int = DEFAULT_HEIGHT) -> str:
    """높이 고정 lanczos 확대·축소, 너비는 짝수(-2)."""
    return f"scale=-2:{_even_height(height)}:flags=lanczos"


EVEN_PAD_FILTER = "pad=ceil(iw/2)*2:ceil(ih/2)*2"


def find_cjk_font(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, *, bold: bool = False
) -> str | None:
    if runner is subprocess.run and not shutil.which("fc-match"):
        return None
    try:
        result = runner(
            ["fc-match", "-f", "%{file}", CJK_FONT_QUERY + (":bold" if bold else "")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = (result.stdout or "").strip()
    return path if result.returncode == 0 and path and "CJK" in Path(path).name else None


def escape_drawtext(text: str) -> str:
    """drawtext `text='...'` 값 이스케이프(\\, :, %, 작은따옴표)."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace("'", "’")
    )


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def drawtext_filter(
    text: str,
    font_path: str,
    *,
    x: str = "12",
    y: str = "12",
    size: int = 24,
    color: str = "white",
    box: bool = True,
) -> str:
    parts = [
        f"fontfile='{_escape_filter_path(font_path)}'",
        f"text='{escape_drawtext(text)}'",
        f"x={x}",
        f"y={y}",
        f"fontsize={size}",
        f"fontcolor={color}",
    ]
    if box:
        parts += ["box=1", "boxcolor=black@0.55", "boxborderw=6"]
    return "drawtext=" + ":".join(parts)


def incident_band_filter(t_start: float, t_end: float, *, height: int = 8, color: str = "0xE0452B@0.9") -> str:
    """사고 구간 동안 영상 하단에 띠를 그린다."""
    if t_end < t_start:
        raise ValueError("사고 구간 끝이 시작보다 앞섭니다")
    return (
        f"drawbox=x=0:y=ih-{height}:w=iw:h={height}:color={color}:t=fill"
        f":enable='between(t,{t_start:g},{t_end:g})'"
    )


def progress_bar_filter(duration_s: float, *, height: int = 4, color: str = "white@0.9") -> str:
    """재생 진행 막대(너비가 시간에 비례)."""
    if duration_s <= 0:
        raise ValueError("duration은 양수여야 합니다")
    return f"drawbox=x=0:y=ih-{height}:w='iw*t/{duration_s:g}':h={height}:color={color}:t=fill"


def transcode_command(src: Path, out: Path, *, fps: int, height: int = DEFAULT_HEIGHT) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"{scale_filter(height)},{EVEN_PAD_FILTER},fps={int(fps)}",
        "-an",
        *h264_args(fps),
        str(out),
    ]


def label_bar_filter(
    text: str,
    font_path: str,
    *,
    height: int = 40,
    size: int = 21,
    background: str = "0x161b22@0.74",
    color: str = "white",
) -> str:
    """영상 위쪽 전폭 라벨 바(drawbox) + 글자(drawtext)."""
    bar = f"drawbox=x=0:y=0:w=iw:h={int(height)}:color={background}:t=fill"
    return bar + "," + drawtext_filter(
        text, font_path, x="16", y=str(max(0, (int(height) - int(size)) // 2 - 1)), size=size, color=color, box=False
    )


def incident_label_filter(text: str, font_path: str, t_start: float, t_end: float, *, band_height: int = 8) -> str:
    """사고 구간 동안 하단 띠 위에 표시하는 글자."""
    return drawtext_filter(
        text, font_path, x="16", y=f"h-{band_height + 40}", size=20, color="white", box=True
    ).replace("boxcolor=black@0.55", "boxcolor=0xC9432B@0.9") + f":enable='between(t,{t_start:g},{t_end:g})'"


def _trim_input(path: Path, start_s: float | None, length_s: float | None) -> list[str]:
    argv: list[str] = []
    if start_s is not None:
        argv += ["-ss", f"{start_s:g}"]
    if length_s is not None:
        argv += ["-t", f"{length_s:g}"]
    return argv + ["-i", str(path)]


def side_by_side_command(
    left: Path,
    right: Path,
    out: Path,
    *,
    fps: int,
    height: int = DEFAULT_HEIGHT,
    labels: tuple[str, str] | None = None,
    font_path: str | None = None,
    incident: tuple[float, float] | None = None,
    incident_label: str | None = None,
    duration_s: float | None = None,
    start_s: float | None = None,
    length_s: float | None = None,
    crf: int = 23,
) -> list[str]:
    """두 영상을 같은 높이로 맞춰 hstack, 라벨 바·사고 띠·진행 막대를 얹는다.

    `start_s`/`length_s`는 두 입력을 같은 구간으로 자른다. 이때 `incident`는 잘린 영상 기준 시각이다.
    """
    scale = f"{scale_filter(height)},{EVEN_PAD_FILTER},fps={int(fps)},setsar=1"
    left_chain = f"[0:v]{scale}"
    right_chain = f"[1:v]{scale}"
    if (labels is not None or incident_label is not None) and not font_path:
        raise ValueError("라벨에는 CJK 글꼴 경로가 필요합니다")
    if labels is not None:
        left_chain += "," + label_bar_filter(labels[0], font_path)
        right_chain += "," + label_bar_filter(labels[1], font_path)
    graph = [f"{left_chain}[l]", f"{right_chain}[r]", "[l][r]hstack=inputs=2[s]"]
    tail = "[s]"
    post = []
    if duration_s is None and length_s is not None:
        duration_s = length_s
    if duration_s is not None:
        post.append(progress_bar_filter(duration_s))
    if incident is not None:
        post.append(incident_band_filter(*incident))
        if incident_label:
            post.append(incident_label_filter(incident_label, font_path, *incident))
    if post:
        graph.append(f"[s]{','.join(post)}[v]")
        tail = "[v]"
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *_trim_input(left, start_s, length_s),
        *_trim_input(right, start_s, length_s),
        "-filter_complex", ";".join(graph),
        "-map", tail,
        "-an",
        *h264_args(fps, crf=crf),
        str(out),
    ]


def clip_command(
    src: Path,
    out: Path,
    *,
    fps: int,
    height: int = DEFAULT_HEIGHT,
    start_s: float | None = None,
    length_s: float | None = None,
    label: str | None = None,
    font_path: str | None = None,
    crf: int = 23,
) -> list[str]:
    """한 영상을 구간으로 잘라 짝수 크기로 다시 인코딩(선택: 라벨 바)."""
    chain = f"{scale_filter(height)},{EVEN_PAD_FILTER},fps={int(fps)},setsar=1"
    if label is not None:
        if not font_path:
            raise ValueError("라벨에는 CJK 글꼴 경로가 필요합니다")
        chain += "," + label_bar_filter(label, font_path)
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *_trim_input(src, start_s, length_s),
        "-vf", chain,
        "-an",
        *h264_args(fps, crf=crf),
        str(out),
    ]


HERO_SIZE = (1200, 600)
HERO_BACKGROUND = "0x10151c"


def _fit_canvas(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={HERO_BACKGROUND},fps={int(fps)},setsar=1,format=yuv420p"
    )


def hero_sequence_command(
    segments: Sequence[dict],
    out: Path,
    *,
    fps: int,
    font_path: str,
    size: tuple[int, int] = HERO_SIZE,
    crf: int = 23,
) -> list[str]:
    """히어로 MP4: 구간 영상·정지 이미지를 같은 캔버스로 맞춰 이어 붙인다.

    segment = {"path", "start_s"?, "length_s", "label"?, "image"?: bool,
               "highlight"?: (a, b) 구간 기준 시각, "highlight_label"?, "fade_in"?: bool}
    """
    if not segments:
        raise ValueError("히어로 구간이 비었습니다")
    width, height = size
    if width % 2 or height % 2:
        raise ValueError("히어로 크기는 짝수여야 합니다")
    argv = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    graph: list[str] = []
    total = 0.0
    for i, seg in enumerate(segments):
        length = float(seg["length_s"])
        if length <= 0:
            raise ValueError("구간 길이는 양수여야 합니다")
        total += length
        if seg.get("image"):
            argv += ["-loop", "1", "-framerate", str(int(fps)), "-t", f"{length:g}", "-i", str(seg["path"])]
        else:
            argv += _trim_input(Path(seg["path"]), seg.get("start_s"), length)
        chain = f"[{i}:v]{_fit_canvas(width, height, fps)}"
        # 입력이 요청 길이보다 짧아도 이어 붙인 길이가 계획과 같도록 마지막 프레임을 늘린 뒤 자른다.
        chain += f",tpad=stop_mode=clone:stop_duration={length:g},trim=duration={length:g},setpts=PTS-STARTPTS"
        if seg.get("fade_in"):
            chain += ",fade=t=in:st=0:d=0.5"
        if seg.get("label"):
            chain += "," + label_bar_filter(seg["label"], font_path, height=48, size=24)
        highlight = seg.get("highlight")
        if highlight is not None:
            a, b = float(highlight[0]), float(highlight[1])
            chain += (
                f",drawbox=x=0:y=0:w=iw:h=ih:color=0xE0452B:t=10:enable='between(t,{a:g},{b:g})'"
            )
            if seg.get("highlight_label"):
                chain += "," + incident_label_filter(seg["highlight_label"], font_path, a, b, band_height=10)
        graph.append(f"{chain}[v{i}]")
    joined = "".join(f"[v{i}]" for i in range(len(segments)))
    graph.append(f"{joined}concat=n={len(segments)}:v=1:a=0[c]")
    graph.append(f"[c]{progress_bar_filter(total, height=5)}[v]")
    return [
        *argv,
        "-filter_complex", ";".join(graph),
        "-map", "[v]",
        "-an",
        *h264_args(fps, crf=crf),
        str(out),
    ]


PASS_MAP_SIZE = (1200, 600)
_MAP_STYLE = {
    "pass": ("0x2F7D5A", "white", "통과"),
    "fail": ("0xC9432B", "white", "실패"),
    "infra": ("0xC3C8CE", "0x1B1F24", "infra"),
    None: ("0xE4E7EA", "0x5A6270", "측정 전"),
}


def _fmt_mm(value: float) -> str:
    return f"{value:g}"


def pass_map_command(
    panels: Sequence[dict],
    out: Path,
    *,
    font_path: str,
    bold_font_path: str | None = None,
    size: tuple[int, int] = PASS_MAP_SIZE,
) -> list[str]:
    """5×5 통과 지도 PNG(조건 A | 조건 B)를 ffmpeg drawbox·drawtext만으로 그린다(추가 의존성 없음).

    panel = {"title", "subtitle", "x_mm": [...], "y_mm": [...], "cells": {(x, y): "pass"|"fail"|"infra"|None}}
    y는 위가 +가 되도록 그린다. 칸마다 색과 글자를 함께 쓴다(색만으로 구분하지 않음).
    """
    if not 1 <= len(panels) <= 2:
        raise ValueError("지도는 1~2장이어야 합니다")
    width, height = size
    bold = bold_font_path or font_path
    panel_w = width // len(panels)
    filters: list[str] = []
    for index, panel in enumerate(panels):
        xs = [float(v) for v in panel["x_mm"]]
        ys = sorted((float(v) for v in panel["y_mm"]), reverse=True)
        n_x, n_y = len(xs), len(ys)
        gap = 4
        cell = min((panel_w - 150 - gap * (n_x - 1)) // max(n_x, 1), (height - 210 - gap * (n_y - 1)) // max(n_y, 1))
        grid_w = cell * n_x + gap * (n_x - 1)
        grid_h = cell * n_y + gap * (n_y - 1)
        left = index * panel_w + (panel_w - grid_w) // 2 + 24
        top = 136
        base_x = index * panel_w + 48
        filters.append(drawtext_filter(panel["title"], bold, x=str(base_x), y="34", size=30, color="0x161B22", box=False))
        if panel.get("subtitle"):
            filters.append(
                drawtext_filter(panel["subtitle"], font_path, x=str(base_x), y="78", size=21, color="0x4A5260", box=False)
            )
        cells = panel["cells"]
        for row, y in enumerate(ys):
            cy = top + row * (cell + gap)
            filters.append(
                drawtext_filter(_fmt_mm(y), font_path, x=f"{left - 14}-tw", y=str(cy + cell // 2 - 10), size=18,
                                color="0x4A5260", box=False)
            )
            for col, x in enumerate(xs):
                cx = left + col * (cell + gap)
                fill, ink, word = _MAP_STYLE.get(cells.get((x, y)), _MAP_STYLE[None])
                filters.append(f"drawbox=x={cx}:y={cy}:w={cell}:h={cell}:color={fill}:t=fill")
                filters.append(
                    drawtext_filter(word, bold, x=f"{cx}+({cell}-tw)/2", y=f"{cy}+({cell}-th)/2", size=19,
                                    color=ink, box=False)
                )
        for col, x in enumerate(xs):
            cx = left + col * (cell + gap)
            filters.append(
                drawtext_filter(_fmt_mm(x), font_path, x=f"{cx}+({cell}-tw)/2", y=str(top + grid_h + 12), size=18,
                                color="0x4A5260", box=False)
            )
        filters.append(
            drawtext_filter("x 오프셋 (mm)", font_path, x=f"{left}+({grid_w}-tw)/2", y=str(top + grid_h + 42),
                            size=18, color="0x4A5260", box=False)
        )
        filters.append(
            drawtext_filter("y (mm)", font_path, x=f"{left - 14}-tw", y=str(top - 28), size=18, color="0x4A5260", box=False)
        )
    if len(panels) == 2:
        filters.append(f"drawbox=x={panel_w - 1}:y=40:w=2:h={height - 80}:color=0xD5D9DE:t=fill")
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0xF7F8F6:s={width}x{height}:d=1",
        "-vf", ",".join(filters),
        "-frames:v", "1",
        "-update", "1",
        str(out),
    ]


def image_webp_command(src: Path, out: Path, *, quality: int = 80, width: int | None = None) -> list[str]:
    argv = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if width:
        argv += ["-vf", f"scale={int(width)}:-2:flags=lanczos"]
    return [*argv, "-frames:v", "1", "-c:v", "libwebp", "-quality", str(int(quality)), str(out)]


def poster_command(video: Path, out: Path, *, at_s: float = 0.0, quality: int = 80) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at_s:g}",
        "-i", str(video),
        "-frames:v", "1",
        "-c:v", "libwebp",
        "-quality", str(int(quality)),
        str(out),
    ]


def ffprobe_command(path: Path) -> list[str]:
    return [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration,size",
        "-of", "json",
        str(path),
    ]


def decode_check_command(path: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"]


def probe_duration(path: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> float:
    result = run_media_command(ffprobe_command(path), runner=runner)
    doc = json.loads(result.stdout or "{}")
    return float(doc.get("format", {}).get("duration") or 0.0)


def run_media_command(
    argv: Sequence[str],
    *,
    timeout_s: float = 600,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    result = runner(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{argv[0]} 실패(exit={result.returncode}): {(result.stderr or '').strip()[-400:]}")
    return result
