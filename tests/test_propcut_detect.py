"""起点检测核心算法单测 — 用合成信号曲线，不跑 ffmpeg。"""

from __future__ import annotations

import numpy as np

from pathlib import Path

from propcut.analyze import STABLE_THR, _snap_off_blur, detect_from_signals, save_cut_filmstrip

FPS = 8


def _flat_brightness(n: int) -> np.ndarray:
    return np.full(n, 120.0)


def test_shaky_intro_then_stable_finds_transition() -> None:
    # 前 4s 高运动（进门晃动），之后 6s 稳定
    n_shaky, n_stable = 4 * FPS, 6 * FPS
    motion = np.concatenate([np.full(n_shaky, 1.2), np.full(n_stable, 0.08)])
    result = detect_from_signals(motion, _flat_brightness(len(motion)), FPS, stable_window=2.0)
    assert not result["fallback"]
    assert 3.5 <= result["detected_start_time"] <= 5.0
    assert result["confidence"] >= 0.7
    assert "稳定" in result["reason"]


def test_stable_from_frame_zero_returns_zero() -> None:
    motion = np.full(10 * FPS, 0.1)
    result = detect_from_signals(motion, _flat_brightness(len(motion)), FPS, stable_window=2.0)
    assert result["detected_start_time"] == 0.0
    assert not result["fallback"]
    assert "开头即稳定" in result["reason"]


def test_never_stable_falls_back_with_low_confidence() -> None:
    rng = np.random.default_rng(7)
    motion = 0.9 + 0.4 * rng.random(10 * FPS)
    result = detect_from_signals(motion, _flat_brightness(len(motion)), FPS, stable_window=2.0)
    assert result["fallback"]
    assert result["confidence"] <= 0.3
    assert "警告" in result["reason"]


def test_brightness_transition_delays_start() -> None:
    # 运动 2s 后就低，但 2-5s 亮度还在剧烈过渡（开门进屋曝光变化）→ 起点应推迟到亮度稳定后
    n = 10 * FPS
    motion = np.concatenate([np.full(2 * FPS, 1.0), np.full(n - 2 * FPS, 0.1)])
    brightness = np.full(n, 120.0)
    ramp = np.linspace(230.0, 60.0, 3 * FPS)  # 2s-5s 亮度从过曝滑到暗
    brightness[2 * FPS : 5 * FPS] = ramp
    result = detect_from_signals(motion, brightness, FPS, stable_window=2.0)
    assert result["detected_start_time"] >= 4.0


def test_too_short_scan_is_flagged() -> None:
    motion = np.full(4, 0.1)  # 0.5s 不够 stable_window
    result = detect_from_signals(motion, _flat_brightness(4), FPS, stable_window=2.0)
    assert result["fallback"]
    assert result["detected_start_time"] == 0.0


def test_exactly_one_window_unstable_uses_fallback_not_too_short() -> None:
    # n == win 的边界：有且仅有一个完整窗口且不稳定 → 走"未找到稳定窗"兜底，不误报"扫描段过短"
    n = 2 * FPS
    motion = np.full(n, 1.0)
    result = detect_from_signals(motion, _flat_brightness(n), FPS, stable_window=2.0)
    assert result["fallback"]
    assert "未找到" in result["reason"]
    assert "过短" not in result["reason"]


def test_threshold_constant_sane() -> None:
    assert 0 < STABLE_THR < 1


def test_snap_off_blur_missing_src_is_noop() -> None:
    # 采样失败（源不存在，ffmpeg 报错）→ 清晰度门静默跳过：不抛异常、不改起点、不标 blur_snapped
    result = {"detected_start_time": 1.5, "confidence": 0.6, "reason": "x"}
    _snap_off_blur(Path("does_not_exist_xyz.mp4"), result, duration_sec=30.0)
    assert result["detected_start_time"] == 1.5
    assert not result.get("blur_snapped")


def test_cut_filmstrip_missing_src_returns_empty(tmp_path) -> None:
    # 抽帧全失败 → 剪切印张返回 ""（不抛异常，不写坏图）
    out = save_cut_filmstrip(Path("does_not_exist_xyz.mp4"), 2.5, 30.0, tmp_path, "nope")
    assert out == ""
