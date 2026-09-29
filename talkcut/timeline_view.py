"""§verify helper — filmstrip + 波形 + 中文词标 + 静音阴影 合成 PNG（本地视觉 drilldown）。

给 video + [start,end]：ffmpeg 抽 N 帧拼横向 filmstrip，下方画音频 RMS 波形，
叠中文 phrase 词标（按标点/静音 gap 聚合，非裸单字）+ 静音阴影 + 时间尺。

定位：Gemini 看画面之前的**免费本地初筛**（减少、非替代云调用）。只在决策点用——
真静音段看画面 / 剪点自查 / retake 对比。**不是扫描工具**，别对每段循环调。

借鉴 browser-use/video-use helpers/timeline_view.py 思路（clean-room 重写 + 中文适配），
评估见 docs/references/video-use-eval-20260701.md。中文适配：faster-whisper 中文 words
是单字/短块，直接标会碎——按标点(SENTENCE_END)/gap≥0.4s 聚合成 phrase 再标（同 subtitle.py 思路）。
读 estatecut transcript schema：segments[].words[]={start,end,word}。

依赖：ffmpeg（抽帧/抽音频，走 ffmpeg_tools.run_command，shell=False）+ PIL + numpy（pyproject 已有）。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from estatecut.exceptions import FFmpegError
from estatecut.ffmpeg_tools import run_command

# 中文字体 fallback（Windows）——video-use 原用 mac 字体，换本地中文字体避免词标豆腐块
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",   # 黑体
    "C:/Windows/Fonts/simsun.ttc",   # 宋体
    "C:/Windows/Fonts/Deng.ttf",     # 等线
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
# phrase 收束标点（中文 word-level 直接标会碎，同 subtitle.py SENTENCE_END 思路）
SENTENCE_END = "。！？；…!?;，,、"

BG = (18, 18, 22)
FG = (235, 235, 235)
DIM = (120, 120, 130)
ACCENT = (255, 140, 60)
SILENCE_FILL = (60, 90, 130, 110)   # 半透明蓝：静音阴影
WAVE = (140, 180, 255)

try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _RESAMPLE = Image.LANCZOS  # type: ignore[attr-defined]


def _load_font(size: int) -> ImageFont.ImageFont:
    explicit = os.environ.get("ESTATECUT_FONT")
    if explicit:
        font = Path(explicit).expanduser()
        if not font.is_file():
            raise FileNotFoundError(f"ESTATECUT_FONT 不存在: {font}")
        return ImageFont.truetype(str(font), size)
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _extract_frames(video: Path, start: float, end: float, n: int, dest: Path) -> list[Path]:
    """抽 N 帧均匀分布在 [start,end]。返回有序路径（失败的帧跳过，不炸）。"""
    if end <= start or n <= 1:
        times = [max(0.0, (start + end) / 2.0)]
    else:
        step = (end - start) / (n - 1)
        times = [max(0.0, start + i * step) for i in range(n)]
    paths: list[Path] = []
    for i, t in enumerate(times):
        out = dest / f"f_{i:03d}.jpg"
        try:
            run_command([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.3f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "3", str(out), "-y",
            ])
        except FFmpegError:
            continue
        if out.exists() and out.stat().st_size > 0:
            paths.append(out)
    return paths


def _compute_envelope(video: Path, start: float, end: float, samples: int = 1600) -> np.ndarray:
    """抽 [start,end] mono 16k PCM，算长度 samples 的 RMS 包络（归一 [0,1]）。

    手动读 WAV（estatecut 环境无 librosa）。无音频轨 → 全零（不炸）。
    """
    dur = max(0.05, end - start)
    zeros = np.zeros(samples, dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        try:
            run_command([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{dur:.3f}",
                "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(wav), "-y",
            ])
        except FFmpegError:
            return zeros
        if not wav.exists() or wav.stat().st_size == 0:
            return zeros
        with wave.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
    if len(raw) < 2:
        return zeros
    pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size == 0:
        return zeros
    window = max(1, pcm.size // samples)
    usable = (pcm.size // window) * window
    if usable == 0:
        return zeros
    env = np.sqrt(np.mean(pcm[:usable].reshape(-1, window) ** 2, axis=1))
    if env.size < samples:
        env = np.pad(env, (0, samples - env.size))
    else:
        env = env[:samples]
    peak = float(env.max())
    if peak > 0:
        env = env / peak
    return env


def _words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    out: list[dict] = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            ws, we = w.get("start"), w.get("end")
            if ws is None or we is None:
                continue
            if float(we) >= start and float(ws) <= end:
                out.append({"start": float(ws), "end": float(we), "word": w.get("word", "")})
    out.sort(key=lambda x: x["start"])
    return out


def _join_words(words: list[dict]) -> str:
    """中文词间不加空格、英文词间加空格（faster-whisper 中文 word 多为单字）。"""
    text = ""
    for w in words:
        p = (w["word"] or "").strip()
        if not p:
            continue
        if text and text[-1].isascii() and text[-1].isalnum() and p[0].isascii() and p[0].isalnum():
            text += " " + p
        else:
            text += p
    return text


def _group_phrases(words: list[dict], gap_threshold: float = 0.4) -> list[dict]:
    """词级 → phrase：遇句末标点收束 or 静音 gap≥阈值 断开（中文防碎）。"""
    phrases: list[dict] = []
    cur: list[dict] = []
    prev_end: float | None = None
    for w in words:
        txt = (w["word"] or "").strip()
        if prev_end is not None and cur and w["start"] - prev_end >= gap_threshold:
            phrases.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": _join_words(cur)})
            cur = []
        cur.append(w)
        if txt and txt[-1] in SENTENCE_END:
            phrases.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": _join_words(cur)})
            cur = []
        prev_end = w["end"]
    if cur:
        phrases.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": _join_words(cur)})
    return [p for p in phrases if p["text"]]


def _find_silences(words: list[dict], start: float, end: float,
                   threshold: float = 0.4) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    prev = start
    for w in words:
        ws = max(start, w["start"])
        if ws - prev >= threshold:
            gaps.append((prev, ws))
        prev = max(prev, w["end"])
    if end - prev >= threshold:
        gaps.append((prev, end))
    return gaps


def render_timeline(video: Path, start: float, end: float, transcript: Path | None = None,
                    n_frames: int = 8, out_path: Path | None = None) -> Path:
    """合成 filmstrip+波形+中文词标+静音阴影 PNG，返回输出路径。"""
    video = Path(video)
    if end <= start:
        raise ValueError(f"end<=start: [{start},{end}]")

    # transcript 自动解析（未给则找同目录 transcript.json）
    tdata: dict = {}
    if transcript is None:
        cand = video.parent / "transcript.json"
        transcript = cand if cand.exists() else None
    if transcript and Path(transcript).exists():
        tdata = json.loads(Path(transcript).read_text(encoding="utf-8"))

    margin = 40
    filmstrip_h = 160
    wave_h = 150

    with tempfile.TemporaryDirectory() as tmp:
        frame_paths = _extract_frames(video, start, end, n_frames, Path(tmp))
        imgs: list[Image.Image] = []
        for fp in frame_paths:
            try:
                im = Image.open(fp).convert("RGB")
            except OSError:
                continue
            aspect = im.width / im.height if im.height else 1.78
            new_w = max(1, int(filmstrip_h * aspect))
            imgs.append(im.resize((new_w, filmstrip_h), _RESAMPLE))

        gap = 2
        strip_w = (sum(im.width for im in imgs) + gap * max(0, len(imgs) - 1)) if imgs else 1200
        canvas_w = max(1400, strip_w + 2 * margin)

        header_y = 10
        filmstrip_y = 40
        label_y = filmstrip_y + filmstrip_h + 8
        wave_y = label_y + 26
        ruler_y = wave_y + wave_h + 6
        canvas_h = ruler_y + 40

        canvas = Image.new("RGB", (canvas_w, canvas_h), BG)
        draw = ImageDraw.Draw(canvas, "RGBA")

        header_font = _load_font(18)
        label_font = _load_font(15)
        small_font = _load_font(12)

        draw.text((margin, header_y),
                  f"{video.name}  [{start:.2f}-{end:.2f}]  ({end - start:.2f}s)",
                  fill=FG, font=header_font)

        # filmstrip
        strip_x0 = margin
        x = strip_x0
        for im in imgs:
            canvas.paste(im, (x, filmstrip_y))
            x += im.width + gap
        strip_x1 = (x - gap) if imgs else (strip_x0 + strip_w)
        strip_span = max(1, strip_x1 - strip_x0)

        def t2x(t: float) -> int:
            frac = (t - start) / max(1e-6, end - start)
            return int(strip_x0 + frac * strip_span)

        words = _words_in_range(tdata, start, end) if tdata else []
        silences = _find_silences(words, start, end) if words else []
        phrases = _group_phrases(words) if words else []

        # 静音阴影（波形区）
        for a, b in silences:
            draw.rectangle([t2x(a), wave_y, t2x(b), wave_y + wave_h], fill=SILENCE_FILL)

        # 波形
        env = _compute_envelope(video, start, end, samples=max(200, strip_span))
        mid_y = wave_y + wave_h // 2
        max_amp = wave_h // 2 - 6
        for i, v in enumerate(env):
            xi = strip_x0 + int(i * strip_span / max(1, len(env) - 1))
            amp = int(float(v) * max_amp)
            if amp > 0:
                draw.line([(xi, mid_y - amp), (xi, mid_y + amp)], fill=WAVE, width=1)
        draw.line([(strip_x0, mid_y), (strip_x1, mid_y)], fill=(70, 70, 80), width=1)

        # phrase 词标（波形上方一行，防重叠）
        last_right = -9999
        for ph in phrases:
            cx = (t2x(ph["start"]) + t2x(ph["end"])) // 2
            txt = ph["text"]
            if len(txt) > 12:
                txt = txt[:12] + "…"
            tb = draw.textbbox((0, 0), txt, font=label_font)
            tw = tb[2] - tb[0]
            lx = cx - tw // 2
            if lx < last_right + 6:
                continue  # 防重叠：间距不够则跳过（tick 仍画）
            draw.line([(cx, label_y + 18), (cx, wave_y)], fill=(90, 90, 100), width=1)
            draw.text((lx, label_y), txt, fill=ACCENT, font=label_font)
            last_right = lx + tw

        # 时间尺
        n_ticks = 6
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            t = start + frac * (end - start)
            xi = strip_x0 + int(frac * strip_span)
            draw.line([(xi, ruler_y), (xi, ruler_y + 6)], fill=DIM, width=1)
            draw.text((xi + 2, ruler_y + 8), f"{t:.1f}s", fill=DIM, font=small_font)

        # 图例
        legend = "蓝色阴影=静音≥0.4s  |  橙字=phrase 词标  |  蓝波形=音量包络"
        if silences:
            legend = f"蓝色阴影=静音≥0.4s（{len(silences)} 段）  |  橙字=phrase 词标  |  蓝波形=音量包络"
        draw.text((margin, ruler_y + 22), legend, fill=DIM, font=small_font)

        if out_path is None:
            out_dir = video.parent / "verify"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{video.stem}_{start:.2f}-{end:.2f}.png"
        else:
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="filmstrip+波形+中文词标 合成图（本地视觉 drilldown）")
    ap.add_argument("video", type=Path)
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--transcript", type=Path, default=None)
    ap.add_argument("--n-frames", type=int, default=8)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()
    out = render_timeline(args.video, args.start, args.end, args.transcript, args.n_frames, args.output)
    print(f"[timeline_view] {out}")


if __name__ == "__main__":
    main()
