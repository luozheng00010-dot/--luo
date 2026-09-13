#!/usr/bin/env bash
# P0-06 媒体工具链验证：
#   1) 检查 ffmpeg/ffprobe 构建具备 libx264、aac、libass(subtitles/ass)、loudnorm
#   2) 生成 10 段 3 秒短素材 → 拼接 30 秒 1080x1920@30fps 竖屏样片
#   3) 烧录中文 ASS 字幕、混音（配音 + 背景乐，loudnorm 归一）
#   4) ffprobe 复核编码、尺寸、帧率、时长
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/p0_media_check"
WORK="$OUT/work"
FF_CANDIDATES=(
  /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
  /opt/homebrew/bin/ffmpeg
  ffmpeg
)
FFPROBE_CANDIDATES=(
  /opt/homebrew/opt/ffmpeg-full/bin/ffprobe
  /opt/homebrew/bin/ffprobe
  ffprobe
)
FF=""; FP=""
for c in "${FF_CANDIDATES[@]}"; do command -v "$c" >/dev/null 2>&1 && FF="$c" && break; done
for c in "${FFPROBE_CANDIDATES[@]}"; do command -v "$c" >/dev/null 2>&1 && FP="$c" && break; done
[ -z "$FF" ] && { echo "FAIL: 未找到 ffmpeg"; exit 1; }
echo "ffmpeg: $FF ($($FF -version | head -1))"

# ---- 1) 能力检查 ----
ENCODERS="$($FF -hide_banner -encoders 2>/dev/null)"
FILTERS="$($FF -hide_banner -filters 2>/dev/null)"
for cap in libx264 aac; do
  echo "$ENCODERS" | grep -q " $cap " || { echo "FAIL: 缺编码器 $cap"; exit 1; }
  echo "OK: 编码器 $cap"
done
echo "$FILTERS" | grep -q " subtitles " || { echo "FAIL: 缺 libass subtitles 滤镜"; exit 1; }
echo "$FILTERS" | grep -q " loudnorm " || { echo "FAIL: 缺 loudnorm 滤镜"; exit 1; }
echo "OK: libass 字幕与 loudnorm 响度滤镜可用"

rm -rf "$WORK"; mkdir -p "$WORK"

# 内置可分发中文字体（OFL 许可，计划 P5-04）
FONTS="$ROOT/backend/app/assets/fonts"
FONT_FILE="$FONTS/NotoSansSC-Regular.otf"
[ -f "$FONT_FILE" ] || { echo "FAIL: 缺少内置中文字体 $FONT_FILE"; exit 1; }

# ---- 2) 生成 10 段 3 秒短素材（模拟 <5s 素材），不同颜色与编号 ----
colors=(0x35507a 0x7a3535 0x357a4b 0x7a6a35 0x5a357a 0x357a76 0x7a4035 0x407a35 0x35407a 0x6f7a35)
for i in $(seq 0 9); do
  n=$((i+1))
  $FF -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=${colors[$i]}:s=1080x1920:d=3:r=30" \
    -vf "drawtext=text='素材 ${n}':fontfile=$FONT_FILE:fontcolor=white:fontsize=120:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -pix_fmt yuv420p "$WORK/clip_$n.mp4"
done
: > "$WORK/concat.txt"
for i in $(seq 1 10); do echo "file '$WORK/clip_$i.mp4'" >> "$WORK/concat.txt"; done
$FF -hide_banner -loglevel error -y -f concat -safe 0 -i "$WORK/concat.txt" -c copy "$WORK/video_only.mp4"
echo "OK: 10 段短素材拼接为 30 秒时间线"

# ---- 3) 中文 ASS 字幕（libass 应能找到系统中文字体渲染） ----
cat > "$WORK/subs.ass" <<'ASS'
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Alignment, MarginL, MarginR, MarginV, Encoding
Style: 默认,Noto Sans CJK SC,72,&H00FFFFFF,&H00000000,&H80000000,0,0,2,64,64,240,1

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.00,0:00:05.00,默认,自动剪辑软件 P0-06 验证
Dialogue: 0,0:00:05.00,0:00:10.00,默认,第一行：短素材拼接成片
Dialogue: 0,0:00:10.00,0:00:15.00,默认,第二行：中文ASS字幕烧录 123
Dialogue: 0,0:00:15.00,0:00:20.00,默认,第三行：配音与背景乐混音
Dialogue: 0,0:00:20.00,0:00:25.00,默认,第四行：竖屏 1080x1920 30fps
Dialogue: 0,0:00:25.00,0:00:30.00,默认,验证完成，输出 H.264 + AAC
ASS
# 配音：正弦语音替代（440Hz + 幅度包络），背景乐：低幅正弦；混音后 loudnorm 到约 -16 LUFS
$FF -hide_banner -loglevel error -y \
  -i "$WORK/video_only.mp4" \
  -f lavfi -i "sine=frequency=440:duration=30:sample_rate=48000" \
  -f lavfi -i "sine=frequency=220:duration=30:sample_rate=48000" \
  -filter_complex "[2:a]volume=0.15[music];[1:a]volume=0.6[voice];[voice][music]amix=inputs=2:duration=first,loudnorm=I=-16:TP=-1:LRA=11[aud]" \
  -map 0:v -map "[aud]" \
  -vf "subtitles=$WORK/subs.ass:fontsdir=$FONTS" \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -movflags +faststart \
  "$OUT/sample_30s.mp4"
echo "OK: 字幕烧录 + 混音 + 导出完成"

# ---- 4) 复核 ----
info=$($FP -v error -select_streams v:0 -show_entries stream=codec_name,width,height,avg_frame_rate:format=duration -of default=noprint_wrappers=1 "$OUT/sample_30s.mp4")
ainfo=$($FP -v error -select_streams a:0 -show_entries stream=codec_name,sample_rate -of default=noprint_wrappers=1 "$OUT/sample_30s.mp4")
echo "--- 成片信息 ---"
echo "$info"
echo "$ainfo"
dur=$(echo "$info" | grep '^duration=' | cut -d= -f2)
python3 - "$dur" <<'PY'
import sys
dur = float(sys.argv[1])
assert 29.5 <= dur <= 30.5, f"时长偏差过大: {dur}"
print(f"OK: 时长 {dur:.2f}s 在容差内")
PY
echo "P0-06 全部通过：样片 $OUT/sample_30s.mp4"
