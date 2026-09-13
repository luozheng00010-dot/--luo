"""测试媒体文件生成辅助。"""

from __future__ import annotations

import subprocess
from pathlib import Path

FF_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "ffmpeg",
)


def find_ffmpeg() -> str:
    for c in FF_CANDIDATES:
        try:
            subprocess.run([c, "-version"], capture_output=True, check=True, timeout=30)
            return c
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("未找到可用的 ffmpeg")


def make_clip(path: Path, duration: float, color: str = "0x35507a", size: str = "320x240") -> Path:
    """生成无声 h264 测试素材（默认 30fps）。"""
    ff = find_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={duration}:r=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return path
