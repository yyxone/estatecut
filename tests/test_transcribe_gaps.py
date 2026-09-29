"""talkcut transcribe.audit_gaps 单测 —— 守"字幕空白≠没内容"第一硬约束。

不真调 ffmpeg：monkeypatch measure_mean_db。验 decision 阈值 + 乱序/重叠/头尾防御。
对应 evals basement-transcribe-gap-recovery 的脱媒体可执行版。
"""
from __future__ import annotations

from pathlib import Path

from talkcut import transcribe


def _audit(monkeypatch, segments, total, fake_db):
    monkeypatch.setattr(transcribe, "measure_mean_db", lambda media, a, dur: fake_db)
    return transcribe.audit_gaps(Path("dummy.mp4"), segments, total)


def test_speech_level_gap_flagged_retranscribe(monkeypatch):
    # 两段间 ≥2s 空隙，电平 -28dB（说话）→ 必须 retranscribe，不当死区剪掉
    segs = [{"start": 0.0, "end": 10.0}, {"start": 40.0, "end": 50.0}]
    audit = _audit(monkeypatch, segs, 50.0, -28.0)
    gap = [g for g in audit if g["start"] == 10.0 and g["end"] == 40.0]
    assert gap and gap[0]["decision"] == "retranscribe"


def test_true_silence_gap_gemini_vision(monkeypatch):
    segs = [{"start": 0.0, "end": 10.0}, {"start": 40.0, "end": 50.0}]
    audit = _audit(monkeypatch, segs, 50.0, -45.0)
    gap = [g for g in audit if g["start"] == 10.0][0]
    assert gap["decision"] == "gemini_vision"


def test_boundary_db_is_retranscribe(monkeypatch):
    # 恰好 SPEECH_FLOOR_DB → `>=` 判 retranscribe（保守留内容）
    segs = [{"start": 0.0, "end": 5.0}, {"start": 10.0, "end": 15.0}]
    audit = _audit(monkeypatch, segs, 15.0, transcribe.SPEECH_FLOOR_DB)
    gap = [g for g in audit if g["start"] == 5.0][0]
    assert gap["decision"] == "retranscribe"


def test_short_gap_skipped(monkeypatch):
    # gap < MIN_GAP_SEC(2.0) 不审
    segs = [{"start": 0.0, "end": 10.0}, {"start": 11.0, "end": 20.0}]
    audit = _audit(monkeypatch, segs, 20.0, -28.0)
    assert all(not (g["start"] == 10.0 and g["end"] == 11.0) for g in audit)


def test_out_of_order_segments_gap_not_missed(monkeypatch):
    # 回归核心：ASR 返回乱序段，有内容的大空隙不能因乱序被算成负值漏审
    segs = [{"start": 40.0, "end": 50.0}, {"start": 0.0, "end": 10.0}]  # 乱序
    audit = _audit(monkeypatch, segs, 50.0, -28.0)
    assert any(g["start"] == 10.0 and g["end"] == 40.0 for g in audit)


def test_overlapping_segments_no_false_gap(monkeypatch):
    # 重叠段（后段 start < 前段 end）：不崩、不造假 gap，后续真 gap 仍审到
    segs = [{"start": 0.0, "end": 30.0}, {"start": 20.0, "end": 25.0}, {"start": 40.0, "end": 50.0}]
    audit = _audit(monkeypatch, segs, 50.0, -28.0)
    assert any(g["start"] == 30.0 and g["end"] == 40.0 for g in audit)
    assert all(g["end"] - g["start"] > 0 for g in audit)  # 无负/零长 gap


def test_head_and_tail_gaps(monkeypatch):
    # 头部（0→首段）与尾部（末段→total）空隙都要审
    segs = [{"start": 5.0, "end": 10.0}]
    audit = _audit(monkeypatch, segs, 20.0, -28.0)
    assert any(g["start"] == 0.0 and g["end"] == 5.0 for g in audit)
    assert any(g["start"] == 10.0 and g["end"] == 20.0 for g in audit)
