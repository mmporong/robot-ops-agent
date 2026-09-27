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
FFMPEG = ("ffmpeg", "-y", "-hide_banner", "-loglevel", "error")


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


def _scaled_chain(height: int, fps: int) -> str:
    return f"{scale_filter(height)},{EVEN_PAD_FILTER},fps={int(fps)},setsar=1"


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
    box_color: str = "black@0.55",
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
        parts += ["box=1", f"boxcolor={box_color}", "boxborderw=6"]
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
        *FFMPEG,
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


def incident_label_filter(
    text: str,
    font_path: str,
    t_start: float,
    t_end: float,
    *,
    band_height: int = 8,
    x: str = "16",
    y: str | None = None,
    size: int = 20,
) -> str:
    """사고 구간 동안 표시하는 글자(기본: 하단 띠 바로 위 왼쪽)."""
    return drawtext_filter(
        text, font_path, x=x, y=y if y is not None else f"h-{band_height + 40}", size=size, color="white",
        box=True, box_color="0xC9432B@0.9",
    ) + f":enable='between(t,{t_start:g},{t_end:g})'"


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
    scale = _scaled_chain(height, fps)
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
        *FFMPEG,
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
    crop: tuple[int, int, int, int] | None = None,
    crf: int = 23,
) -> list[str]:
    """한 영상을 구간으로 잘라 짝수 크기로 다시 인코딩(선택: 라벨 바)."""
    chain = _scaled_chain(height, fps)
    if crop is not None:
        chain = crop_filter(crop) + "," + chain
    if label is not None:
        if not font_path:
            raise ValueError("라벨에는 CJK 글꼴 경로가 필요합니다")
        chain += "," + label_bar_filter(label, font_path)
    return [
        *FFMPEG,
        *_trim_input(src, start_s, length_s),
        "-vf", chain,
        "-an",
        *h264_args(fps, crf=crf),
        str(out),
    ]


def crop_filter(crop: tuple[int, int, int, int]) -> str:
    w, h, x, y = (int(v) for v in crop)
    if w <= 0 or h <= 0 or w % 2 or h % 2 or x < 0 or y < 0:
        raise ValueError(f"crop은 양의 짝수 크기·음이 아닌 위치여야 합니다: {crop}")
    return f"crop={w}:{h}:{x}:{y}"


