"""抽帧、缩略图与有效时段判断（计划 4.1 第 6/7 条 / P2-03）。

- 默认在有效时长的 10%/50%/90% 抽帧；极短素材按实际可解码帧数去重抽取；
- blackdetect 标记黑帧片头片尾，产出 usable_start_ms/usable_end_ms；
- 抽帧时间不越界（min(duration - epsilon, t)）。
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConfig
from app.models import Asset

logger = logging.getLogger(__name__)

BLACKDETECT_PICTURE_THRESHOLD = 0.90  # 画面 90% 以上黑 → 黑帧
BLACKDETECT_MIN_DURATION_S = 0.05
FRAME_EPSILON_S = 0.05  # 抽帧点向内收 50ms，避免请求最后一帧越界


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def extract_frames(asset: Asset, config: AppConfig, *, fractions: tuple[float, ...] = (0.1, 0.5, 0.9),
                   ffmpeg: str = "ffmpeg") -> list[Path]:
    """按比例抽帧到 thumbnails/{asset_id}/，返回帧文件列表。"""
    src = asset.managed_path or ""
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"素材文件缺失: {src}")
    duration_s = (asset.duration_ms or 0) / 1000
    if duration_s <= 0:
        raise ValueError("素材时长无效")

    out_dir = config.data_dir / "thumbnails" / asset.id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 旋转素材：ffmpeg 默认按显示方向输出（autorotate），帧即用户所见方向
    paths: list[Path] = []
    for i, frac in enumerate(fractions):
        t = min(max(duration_s * frac, 0.0), max(duration_s - FRAME_EPSILON_S, 0.0))
        out = out_dir / f"frame_{i}_{int(frac * 100)}.jpg"
        result = _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{t:.3f}", "-i", src,
                "-frames:v", "1", "-vf", "scale=480:-2",
                str(out),
            ]
        )
        if result.returncode == 0 and out.exists():
            paths.append(out)
        else:
            logger.warning(
                "frame_extract_failed",
                extra={"asset_id": asset.id, "t": t, "err": result.stderr.decode(errors="replace")[:120]},
            )
    return paths


def detect_black_segments(asset: Asset, *, ffmpeg: str = "ffmpeg") -> list[tuple[float, float]]:
    """返回黑帧区段 [(start_s, end_s), ...]。"""
    src = asset.managed_path or ""
    if not src or not Path(src).exists():
        return []
    result = _run(
        [
            ffmpeg, "-hide_banner", "-i", src,
            "-vf",
            f"blackdetect=d={BLACKDETECT_MIN_DURATION_S}:pic_th={BLACKDETECT_PICTURE_THRESHOLD}",
            "-an", "-f", "null", "-",
        ]
    )
    segments: list[tuple[float, float]] = []
    for line in result.stderr.decode(errors="replace").splitlines():
        if "black_start" in line:
            payload = line.split("black_start:")[1]
            try:
                start_s = float(payload.split("black_end:")[0].strip())
                rest = payload.split("black_end:")[1]
                end_s = float(rest.split("black_duration:")[0].strip())
            except (ValueError, IndexError):
                continue
            segments.append((start_s, end_s))
    return segments


@dataclass
class UsableRange:
    start_ms: int
    end_ms: int
    black_flagged: bool


def compute_usable_range(asset: Asset) -> UsableRange:
    """按黑帧阈值确定可用时段（计划 4.1 第 7 条）。"""
    duration_ms = asset.duration_ms or 0
    usable_start = 0
    usable_end = duration_ms
    black_flagged = False
    for start_s, end_s in detect_black_segments(asset):
        black_flagged = True
        # 片头黑帧：可用起点右移；片尾黑帧：可用终点左移
        if start_s <= 0.2:
            usable_start = max(usable_start, int(end_s * 1000))
        if end_s >= (duration_ms / 1000) - 0.2:
            usable_end = min(usable_end, int(start_s * 1000))
    return UsableRange(start_ms=usable_start, end_ms=usable_end, black_flagged=black_flagged)


def count_video_frames(asset: Asset, *, ffprobe: str = "ffprobe") -> int:
    """统计实际可解码帧数（极短素材抽帧去重的依据）。"""
    src = asset.managed_path or ""
    if not src or not Path(src).exists():
        return 0
    result = _run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-print_format", "json", src,
        ]
    )
    try:
        data = json.loads(result.stdout.decode())
        return int(data["streams"][0]["nb_read_packets"])
    except (ValueError, KeyError, IndexError):
        return 0
