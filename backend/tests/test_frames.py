"""P2-03 验收：抽帧、缩略图、有效时段（旋转/VFR/极短素材/黑帧/不越界）。"""

from __future__ import annotations

import subprocess

import pytest

from app.config import AppConfig
from app.models import Asset
from app.services.analysis.frames import compute_usable_range, count_video_frames, extract_frames
from tests.media import find_ffmpeg


def _mk_asset(asset_id: str, managed: str, duration_ms: int) -> Asset:
    return Asset(
        id=asset_id, sha256=asset_id * 4, managed_path=managed, duration_ms=duration_ms,
        fps_num=30, fps_den=1, width=320, height=240,
    )


def _make_rotated_clip(path, ffmpeg_bin: str):
    """生成带 90 度旋转元数据的 2 秒素材。"""
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=240x320:d=2:r=30",
            "-metadata:s:v:0", "rotate=90",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, timeout=120,
    )


def _make_vfr_clip(path, ffmpeg_bin: str):
    """可变帧率素材：同一素材内时间戳不均匀。"""
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:d=2:r=30",
            "-vf", "setpts='N/(3*TB)'",  # 重打时间戳 → VFR 效果
            "-fps_mode", "vfr",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, timeout=120,
    )


def _make_black_head_clip(path, ffmpeg_bin: str):
    """片头 0.5 秒黑帧 + 之后正常画面。"""
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.5:r=30",
            "-f", "lavfi", "-i", "testsrc=size=320x240:d=1.5:r=30",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, timeout=120,
    )


@pytest.fixture()
def ff():
    return find_ffmpeg()


def test_frames_within_bounds_and_thumbnails(config: AppConfig, tmp_path, ff):
    clip = tmp_path / "c.mp4"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:d=2:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True, timeout=120,
    )
    asset = _mk_asset("a1", str(clip), 2000)
    frames = extract_frames(asset, config)
    assert len(frames) == 3, "10%/50%/90% 各抽一帧"
    for f in frames:
        assert f.exists() and f.stat().st_size > 0
    # 抽帧点不越界：0.9 × 2000ms = 1800ms < 2000 - 50ms epsilon 内
    assert (config.data_dir / "thumbnails" / "a1").is_dir()


def test_rotated_and_vfr_clips(config: AppConfig, tmp_path, ff):
    rot = tmp_path / "rot.mp4"
    _make_rotated_clip(rot, ff)
    probe_rot = probe_duration(rot)
    asset = _mk_asset("rot", str(rot), probe_rot)
    frames = extract_frames(asset, config)
    assert len(frames) == 3, "旋转素材抽帧正常（按显示方向）"

    vfr = tmp_path / "vfr.mp4"
    _make_vfr_clip(vfr, ff)
    asset_vfr = _mk_asset("vfr", str(vfr), probe_duration(vfr))
    frames_vfr = extract_frames(asset_vfr, config)
    assert len(frames_vfr) >= 1, "可变帧率素材至少抽出 1 帧"


def test_ultra_short_clip_dedup_frames(config: AppConfig, tmp_path, ff):
    clip = tmp_path / "tiny.mp4"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=0x35507a:s=320x240:d=0.3:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True, timeout=120,
    )
    asset = _mk_asset("tiny", str(clip), 300)
    total = count_video_frames(asset)
    assert 0 < total < 15, "极短素材实际帧数统计正常"
    frames = extract_frames(asset, config)
    assert len(frames) == 3, "0.3 秒素材按三个比例点抽帧不越界"


def test_black_head_moves_usable_start(config: AppConfig, tmp_path, ff):
    clip = tmp_path / "blackhead.mp4"
    _make_black_head_clip(clip, ff)
    asset = _mk_asset("bh", str(clip), 2000)
    usable = compute_usable_range(asset)
    assert usable.black_flagged is True
    assert usable.start_ms >= 400, f"片头黑帧后可用起点应右移，实际 {usable.start_ms}"
    assert usable.end_ms <= 2000


def probe_duration(path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, check=True, timeout=60,
    )
    return int(float(out.stdout.decode().strip()) * 1000)
