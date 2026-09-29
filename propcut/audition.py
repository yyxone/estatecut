"""propcut audition 小样（P0-2）——同一段画面 × N 首候选曲，等响度对齐试听。

解决"光看清单定不了曲"：画面代理只编码一次（540p 无音频），每首候选只做混音
remux（视频流 copy），秒级出 N 个小样。候选间用 loudnorm 单遍测量把响度对齐到
统一基准（AUDITION_TARGET_I）——不对齐的话响的曲子天然显得"更带感"，试听会系统性
误选（研究笔记：等响度是试听公平性的前提）。对齐增益只作用于小样，不影响成片。

小样音频链独立于成片：淡出固定 1s（试听尾巴利落即可，不套 profile fade_out）；
淡入/music_start 沿用配置（跟成片同段落起听）。产物 out/<dir>/audition/<slug>/。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from estatecut.ffmpeg_tools import probe_media, run_command
from estatecut.utils import ensure_dir, now_iso, require_outside, write_json

from .config import load_config, load_overrides, match_override
from .export import parse_loudnorm_json as _parse_loudnorm_json
from .pipeline import _cache_matches, _load_detection_cache, _slug, scan_videos
from .suggest import SuggestUnavailable, suggest

AUDITION_TARGET_I = -18.0   # 试听等响基准 LUFS（仅小样对齐用，不代表成片/平台响度）
GAIN_LIMIT_DB = 20.0        # 对齐增益上限（超出说明测量或素材异常，不硬拉）
PROBE_SECONDS = 60.0        # loudnorm 测量只取曲子前 N 秒（够代表，省时间）
FADE_OUT_SEC = 1.0          # 小样固定短淡出


def measure_input_i(track: Path, probe_seconds: float = PROBE_SECONDS) -> float | None:
    """单遍 loudnorm 测量曲子的 integrated loudness（LUFS）；失败 → None（调用方记警告）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-t", f"{probe_seconds:.1f}", "-i", str(track),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False)
    parsed = _parse_loudnorm_json(proc.stderr or "")
    if not parsed:
        return None
    try:
        return float(parsed["input_i"])
    except (KeyError, TypeError, ValueError):
        return None


def _alignment_gain(input_i: float | None) -> tuple[float, str | None]:
    """候选 → 试听对齐增益 dB（clamp ±GAIN_LIMIT_DB）。测不出 → 0 dB + 警告。"""
    if input_i is None:
        return 0.0, "loudnorm 测量失败，该候选未做响度对齐（试听时注意音量差）"
    gain = AUDITION_TARGET_I - input_i
    if abs(gain) > GAIN_LIMIT_DB:
        clamped = max(-GAIN_LIMIT_DB, min(GAIN_LIMIT_DB, gain))
        return clamped, f"对齐增益 {gain:+.1f}dB 超限已收到 {clamped:+.1f}dB（素材响度异常）"
    return gain, None


def _sample_audio_chain(gain_db: float, music_start: float, fade_in: float,
                        sample_dur: float) -> str:
    """小样音频链：选段起点 → 对齐增益 → 淡入（配置）→ 固定 1s 淡出。"""
    steps = []
    if music_start > 0:
        steps.append(f"atrim=start={music_start:.3f},asetpts=PTS-STARTPTS")
    steps.append(f"volume={gain_db:.2f}dB")
    if fade_in > 0:
        steps.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    fade_start = max(0.0, sample_dur - FADE_OUT_SEC)
    steps.append(f"afade=t=out:st={fade_start:.3f}:d={FADE_OUT_SEC:.3f}")
    return "[1:a]" + ",".join(steps) + "[a]"


