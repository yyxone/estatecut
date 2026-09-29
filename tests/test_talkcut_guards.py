"""P2 防御回归：人审产物 / 契约缺失时 fail-fast，不静默送 ffmpeg / ASR。

对应 CODE-REVIEW-2026-06-21：P2-4（transcribe 缺 audio 键）、P2-11（rough-cut
keep_ranges 校验）、P2-12（subtitle 要求 cut_lock）。全部纯逻辑，不调 ffmpeg / 真实 ASR。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from talkcut.cut import validate_keep_ranges
from talkcut.subtitle import make_srt
from talkcut.transcribe import transcribe


class TestKeepRangesValidation:
    """P2-11：人工改过的 EDL 送渲染前校验。"""

    def test_negative_duration_rejected(self):
        with pytest.raises(ValueError, match="非法区间"):
            validate_keep_ranges([{"src_start": 5.0, "src_end": 4.0}])

    def test_negative_start_rejected(self):
        with pytest.raises(ValueError, match="非法区间"):
            validate_keep_ranges([{"src_start": -1.0, "src_end": 2.0}])

    def test_overlap_rejected(self):
        with pytest.raises(ValueError, match="重叠"):
            validate_keep_ranges([{"src_start": 0.0, "src_end": 3.0},
                                  {"src_start": 2.0, "src_end": 5.0}])

    def test_unsorted_rejected(self):
        with pytest.raises(ValueError, match="重叠/乱序"):
            validate_keep_ranges([{"src_start": 4.0, "src_end": 5.0},
                                  {"src_start": 0.0, "src_end": 2.0}])

    def test_out_of_bounds_rejected(self):
        with pytest.raises(ValueError, match="越界"):
            validate_keep_ranges([{"src_start": 0.0, "src_end": 10.0}], total=6.0)

    def test_valid_ranges_pass(self):
        validate_keep_ranges([{"src_start": 0.0, "src_end": 2.5},
                              {"src_start": 3.5, "src_end": 5.5}], total=6.0)

    def test_start_beyond_eof_rejected(self):
        # Codex review 抓漏：整段落在 EOF 后但宽度 <50ms，尾点钻进容忍窗
        with pytest.raises(ValueError, match="起点越界"):
            validate_keep_ranges([{"src_start": 10.01, "src_end": 10.04}], total=10.0)

    def test_nan_rejected(self):
        # Codex review 抓漏：NaN 与所有比较均 False，静默通过生成 trim=nan
        with pytest.raises(ValueError, match="非有限数"):
            validate_keep_ranges([{"src_start": float("nan"), "src_end": 2.0}])

    def test_infinity_rejected(self):
        with pytest.raises(ValueError, match="非有限数"):
            validate_keep_ranges([{"src_start": 0.0, "src_end": float("inf")}])


def test_transcribe_missing_audio_fails_fast(tmp_path: Path):
    """P2-4：ingest_summary 缺 audio 键必须报错，不静默把视频喂 ASR。"""
    summary = {"source": str(tmp_path / "source.mp4"), "duration_sec": 6.0}
    with pytest.raises(KeyError, match="audio"):
        transcribe(tmp_path, "guard", summary, mode="mock")


def test_make_srt_requires_cut_lock(tmp_path: Path):
    """P2-12：cut_lock 未上 → subtitle 拒绝（未审 EDL 出字幕必错位）。"""
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    (tmp_path / "edl.json").write_text(
        json.dumps({"keep_ranges": [], "human_locked": False}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cut_lock"):
        make_srt(tmp_path, "guard")
