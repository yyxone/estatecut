"""Safe FFmpeg and FFprobe wrappers."""

from __future__ import annotations

import json
import shutil
import subprocess
import os
from pathlib import Path
from typing import Any

from .exceptions import FFmpegError
from .utils import ensure_dir


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


_nvenc_runtime_disabled = False


def h264_encoder() -> str:
    """静态检测可用 H.264 编码器，libx264 优先（稳定，给 legacy estatecut 路径用）。

    talkcut 成品编码走 preferred_encoder() + run_video_encode（NVENC 优先 + 失败回退）；
    本函数保持 libx264 优先不变，避免共享 helper 改动让 legacy renderer/segment
    无回退地用上 NVENC（NVENC 被 FFmpeg 列出 ≠ 当前显卡/驱动真能编码）。
    """
    override = os.environ.get("ESTATECUT_ENCODER")
    if override:
        if override not in {"libx264", "h264_nvenc"}:
            raise ValueError("ESTATECUT_ENCODER must be libx264 or h264_nvenc")
        return override
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, shell=False)
    text = proc.stdout + proc.stderr
    if "libx264" in text:
        return "libx264"
    if "h264_mf" in text:
        return "h264_mf"
    if "h264_nvenc" in text:
        return "h264_nvenc"
    return "mpeg4"


def encoder_args(encoder: str) -> list[str]:
    """统一的 H.264 编码参数（cut / fine-cut / qa-export 共用，避免三处漂移）。"""
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    return ["-c:v", encoder]


def preferred_encoder() -> str:
    """talkcut 成品编码首选：NVENC 优先（GPU 提速），运行时失败过则降级 libx264。

    与 h264_encoder() 区分：本函数只在 NVENC（可回退）和 libx264（稳定）间选，
    不走 h264_mf 这类无运行时回退的硬件路径。
    """
    if _nvenc_runtime_disabled:
        return "libx264"
    override = os.environ.get("ESTATECUT_ENCODER")
    if override:
        if override not in {"libx264", "h264_nvenc"}:
            raise ValueError("ESTATECUT_ENCODER must be libx264 or h264_nvenc")
        return override
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, shell=False)
    text = proc.stdout + proc.stderr
    if "h264_nvenc" in text:
        return "h264_nvenc"
    if "libx264" in text:
        return "libx264"
    return h264_encoder()  # 极端兜底：无 nvenc 无 libx264 → mf / mpeg4


def run_command(argv: list[str], log_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    if not argv:
        raise ValueError("Empty command")
    proc = subprocess.run(argv, capture_output=True, text=True, shell=False)
    if log_path:
        ensure_dir(log_path.parent)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(argv) + "\n")
            handle.write(proc.stdout)
            handle.write(proc.stderr)
            handle.write(f"\n[exit {proc.returncode}]\n")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"Command failed with exit {proc.returncode}: {' '.join(argv)}"
        raise FFmpegError(detail)
    return proc


def run_video_encode(build_argv, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    """跑视频编码，NVENC 失败自动回退 libx264。

    build_argv(enc_args) -> 完整 ffmpeg argv。硬件编码器"FFmpeg 声明支持"≠
    "当前显卡/驱动/环境真能编码"。**只有 libx264 回退成功才禁用 NVENC**——
    若 libx264 也失败，说明失败非 NVENC 特有，保留两次错误上下文且不污染本进程
    后续编码状态。回退成功后本进程后续编码直接走 libx264，避免每个片段重复失败。
    """
    global _nvenc_runtime_disabled
    enc = preferred_encoder()
    try:
        return run_command(build_argv(encoder_args(enc)), log)
    except FFmpegError as nvenc_err:
        if enc != "h264_nvenc" or _nvenc_runtime_disabled:
            raise
        try:
            result = run_command(build_argv(encoder_args("libx264")), log)
        except FFmpegError as x264_err:
            # libx264 也失败 → 非 NVENC 特有问题（输入/filtergraph/map/权限）：
            # 不禁用 NVENC、保留两次错误上下文，避免误把通用错误归到 NVENC。
            raise FFmpegError(f"NVENC 与 libx264 编码均失败。NVENC: {nvenc_err} | libx264: {x264_err}") from x264_err
        _nvenc_runtime_disabled = True  # 仅当 libx264 回退成功，才判定 NVENC 不可用并禁用
        return result


def ffprobe_json(path: Path) -> dict[str, Any]:
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = run_command(argv)
    return json.loads(proc.stdout)


def _parse_fps(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            denominator = float(den)
            return float(num) / denominator if denominator else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe_media(path: Path) -> dict[str, Any]:
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = video.get("duration") or data.get("format", {}).get("duration") or 0
    rotation = None
    tags = video.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(tags["rotate"])
        except ValueError:
            rotation = None
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                rotation = int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                pass
    return {
        "duration_sec": float(duration or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "codec_name": video.get("codec_name"),
        "audio_present": audio,
        "rotation": rotation,
        "created_time": tags.get("creation_time") or data.get("format", {}).get("tags", {}).get("creation_time"),
    }


def create_synthetic_clip(path: Path, duration: float, size: str = "720x1280", label: str = "estatecut") -> None:
    ensure_dir(path.parent)
    vf = f"testsrc=size={size}:rate=30,format=yuv420p,drawtext=text='{label}':x=40:y=80:fontsize=36:fontcolor=white"
    run_video_encode(lambda enc: [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        vf,
        "-t",
        f"{duration:.2f}",
        "-an",
        *enc,
        "-pix_fmt",
        "yuv420p",
        str(path),
    ])
