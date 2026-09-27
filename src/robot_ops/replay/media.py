"""ffmpeg 명령 조립(순수 함수)과 실행 도우미.

인코딩 규칙은 robot-dashboard `core/video.py`의 H.264 `-g <fps>` 패턴을 따른다:
`libx264 -preset slow -crf 23 -pix_fmt yuv420p -g <fps> -movflags +faststart`, 짝수 크기.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

CJK_FONT_QUERY = "Noto Sans CJK KR"
DEFAULT_HEIGHT = 480


def h264_args(fps: int) -> list[str]:
    if fps <= 0:
        raise ValueError("fps는 양수여야 합니다")
    return [
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "23",
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


def find_cjk_font(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    if runner is subprocess.run and not shutil.which("fc-match"):
        return None
    try:
        result = runner(
            ["fc-match", "-f", "%{file}", CJK_FONT_QUERY],
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


def incident_band_filter(t_start: float, t_end: float, *, height: int = 8, color: str = "red@0.8") -> str:
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
    duration_s: float | None = None,
) -> list[str]:
    """두 영상을 같은 높이로 맞춰 hstack, 라벨·사고 띠·진행 막대를 얹는다."""
    scale = f"{scale_filter(height)},{EVEN_PAD_FILTER},fps={int(fps)},setsar=1"
    left_chain = f"[0:v]{scale}"
    right_chain = f"[1:v]{scale}"
    if labels is not None:
        if not font_path:
            raise ValueError("라벨에는 CJK 글꼴 경로가 필요합니다")
        left_chain += "," + drawtext_filter(labels[0], font_path)
        right_chain += "," + drawtext_filter(labels[1], font_path)
    graph = [f"{left_chain}[l]", f"{right_chain}[r]", "[l][r]hstack=inputs=2[s]"]
    tail = "[s]"
    post = []
    if duration_s is not None:
        post.append(progress_bar_filter(duration_s))
    if incident is not None:
        post.append(incident_band_filter(*incident))
    if post:
        graph.append(f"[s]{','.join(post)}[v]")
        tail = "[v]"
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(left),
        "-i", str(right),
        "-filter_complex", ";".join(graph),
        "-map", tail,
        "-an",
        *h264_args(fps),
        str(out),
    ]


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
