"""媒体探测与文件去重（计划 4.1 / P2-01 / P2-02）。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError


@dataclass
class ProbeResult:
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    rotation: int
    has_audio: bool
    codec: str


def probe_video(path: Path, ffprobe: str = "ffprobe") -> ProbeResult:
    """FFprobe 获取时长、尺寸、帧率、编码、旋转与音轨（计划 4.1 第 1 条）。"""
    if not path.exists():
        raise ValidationError(f"文件不存在: {path}")
    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=60)
    except subprocess.CalledProcessError as exc:
        raise ValidationError(f"无法解码文件: {exc.stderr.decode(errors='replace')[:200]}") from exc
    data = json.loads(out.stdout.decode("utf-8", errors="replace"))
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValidationError("无视频轨")
    duration_s = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    avg, den = (video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1").split("/")
    num = int(avg) if avg else 0
    den = int(den) if den else 1
    rotation = 0
    side = video.get("side_data_list") or []
    for entry in side:
        if "rotation" in entry:
            rotation = int(entry["rotation"])
    return ProbeResult(
        duration_ms=round(duration_s * 1000),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps_num=num,
        fps_den=den,
        rotation=rotation,
        has_audio=any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        codec=str(video.get("codec_name", "")),
    )


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """流式计算 SHA-256（大文件不全量读入内存，计划 10）。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def copy_to_managed(path: Path, managed_dir: Path, asset_id: str) -> Path:
    """复制进托管目录，检查目标空间（计划 10：托管复制前检查空间）。"""
    managed_dir.mkdir(parents=True, exist_ok=True)
    dest = managed_dir / f"{asset_id}{path.suffix.lower()}"
    free = shutil.disk_usage(managed_dir).free
    if free < path.stat().st_size * 1.1:
        raise ValidationError("磁盘空间不足，无法托管复制")
    shutil.copy2(path, dest)
    return dest
