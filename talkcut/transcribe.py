"""§2 transcribe — ASR（抗幻觉）+ gap 音量审计 → transcript.json。

核心硬规则：字幕空白 ≠ 没内容。对段间空隙测音量，≈说话电平 → 标 retranscribe；
真静音 → 标 true_silence / gemini_vision。conforms schemas/transcript.schema.json。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from estatecut.ffmpeg_tools import run_command

from . import asr_adapter
from .state import mark_stage

MIN_GAP_SEC = 2.0          # 只审 ≥2s 的空隙
SPEECH_FLOOR_DB = -33.0    # ≥ 此值视为疑似有语音（实战：漏转死区 ~-28dB，真静音 -35~-47dB）


def measure_mean_db(media: Path, start: float, dur: float) -> float:
    proc = run_command([
        "ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-i", str(media),
        "-t", f"{dur:.3f}", "-map", "0:a:0?", "-af", "volumedetect", "-f", "null", "-",
    ])
    m = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -99.0


def audit_gaps(media: Path, segments: list[dict], total: float) -> list[dict]:
    """找 segments 之间 + 头尾的空隙，测音量，给 decision。

    硬约束防御：ASR 段可能乱序/重叠（faster-whisper VAD 偶发）。先按 start 排序，
    用 cursor=max(已见 end) 推进算空隙——否则乱序/重叠会把有内容的 gap 算成负值后
    静默跳过，违反"字幕空白≠没内容"。
    """
    audit = []
    segs = sorted(segments, key=lambda s: (float(s["start"]), float(s["end"])))
    bounds: list[tuple[float, float]] = []
    cursor = 0.0
    for s in segs:
        st, en = float(s["start"]), float(s["end"])
        if st - cursor >= MIN_GAP_SEC:
            bounds.append((cursor, st))
        cursor = max(cursor, en)  # clamp 重叠：不让重叠段缩小后续 gap 检测
    if total - cursor >= MIN_GAP_SEC:
        bounds.append((cursor, total))
    for a, b in bounds:
        db = measure_mean_db(media, a, b - a)
        if db >= SPEECH_FLOOR_DB:
            decision, result = "retranscribe", f"疑似漏转（mean {db:.1f}dB ≥ {SPEECH_FLOOR_DB}dB），需重转/交人确认"
        else:
            # 硬规则：真静音不直接判剪——先交 Gemini 看画面，vision/人工确认后才进 cuts
            decision, result = "gemini_vision", f"真静音（mean {db:.1f}dB），待 Gemini 看画面定 留(B-roll)/剪；未确认前不剪"
        audit.append({"start": round(a, 3), "end": round(b, 3), "mean_volume_db": round(db, 1),
                      "decision": decision, "result": result})
    return audit


def transcribe(out_dir: Path, project: str, ingest_summary: dict, mode: str = "real",
               device: str = "cuda", corrections_version: str = "2026-06-03") -> dict:
    out_dir = Path(out_dir)
    source = Path(ingest_summary["source"])
    if "audio" not in ingest_summary:
        # 契约：ASR 吃 ingest 抽出的 16k 单声道 wav。缺键直接报错，不静默拿视频顶
        raise KeyError("[transcribe] ingest_summary 缺 'audio'（16k wav）——先跑 ingest 补齐，不回退视频喂 ASR")
    audio = Path(ingest_summary["audio"])
    total = float(ingest_summary["duration_sec"])

    asr = asr_adapter.transcribe_wav(audio, mode=mode, device=device)
    segments = asr["segments"]
    gap_audit = audit_gaps(source, segments, total)

    transcript = {
        "project": project,
        "source": {"path": str(source), "duration_sec": total, "clips": ingest_summary.get("clips", [])},
        "asr": asr["asr"],
        "segments": segments,
        "gap_audit": gap_audit,
        "corrections_version": corrections_version,
    }
    p = out_dir / "transcript.json"
    p.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    flagged = sum(1 for g in gap_audit if g["decision"] == "retranscribe")
    mark_stage(out_dir, project, "transcribe", "done", [str(p)], verified=p.exists(),
               note=f"{len(segments)} segs / {len(gap_audit)} gaps / {flagged} 疑似漏转")
    return transcript
