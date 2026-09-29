"""导出：裁头 + 调色 + 尺寸适配 + 配乐混音，一条 ffmpeg 命令完成。

裁剪用输入侧 `-ss` + 重编码（帧精确）；音乐用 `-stream_loop -1` 循环输入 +
输出侧 `-t` 截断（短则循环补足、长则截断），结尾 afade 收音。曲长 < 片长、
循环点会被听到时，filter 内改用 crossfade 拼贴消接缝（music_loop_plan）。
编码 NVENC 优先、运行时失败回退 libx264（质量档可配，故不直接用
estatecut.ffmpeg_tools.run_video_encode 的固定 CRF/CQ）。

响度归一（music.loudnorm.enabled，默认关）= 两遍 loudnorm，两遍都跑**最终音频
时间线**（volume/fade/amix/`-t` 截断全在内）：第一遍只出测量 JSON 不编视频
（`-f null`）；第二遍把 measured_* 回填 + linear=true 线性增益（避免动态模式的
泵感），loudnorm 内部 192k 上采样后必须 aresample 回 48k。测量失败降级为不归一
（现状行为）+ warning 上报，不阻塞出片。
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from estatecut.exceptions import FFmpegError
from estatecut.ffmpeg_tools import preferred_encoder, probe_media, run_command

from .config import parse_resolution

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]

# loudnorm 测量 JSON 里回填第二遍所需的键（值保持 ffmpeg 原样字符串直接嵌 filter）
_MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")

LOOP_XFADE_SEC = 1.0      # 循环接缝交叉淡化时长（P1-3）
_LOOP_MIN_TRACK = 3.0     # 曲子短于此不做 crossfade 拼贴（acrossfade 没有足够重叠料）
_LOOP_MAX_COPIES = 64     # 拼贴份数上限（超出 = 极短曲 × 超长片的病态组合，退回现状硬接）

_nvenc_runtime_disabled = False


def _quality_args(encoder: str, quality: str) -> list[str]:
    crf = {"high": "18", "medium": "21", "low": "26"}[quality]
    cq = {"high": "19", "medium": "23", "low": "28"}[quality]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", cq]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", crf]
    return ["-c:v", encoder]


def _encode_with_fallback(build_argv, quality: str, log: Path | None = None):
    """同 ffmpeg_tools.run_video_encode 的回退策略：仅 libx264 回退成功才判定 NVENC 不可用。"""
    global _nvenc_runtime_disabled
    enc = "libx264" if _nvenc_runtime_disabled else preferred_encoder()
    try:
        return run_command(build_argv(_quality_args(enc, quality)), log)
    except FFmpegError as nvenc_err:
        if enc != "h264_nvenc":
            raise
        try:
            result = run_command(build_argv(_quality_args("libx264", quality)), log)
        except FFmpegError as x264_err:
            raise FFmpegError(f"NVENC 与 libx264 均失败。NVENC: {nvenc_err} | libx264: {x264_err}") from x264_err
        _nvenc_runtime_disabled = True
        return result


def scale_pad_filter(w: int, h: int, fit: str) -> str:
    """统一到 w×h 的缩放滤镜（fit=crop 裁切 / pad 补边）。stitch 段规格归一也用它。"""
    if fit == "crop":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"


def _scale_filter(export_cfg: dict[str, Any]) -> str:
    res = parse_resolution(export_cfg["resolution"])
    if res is None:
        return ""
    return scale_pad_filter(*res, export_cfg["fit"])


def music_loop_plan(track_dur: float | None, music_start: float,
                    target_dur: float) -> tuple[int, float] | None:
    """曲长不够、会真的循环时 → crossfade 拼贴方案 (份数 N, 交叉淡化秒 d)。

    不需要循环 / 曲长未知 / 曲子过短或病态组合 → None（保持现状 `-stream_loop -1`
    硬接，行为零变化）。N 份整曲经 N-1 次 acrossfade 后总长 = N*L - (N-1)*d，
    须 ≥ music_start + target_dur + d + 0.5（margin 吸收 mp3 头部时长估计误差）。
    """
    if track_dur is None or track_dur <= 0:
        return None
    length = float(track_dur)
    if length - music_start >= target_dur + 0.25:
        return None  # 播不到曲尾，接缝不存在
    if length < _LOOP_MIN_TRACK or length - music_start < 1.0:
        return None
    xfade = min(LOOP_XFADE_SEC, length / 4)
    need = music_start + target_dur + xfade + 0.5
    copies = math.ceil((need - xfade) / (length - xfade))
    if copies <= 1 or copies > _LOOP_MAX_COPIES:
        return None
    return copies, xfade


def build_music_audio_chain(
    music_cfg: dict[str, Any],
    target_dur: float,
    mix_original: bool = False,
    music_label: str = "[1:a]",
    original_label: str = "[0:a]",
    out_label: str = "[outa]",
    track_dur: float | None = None,
) -> str:
    """音乐侧音频 filter 图（起点/音量/淡入淡出，mix 时与原声 amix），输出 [outa]。

    单视频导出与 stitch 终混共用。长度匹配语义在调用侧：输入 `-stream_loop -1`
    无限循环 + 输出侧 `-t target_dur` 截断，本函数只负责 filter 图。

    music_start 用 atrim 跳开头（不用输入侧 -ss）：-stream_loop -1 是无限流，atrim 在滤镜里
    丢弃前 N 秒后仍是有效无限流；输入 -ss 与 -stream_loop 的交互跨 ffmpeg 版本不稳定，
    且曲子短于 N 秒时输入 seek 行为未定义。asetpts 把时间戳归零，让下游 afade/amix/-t 正常。

    传了 track_dur 且曲长 < 片长时，用等长淡入淡出、延迟和不归一的 amix
    做线性交叉淡化。避免 FFmpeg 6.1 的串联 acrossfade 在共享 EOF 时丢失音轨。
    再进入原有起点/音量/整体淡入淡出步骤。命令侧不变。
    """
    fade = min(float(music_cfg.get("fade_out") or 0), target_dur / 2)
    fade_in = min(float(music_cfg.get("fade_in") or 0), target_dur / 2)
    music_start = float(music_cfg.get("music_start") or 0)
    music_vol = float(music_cfg.get("volume", 0.8))
    orig_vol = float(music_cfg.get("original_volume", 1.0))

    loop_prefix = ""
    plan = music_loop_plan(track_dur, music_start, target_dur)
    if plan is not None:
        copies, xfade = plan
        splits = "".join(f"[lp{i}]" for i in range(copies))
        loop_prefix = (f"{music_label}atrim=duration={float(track_dur):.3f},"
                       f"asetpts=PTS-STARTPTS,asplit={copies}{splits};")
        for i in range(copies):
            filters = []
            if i > 0:
                filters.append(f"afade=t=in:st=0:d={xfade:.3f}")
            if i < copies - 1:
                filters.append(f"afade=t=out:st={float(track_dur) - xfade:.3f}:d={xfade:.3f}")
            if i > 0:
                delay_ms = round(i * (float(track_dur) - xfade) * 1000)
                filters.append(f"adelay={delay_ms}:all=1")
            loop_prefix += f"[lp{i}]{','.join(filters)}[lx{i}];"
        loop_prefix += ("".join(f"[lx{i}]" for i in range(copies))
                        + f"amix=inputs={copies}:duration=longest:dropout_transition=0:normalize=0[loop];")
        music_label = "[loop]"

    mchain = music_label
    if music_start > 0:
        mchain += f"atrim=start={music_start:.3f},asetpts=PTS-STARTPTS,"
    mchain += f"volume={music_vol:g}"
    if fade_in > 0:
        mchain += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade > 0:
        mchain += f",afade=t=out:st={max(0.0, target_dur - fade):.3f}:d={fade:.3f}"
    if mix_original:
        # normalize=0：amix 默认把每路除以输入数（音量减半），音量只由 volume 滤镜控制。
        # duration=longest：源音轨若比视频流短，first 会让音乐跟着提前断；音乐无限循环由 -t 截断。
        return (loop_prefix + f"{original_label}volume={orig_vol:g}[a0];{mchain}[a1];"
                f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0{out_label}")
    return loop_prefix + f"{mchain}{out_label}"


def parse_loudnorm_json(stderr: str) -> dict[str, Any] | None:
    """loudnorm print_format=json 的测量块在 stderr 末尾——取最后一个含 input_i 的 {...}。"""
    matches = re.findall(r"\{[^{}]*\}", stderr, flags=re.DOTALL)
    for chunk in reversed(matches):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if "input_i" in obj:
            return obj
    return None


def loudnorm_filter(ln_cfg: dict[str, Any], measured: dict[str, Any] | None = None) -> str:
    """第一遍（measured=None）出测量 JSON；第二遍回填 measured_* + linear + 归回 48k。"""
    base = f"loudnorm=I={float(ln_cfg['i']):g}:TP={float(ln_cfg['tp']):g}:LRA={float(ln_cfg['lra']):g}"
    if measured is None:
        return base + ":print_format=json"
    return (base + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true,aresample=48000")


def resolve_audio_mode(music_cfg: dict[str, Any], music_path: Path | None,
                       audio_present: bool) -> tuple[str, Path | None]:
    """original_audio 的降级链 →（实际音频模式, 实际参与混音的音乐）。

    测量遍与导出遍共用，保证两遍跑的是同一条音频时间线。
    """
    audio_mode = music_cfg["original_audio"]
    if music_path is None:
        # keep/mix 无音乐可混 → 都落到保留原声；remove → 静音片
        return ("remove_silent" if audio_mode == "remove" else "keep"), None
    if audio_mode == "mix" and not audio_present:
        return "remove", music_path  # 源无音轨，mix 降级为纯音乐
    if audio_mode == "keep":
        return "keep", None  # keep = 保留原声不加音乐
    return audio_mode, music_path


def build_measure_command(src: Path, start: float, target_dur: float, music_path: Path,
                          music_cfg: dict[str, Any], mix_original: bool,
                          music_track_dur: float | None = None) -> list[str]:
    """第一遍测量命令：只跑最终音频时间线到 `-f null`，不解码/编码视频。

    不能用 -loglevel error——loudnorm 的测量 JSON 是 info 级 stderr 输出。
    """
    chain = build_music_audio_chain(music_cfg, target_dur, mix_original, out_label="[premix]",
                                    track_dur=music_track_dur)
    fc = chain + f";[premix]{loudnorm_filter(music_cfg['loudnorm'])}[m]"
    return ["ffmpeg", "-hide_banner", "-nostats", "-y",
            "-ss", f"{start:.3f}", "-i", str(src),
            "-stream_loop", "-1", "-i", str(music_path),
            "-filter_complex", fc, "-map", "[m]",
            "-f", "null", "-t", f"{target_dur:.3f}", "-"]


def measure_timeline_loudness(measure_argv: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """跑测量遍 →（measured 键值 | None, warning | None）。失败/非有限值 → 降级不归一。"""
    proc = subprocess.run(measure_argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", shell=False)
    parsed = parse_loudnorm_json(proc.stderr or "")
    if proc.returncode != 0 or not parsed:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["无 stderr"]
        return None, f"loudnorm 测量遍失败（exit {proc.returncode}，{tail[0]}），本条不做响度归一"
    measured: dict[str, str] = {}
    for key in _MEASURED_KEYS:
        raw = parsed.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            # 全静音等极端输入会测出 -inf；linear 模式喂进去直接出错 → 降级
            return None, f"loudnorm 测量值异常（{key}={raw!r}），本条不做响度归一"
        measured[key] = str(raw)
    return measured, None


def build_export_command(
    src: Path,
    out_path: Path,
    start: float,
    duration_sec: float,
    grading_filter: str,
    export_cfg: dict[str, Any],
    music_path: Path | None,
    music_cfg: dict[str, Any],
    audio_present: bool,
    color_tag: bool = False,
    loudnorm_measured: dict[str, str] | None = None,
    music_track_dur: float | None = None,
) -> tuple[Any, float, str]:
    """返回 (build_argv 闭包, target_duration, 实际音频模式)。拆出来方便单测命令构造。

    color_tag=True 时输出加 bt709 色彩 tag（SDR 源 709/未标注或经 tonemap 后，防播放器猜错）。
    """
    target_dur = max(0.1, duration_sec - start)

    vf_parts = [p for p in (grading_filter, _scale_filter(export_cfg)) if p]
    vchain = ",".join(vf_parts) if vf_parts else "null"

    audio_mode, music_path = resolve_audio_mode(music_cfg, music_path, audio_present)

    filter_complex = f"[0:v]{vchain}[outv]"
    maps: list[str] = ["-map", "[outv]"]
    audio_out_args: list[str] = []

    if music_path is not None:
        use_ln = loudnorm_measured is not None
        filter_complex += ";" + build_music_audio_chain(
            music_cfg, target_dur, mix_original=(audio_mode == "mix"),
            out_label="[premix]" if use_ln else "[outa]", track_dur=music_track_dur)
        if use_ln:
            filter_complex += f";[premix]{loudnorm_filter(music_cfg['loudnorm'], loudnorm_measured)}[outa]"
        maps += ["-map", "[outa]"]
        audio_out_args = AUDIO_ARGS
    elif audio_mode == "keep":
        # 只取第一条音频流：iPhone .MOV 常带第二条 apac 空间音频轨（多数 ffmpeg 无解码器），
        # `0:a?` 会连它一起 map，重编码时解码 apac 失败。`0:a:0?` 只要主 aac 轨。
        maps += ["-map", "0:a:0?"]
        audio_out_args = AUDIO_ARGS if audio_present else []
    else:  # remove_silent
        maps += ["-an"]

    tag_args = (["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
                if color_tag else [])

    def build_argv(enc_args: list[str]) -> list[str]:
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-i", str(src)]
        if music_path is not None:
            argv += ["-stream_loop", "-1", "-i", str(music_path)]
        argv += ["-filter_complex", filter_complex, *maps,
                 *enc_args, "-pix_fmt", "yuv420p", *tag_args, *audio_out_args,
                 "-t", f"{target_dur:.3f}", "-movflags", "+faststart", str(out_path)]
        return argv

    return build_argv, target_dur, audio_mode


def export_video(
    src: Path,
    out_path: Path,
    start: float,
    duration_sec: float,
    grading_filter: str,
    export_cfg: dict[str, Any],
    music_path: Path | None,
    music_cfg: dict[str, Any],
    audio_present: bool,
    log: Path | None = None,
    color_tag: bool = False,
) -> dict[str, Any]:
    # 先写临时名再原子替换：批量中断不留半截 mp4（半截文件会被 overwrite=false 跳过，用户拿到坏片）
    tmp_path = out_path.with_name(out_path.stem + ".part" + out_path.suffix)

    # 响度归一第一遍：opt-in 且实际有配乐链时才测（keep/无曲不归一——目标是配乐成片一致性）
    ln_cfg = music_cfg.get("loudnorm") or {}
    measured: dict[str, str] | None = None
    loudnorm_report: dict[str, Any] | None = None
    eff_mode, eff_music = resolve_audio_mode(music_cfg, music_path, audio_present)
    track_dur: float | None = None
    if eff_music is not None:
        # 曲长给 crossfade 循环消接缝用；探不出 → None = 循环保持现状硬接（真正的解码
        # 错误留给导出遍报，两遍链仍一致）
        try:
            track_dur = float(probe_media(eff_music)["duration_sec"]) or None
        except Exception:
            track_dur = None
    if eff_music is not None and ln_cfg.get("enabled"):
        target = max(0.1, duration_sec - start)
        measured, ln_warn = measure_timeline_loudness(build_measure_command(
            src, start, target, eff_music, music_cfg, mix_original=(eff_mode == "mix"),
            music_track_dur=track_dur))
        loudnorm_report = {"applied": measured is not None,
                           "target_i": float(ln_cfg["i"]), "target_tp": float(ln_cfg["tp"])}
        if measured is not None:
            loudnorm_report["measured_i"] = float(measured["input_i"])
            loudnorm_report["measured_tp"] = float(measured["input_tp"])
        if ln_warn:
            loudnorm_report["warning"] = ln_warn

    build_argv, target_dur, audio_mode = build_export_command(
        src, tmp_path, start, duration_sec, grading_filter,
        export_cfg, music_path, music_cfg, audio_present, color_tag=color_tag,
        loudnorm_measured=measured, music_track_dur=track_dur)
    try:
        _encode_with_fallback(build_argv, export_cfg["quality"], log)
        _verify_part(tmp_path, target_dur, audio_mode, audio_present)
        tmp_path.replace(out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    result = {"output": str(out_path), "target_duration": round(target_dur, 3), "audio_mode": audio_mode}
    if loudnorm_report is not None:
        result["loudnorm"] = loudnorm_report
    return result


def _verify_part(tmp_path: Path, target_dur: float, audio_mode: str, audio_present: bool) -> None:
    """替换正式输出前在 .part 上校验：非空 / 有效视频流 / 时长 / 音轨符合模式。

    失败抛 FFmpegError（pipeline 逐视频 catch → entry error），旧正式输出保持原样。
    """
    expected_audio = audio_mode in ("remove", "mix") or (audio_mode == "keep" and audio_present)
    info = probe_media(tmp_path)
    problems: list[str] = []
    if tmp_path.stat().st_size <= 0:
        problems.append("产物 0 字节")
    if int(info.get("width") or 0) <= 0 or int(info.get("height") or 0) <= 0:
        problems.append("无有效视频流")
    if abs(float(info.get("duration_sec") or 0.0) - target_dur) >= 1.0:
        problems.append(f"时长 {float(info.get('duration_sec') or 0.0):.2f}s 偏离目标 {target_dur:.2f}s")
    if bool(info.get("audio_present")) != expected_audio:
        problems.append(f"音轨 {bool(info.get('audio_present'))} ≠ 模式 {audio_mode} 预期 {expected_audio}")
    if problems:
        raise FFmpegError(f"导出校验失败（.part 已丢弃，正式输出未覆盖）: {'；'.join(problems)}")
