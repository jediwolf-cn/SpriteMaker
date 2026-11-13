"""ffmpeg 封装：视频信息探测 + 切帧。

只依赖 tools/ffmpeg.exe（或 PATH 里的 ffmpeg），不需要 ffprobe。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from . import config


class FFmpegError(RuntimeError):
    pass


def _noop(_: str) -> None:
    pass


def executable() -> Path:
    exe = config.ffmpeg_exe()
    if not exe.exists():
        raise FFmpegError(f"找不到 ffmpeg：{exe}（请检查 config.json 的 ffmpeg 项）")
    return exe


def run(args: list[str], *, log: Callable[[str], None] = _noop) -> subprocess.CompletedProcess:
    cmd = [str(executable()), "-hide_banner", "-loglevel", "error", "-y", *args]
    log("ffmpeg " + " ".join(args))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe(video: Path) -> dict:
    """用 ffmpeg -i 的 stderr 解析时长 / 尺寸 / 帧率（无 ffprobe 时的兜底）。"""
    cmd = [str(executable()), "-hide_banner", "-i", str(video)]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (cp.stderr or "") + (cp.stdout or "")
    info: dict = {"path": str(video), "duration": 0.0, "width": 0, "height": 0, "fps": 24.0}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m[1]), int(m[2])
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        info["fps"] = float(m[1])
    return info


def extract_frames(video: Path, out_dir: Path, *, fps: float = 0, scale: int = 0,
                   start_number: int = 0, prefix: str = "frame_",
                   log: Callable[[str], None] = _noop) -> list[Path]:
    """把视频切成 PNG 序列。fps=0 表示按原始帧率全切。"""
    if not video.exists():
        raise FFmpegError(f"找不到视频：{video}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}*.png"):
        old.unlink()
    filters = []
    if fps and fps > 0:
        filters.append(f"fps={fps}")
    if scale and scale > 0:
        filters.append(f"scale={scale}:{scale}:flags=lanczos")
    args = ["-i", str(video)]
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-vsync", "0", "-start_number", str(start_number),
             "-pix_fmt", "rgba", str(out_dir / f"{prefix}%04d.png")]
    cp = run(args, log=log)
    if cp.returncode != 0:
        raise FFmpegError(f"ffmpeg 切帧失败：{cp.stderr[-800:]}")
    frames = sorted(out_dir.glob(f"{prefix}*.png"))
    if not frames:
        raise FFmpegError("ffmpeg 没有切出任何帧，请检查视频文件")
    log(f"切帧完成：{len(frames)} 帧 → {out_dir.name}/")
    return frames