def frame_rgb_command(video: Path, at_s: float, *, width: int, height: int) -> list[str]:
    """한 프레임을 원본 크기 rgb24 raw로 stdout에 뽑는다."""
    return [
        "ffmpeg", "-v", "error", "-ss", f"{at_s:g}", "-i", str(video), "-frames:v", "1",
        "-vf", f"scale={int(width)}:{int(height)}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]


def is_cup_yellow(r: int, g: int, b: int) -> bool:
    """Isaac 컵 장면의 노란 컵 색(밝은 R·G, 낮은 B)."""
    return r > 170 and g > 150 and b < 110 and r - b > 90


def color_bbox(
    rgb: bytes, width: int, height: int, predicate: Callable[[int, int, int], bool], *, step: int = 2
) -> tuple[int, int, int, int] | None:
    """rgb24 버퍼에서 조건을 만족하는 픽셀의 경계 상자(x0, y0, x1, y1). 없거나 너무 적으면 None."""
    if len(rgb) < width * height * 3:
        raise ValueError("프레임 버퍼 크기가 해상도와 맞지 않습니다")
    xs: list[int] = []
    ys: list[int] = []
    for y in range(0, height, step):
        row = y * width * 3
        for x in range(0, width, step):
            i = row + x * 3
            if predicate(rgb[i], rgb[i + 1], rgb[i + 2]):
                xs.append(x)
                ys.append(y)
    if len(xs) < 20:
        return None
    xs.sort()
    ys.sort()
    # 외곽 1 % 잡음을 버린다
    k = len(xs) // 100
    return xs[k], ys[k], xs[-1 - k], ys[-1 - k]


def roi_around(
    bbox: tuple[int, int, int, int],
    frame: tuple[int, int],
    *,
    height_scale: float = 2.3,
    aspect: float = 2.0,
    x_bias: float = 0.22,
    y_bias: float = -0.15,
) -> tuple[int, int, int, int]:
    """물체 상자 주변의 고정 ROI(w, h, x, y). 짝수 크기, 프레임 안으로 자른다.

    ROI 높이 = 물체 높이 × height_scale, 너비 = 높이 × aspect. 중심은 물체 중심에서
    너비의 x_bias·높이의 y_bias만큼 옮긴다(컵 과제는 팔이 컵 오른쪽 위에서 접근).
    """
    fw, fh = frame
    x0, y0, x1, y1 = bbox
    h = min(fh, int(round((y1 - y0) * height_scale)))
    w = min(fw, int(round(h * aspect)))
    h = min(h, int(w / aspect)) if w < h * aspect else h
    w -= w % 2
    h -= h % 2
    cx = (x0 + x1) / 2 + w * x_bias
    cy = (y0 + y1) / 2 + h * y_bias
    x = int(min(max(0, cx - w / 2), fw - w))
    y = int(min(max(0, cy - h / 2), fh - h))
    return w, h, x - x % 2, y - y % 2


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

    segment = {"path", "start_s"?, "length_s", "label"?, "image"?: bool, "crop"?: (w, h, x, y),
               "highlight"?: (a, b) 구간 기준 시각, "highlight_label"?, "fade_in"?: bool}
    """
    if not segments:
        raise ValueError("히어로 구간이 비었습니다")
    width, height = size
    if width % 2 or height % 2:
        raise ValueError("히어로 크기는 짝수여야 합니다")
    argv = list(FFMPEG)
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
        chain = f"[{i}:v]"
        if seg.get("crop") is not None:
            chain += crop_filter(seg["crop"]) + ","
        chain += _fit_canvas(width, height, fps)
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
                # 재생 컨트롤이 덮지 않도록 라벨 바 아래 오른쪽에 둔다
                chain += "," + incident_label_filter(
                    seg["highlight_label"], font_path, a, b, x="w-tw-24", y="68", size=26
                )
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

    panel = {"title", "subtitle", "x_mm": [...], "y_mm": [...], "cells": {(x, y): "pass"|"fail"|"infra"|None},
             "outline_rows"?: [y...], "outline_cols"?: [x...]}
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
        # 전부 실패한 행·열은 굵은 테두리로 묶어 패턴이 한눈에 보이게 한다
        for y in panel.get("outline_rows") or []:
            row = ys.index(float(y))
            filters.append(
                f"drawbox=x={left - 6}:y={top + row * (cell + gap) - 6}:w={grid_w + 12}:h={cell + 12}:color=0x161B22:t=4"
            )
        for x in panel.get("outline_cols") or []:
            col = xs.index(float(x))
            filters.append(
                f"drawbox=x={left + col * (cell + gap) - 6}:y={top - 6}:w={cell + 12}:h={grid_h + 12}:color=0x161B22:t=4"
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
        *FFMPEG,
        "-f", "lavfi", "-i", f"color=c=0xF7F8F6:s={width}x{height}:d=1",
        "-vf", ",".join(filters),
        "-frames:v", "1",
        "-update", "1",
        str(out),
    ]


def image_webp_command(src: Path, out: Path, *, quality: int = 80, width: int | None = None) -> list[str]:
    argv = [*FFMPEG, "-i", str(src)]
    if width:
        argv += ["-vf", f"scale={int(width)}:-2:flags=lanczos"]
    return [*argv, "-frames:v", "1", "-c:v", "libwebp", "-quality", str(int(quality)), str(out)]


def poster_command(video: Path, out: Path, *, at_s: float = 0.0, quality: int = 80) -> list[str]:
    return [
        *FFMPEG,
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


def probe_video(path: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    result = run_media_command(ffprobe_command(path), runner=runner)
    doc = json.loads(result.stdout or "{}")
    stream = (doc.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration_s": float(doc.get("format", {}).get("duration") or 0.0),
    }


def find_object_roi(
    video: Path,
    at_s: float,
    *,
    predicate: Callable[[int, int, int], bool] = is_cup_yellow,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    **roi_kwargs,
) -> tuple[int, int, int, int] | None:
    """영상 한 프레임에서 물체(기본: 노란 컵)를 찾아 고정 ROI(w, h, x, y)를 돌려준다. 못 찾으면 None."""
    info = probe_video(video, runner=runner)
    width, height = info["width"], info["height"]
    if not width or not height:
        return None
    result = runner(frame_rgb_command(video, at_s, width=width, height=height), capture_output=True, timeout=60, check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    bbox = color_bbox(result.stdout, width, height, predicate)
    return roi_around(bbox, (width, height), **roi_kwargs) if bbox else None


def probe_duration(path: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> float:
    return probe_video(path, runner=runner)["duration_s"]


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
