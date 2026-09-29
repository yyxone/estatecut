"""§1 ingest — DJI 分段无损拼接成连续时间线 + 抽 16k 单声道音轨 + 段偏移。

复用 estatecut.ffmpeg_tools.run_command / probe_media。不改源媒体。
"""
from __future__ import annotations

import re
from pathlib import Path

from estatecut.ffmpeg_tools import probe_media, run_command
from estatecut.utils import require_outside

from .state import mark_stage

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_clips(input_dir: Path) -> list[Path]:
    files = [p for p in Path(input_dir).iterdir() if p.suffix.lower() in VIDEO_EXTS]
    files.sort(key=lambda p: _natural_key(p.name))
    return files


def ingest(input_dir: Path, out_dir: Path, project: str, log: Path | None = None) -> dict:
    """拼接 -> source.mp4（-c copy 无损）+ audio.wav（16k mono）。返回 ingest 摘要 dict。"""
    out_dir = Path(out_dir)
    require_outside(out_dir, Path(input_dir))  # 硬约束 1：输出不能落在源媒体目录内
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = list_clips(input_dir)
    if not clips:
        raise FileNotFoundError(f"无视频片段: {input_dir}")

    # 段偏移
    offset = 0.0
    clip_meta = []
    for i, c in enumerate(clips):
        dur = probe_media(c)["duration_sec"]
        clip_meta.append({"clip_id": f"c{i:03d}", "filename": c.name, "offset_sec": round(offset, 3), "duration_sec": round(dur, 3)})
        offset += dur

    # concat -c copy（无损）。路径里单引号按 ffconcat 规则转义 ' -> '\''
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    list_file = work / "concat_list.txt"
    list_file.write_text("".join(f"file '{c.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for c in clips), encoding="utf-8")
    source = out_dir / "source.mp4"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-movflags", "+faststart", str(source), "-y",
    ], log)

    # 抽 16k mono 音轨
    audio = out_dir / "audio.wav"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:a:0?", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio), "-y",
    ], log)

    total = round(probe_media(source)["duration_sec"], 3)
    summary = {"project": project, "source": str(source), "audio": str(audio), "duration_sec": total, "clips": clip_meta}
    mark_stage(out_dir, project, "ingest", "done", [str(source), str(audio)], verified=source.exists() and audio.exists())
    return summary