def run_audition(config_path: Path, only: str | None = None,
                 tracks: list[str] | None = None, top: int = 5,
                 duration: float = 18.0) -> dict[str, Any]:
    """主入口：1 条视频 × N 候选曲 → audition/<slug>/ 小样组 + manifest。"""
    config_path = Path(config_path)
    cfg = load_config(config_path)
    input_dir = Path(cfg["input"]["dir"])
    output_dir = Path(cfg["output"]["dir"])
    require_outside(output_dir, input_dir)

    videos = scan_videos(cfg)
    if only:
        videos = [v for v in videos if only in v.name]
    if not videos:
        raise ValueError(f"没有匹配的视频（--only={only!r}）: {input_dir}")
    if len(videos) > 1:
        names = ", ".join(v.name for v in videos[:8])
        raise ValueError(f"audition 一次只做一条视频，匹配到 {len(videos)} 条（{names}…）"
                         f"——用 --only 收窄到一条")
    src = videos[0]
    rel = src.relative_to(input_dir)
    slug = _slug(rel)

    probe = probe_media(src)
    total_dur = float(probe["duration_sec"])
    if total_dur <= 0:
        raise ValueError(f"ffprobe 读不到时长: {src}")

    # 起点：override > 检测缓存 > 0（提示先跑 detect 更准）
    warnings: list[str] = []
    reports_dir = output_dir / "_reports"
    overrides = load_overrides(cfg)
    ov = match_override(overrides, rel) or {}
    cached = _load_detection_cache(reports_dir, slug)
    if "start_time" in ov:
        start = float(ov["start_time"])
    elif cached is not None and _cache_matches(cached, src, total_dur, cfg["detect"]):
        start = float(cached["detection"]["detected_start_time"])
    else:
        start = 0.0
        warnings.append("无检测缓存（或已失效），小样从 0s 开始——先跑 detect 可用正式开头")
    effective_dur = total_dur - start
    sample_dur = min(float(duration), effective_dur)
    if sample_dur <= 0:
        raise ValueError(f"起点 {start:.2f}s 之后没有内容（视频 {total_dur:.2f}s）")

    # 候选曲：显式 --tracks 优先；否则推荐器 top-N（按成片有效时长打分，不是小样时长）
    suggest_by_path: dict[str, dict[str, Any]] = {}
    if tracks:
        track_paths = []
        for t in tracks:
            p = Path(t)
            if not p.is_file():
                raise ValueError(f"--tracks 指定的曲子不存在: {p}")
            track_paths.append(p)
    else:
        try:
            result = suggest(config_path, duration=effective_dur, top=top)
        except SuggestUnavailable as exc:
            raise ValueError(f"推荐器不可用（{exc}）——用 --tracks 显式给候选曲") from exc
        if not result["suggestions"]:
            raise ValueError("推荐器 0 候选（过滤闸后池为空）——用 --tracks 显式给候选曲")
        track_paths = [Path(s["path"]) for s in result["suggestions"]]
        suggest_by_path = {s["path"]: s for s in result["suggestions"]}

    out_dir = ensure_dir(output_dir / "audition" / slug)

    # 画面代理只编码一次：540p 无音频（后续每候选只混音 remux，视频流 copy）
    proxy = out_dir / "_proxy.mp4"
    run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{start:.3f}", "-t", f"{sample_dur:.3f}", "-i", str(src),
                 "-vf", r"scale=-2:min(540\,ih)", "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-movflags", "+faststart", str(proxy)])

    music_start = float(cfg["music"].get("music_start") or 0.0)
    fade_in = float(cfg["music"].get("fade_in") or 0.0)
    candidates: list[dict[str, Any]] = []
    for i, track in enumerate(track_paths, 1):
        input_i = measure_input_i(track)
        gain_db, warn = _alignment_gain(input_i)
        out_path = out_dir / f"{i:02d}_{track.stem}.mp4"
        chain = _sample_audio_chain(gain_db, music_start, fade_in, sample_dur)
        run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(proxy), "-stream_loop", "-1", "-i", str(track),
                     "-filter_complex", chain, "-map", "0:v", "-map", "[a]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-t", f"{sample_dur:.3f}", "-movflags", "+faststart", str(out_path)])
        pm = probe_media(out_path)
        ok = (out_path.stat().st_size > 0 and pm["audio_present"]
              and abs(pm["duration_sec"] - sample_dur) < 0.5)
        cand: dict[str, Any] = {
            "idx": i, "track": str(track), "track_name": track.name,
            "input_i": input_i, "gain_db": round(gain_db, 2),
            "output": str(out_path), "ok": ok,
        }
        if warn:
            cand["warning"] = warn
        s = suggest_by_path.get(str(track))
        if s:
            cand["suggest_score"] = s["score"]
            cand["suggest_parts"] = s["parts"]
        candidates.append(cand)

    manifest = {
        "at": now_iso(), "video": str(src), "slug": slug,
        "start": round(start, 3), "sample_duration": round(sample_dur, 3),
        "effective_duration": round(effective_dur, 3),
        "target_i": AUDITION_TARGET_I, "music_start": music_start, "fade_in": fade_in,
        "note": "小样已按 loudnorm 对齐到统一试听响度（gain_db 仅作用于小样，不影响成片）",
        "warnings": warnings, "candidates": candidates,
    }
    write_json(out_dir / "audition_manifest.json", manifest)
    return manifest
