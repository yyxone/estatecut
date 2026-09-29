"""起点检测核心 — 找"正式进入稳定房间展示"的时间点。

三信号融合（纯本地，不调云端模型）：
1. 帧差能量（motion_diff）— 抓手持晃动 / 开门 / 快速转场；
2. 全局位移（phaseCorrelate）— 抓云台稳定下的平滑行走（帧差小但整幅平移大，
   DJI Pocket 进门段的典型形态，单靠帧差会漏）；
3. 亮度稳定性 — 门口室外亮→门厅暗→室内稳定的曝光过渡段不算稳定。

输出 {detected_start_time, confidence, reason}，reason 只描述真实测到的信号，
不编造"识别到门牌号"这类没做的语义判断。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from estatecut.exceptions import FFmpegError
from estatecut.ffmpeg_tools import run_command
from estatecut.utils import ensure_dir

# 分析分辨率固定 160x90（拉伸不保比例——只做时序统计，比例失真不影响判断）
FRAME_W, FRAME_H = 160, 90
# 运动信号的绝对参考尺度（uint8 帧差 / 像素位移），防全稳视频里噪声被自适应归一放大
DIFF_SCALE = 6.0
SHIFT_SCALE = 1.5
STABLE_THR = 0.35          # 归一化组合运动能量的"稳定"阈值
BRIGHTNESS_STD_TOL = 12.0  # 稳定窗口内亮度标准差上限（曝光过渡中不算稳定）

# 检测算法版本：上面任何常数或信号融合逻辑改了就 bump——detect_cfg 只覆盖用户配置，
# 覆盖不了这些内置阈值；旧缓存裁点是旧算法产物，靠这个字段判失效（Codex R3 F8）。
ANALYZER_VERSION = "start-detect-v1"


def sample_frames(src: Path, scan_seconds: float, sample_fps: int) -> np.ndarray:
    """ffmpeg 解码前 scan_seconds 秒 → (N, H, W) uint8 灰度帧序列。"""
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-t", f"{scan_seconds:.3f}", "-i", str(src),
        "-vf", f"fps={sample_fps},scale={FRAME_W}:{FRAME_H}",
        "-pix_fmt", "gray", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(argv, capture_output=True, shell=False)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode("utf-8", errors="replace").strip() or f"帧采样失败: {src}")
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(data) // (FRAME_W * FRAME_H)
    if n < 2:
        raise FFmpegError(f"采样帧数不足（{n} 帧），视频过短或解码异常: {src}")
    return data[: n * FRAME_W * FRAME_H].reshape(n, FRAME_H, FRAME_W)


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    """edge-padding 滑动平均——零填充会把首尾运动能量压低，可能让全程轻晃的开头被误判稳定。"""
    if window <= 1:
        return values
    pad = window // 2
    padded = np.pad(values, (pad, window - 1 - pad), mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(padded, kernel, mode="valid")


def compute_signals(frames: np.ndarray, sample_fps: int) -> dict[str, np.ndarray]:
    """从帧序列算逐帧信号曲线（首帧无前帧，运动信号从第 2 帧起，补齐首值）。"""
    f = frames.astype(np.float32)
    n = len(f)
    diff = np.zeros(n)
    shift = np.zeros(n)
    for i in range(1, n):
        diff[i] = float(np.mean(np.abs(f[i] - f[i - 1])))
        (dx, dy), _ = cv2.phaseCorrelate(f[i - 1].astype(np.float64), f[i].astype(np.float64))
        shift[i] = float(np.hypot(dx, dy))
    diff[0], shift[0] = diff[1], shift[1]
    brightness = f.mean(axis=(1, 2)).astype(np.float64)

    # 归一：自适应（p90）与绝对尺度取大者做分母——全稳视频 p90 极小时不放大噪声
    diff_norm = diff / max(float(np.percentile(diff, 90)), DIFF_SCALE)
    shift_norm = shift / max(float(np.percentile(shift, 90)), SHIFT_SCALE)
    motion = 0.5 * diff_norm + 0.5 * shift_norm
    smooth_win = max(1, int(round(0.5 * sample_fps)))
    return {
        "motion": _smooth(motion, smooth_win),
        "diff": diff,
        "shift": shift,
        "brightness": brightness,
    }


def detect_from_signals(
    motion: np.ndarray,
    brightness: np.ndarray,
    sample_fps: int,
    stable_window: float,
) -> dict[str, Any]:
    """核心判定：第一个"运动能量低 + 亮度平稳"并持续 stable_window 的时间点。

    与 ffmpeg 解耦，方便用合成曲线做单测。
    """
    n = len(motion)
    win = max(2, int(round(stable_window * sample_fps)))
    num_windows = max(0, n - win + 1)  # 完整窗口数，主循环与 fallback 统一用（防 off-by-one）

    start_idx: int | None = None
    for i in range(num_windows):
        seg = motion[i : i + win]
        bseg = brightness[i : i + win]
        if seg.mean() < STABLE_THR and seg.max() < 2 * STABLE_THR and bseg.std() < BRIGHTNESS_STD_TOL:
            start_idx = i
            break

    if start_idx is None:
        # 扫描段内没有满足条件的稳定窗 → 兜底选运动最低的窗口起点，低置信度警告
        if num_windows > 0:
            means = np.array([motion[i : i + win].mean() for i in range(num_windows)])
            idx = int(means.argmin())
            t = idx / sample_fps
            return {
                "detected_start_time": round(t, 2),
                "confidence": 0.25,
                "reason": (
                    f"警告：扫描段内未找到持续 {stable_window:.1f}s 的稳定画面"
                    f"（全段运动能量均值 {motion.mean():.2f} ≥ 阈值 {STABLE_THR}）。"
                    f"兜底选运动最低的 {t:.1f}s 处，建议人工复核并在 overrides 里指定 start_time。"
                ),
                "fallback": True,
            }
        return {
            "detected_start_time": 0.0,
            "confidence": 0.2,
            "reason": "警告：扫描段过短，无法完成稳定性判定，从 0s 保留全片，建议人工复核。",
            "fallback": True,
        }

    t = start_idx / sample_fps
    post = float(motion[start_idx : start_idx + win].mean())
    margin = max(0.0, min(1.0, 1.0 - post / STABLE_THR))

    if start_idx == 0:
        conf = round(0.6 + 0.3 * margin, 2)
        return {
            "detected_start_time": 0.0,
            "confidence": conf,
            "reason": (
                f"开头即稳定：前 {stable_window:.1f}s 运动能量均值 {post:.2f} 低于阈值 {STABLE_THR}，"
                f"未检测到不稳定的进门/晃动开头，从 0s 完整保留。"
            ),
            "fallback": False,
        }

    pre = float(motion[:start_idx].mean())
    contrast = max(0.0, min(1.0, (pre - post) / (pre + 1e-6)))
    conf = round(min(0.97, 0.4 + 0.4 * contrast + 0.2 * margin), 2)
    return {
        "detected_start_time": round(t, 2),
        "confidence": conf,
        "reason": (
            f"前 {t:.1f}s 检测到持续镜头运动/位移（运动能量均值 {pre:.2f}，典型于门口、开门、进门行走段），"
            f"{t:.1f}s 起画面进入稳定（运动能量降至 {post:.2f}，持续 ≥{stable_window:.1f}s，亮度平稳），"
            f"判定为正式室内展示起点。"
        ),
        "fallback": False,
    }


def detect_start(src: Path, detect_cfg: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    """对单个视频跑完整起点检测，返回检测结果 dict（可直接落盘复用）。"""
    scan = min(float(detect_cfg["scan_seconds"]), duration_sec)
    fps = int(detect_cfg["sample_fps"])
    frames = sample_frames(src, scan, fps)
    signals = compute_signals(frames, fps)
    result = detect_from_signals(signals["motion"], signals["brightness"], fps, float(detect_cfg["stable_window"]))

    # 清晰度门：检测点若严重糊帧，向后小窗顺移到锐帧（治慢摇 walkthrough 偏早落在运动模糊帧）
    _snap_off_blur(src, result, duration_sec)

    max_trim = detect_cfg.get("max_trim_seconds")
    if max_trim is not None and result["detected_start_time"] > float(max_trim):
        result["reason"] += f" （检测值 {result['detected_start_time']:.1f}s 超过 max_trim_seconds={max_trim}，已钳制）"
        result["detected_start_time"] = float(max_trim)
        result["confidence"] = min(result["confidence"], 0.4)

    result["scan_seconds"] = round(scan, 2)
    result["sample_fps"] = fps
    return result


def save_keyframes(src: Path, start: float, duration_sec: float, out_dir: Path, slug: str) -> dict[str, str]:
    """导出裁剪点前 / 所在 / 后三张关键帧截图，方便人工快速复查。"""
    ensure_dir(out_dir)
    points = {
        "before": max(0.0, start - 2.0),
        "selected": min(start, max(0.0, duration_sec - 0.1)),
        "after": min(max(0.0, duration_sec - 0.1), start + 2.0),
    }
    paths: dict[str, str] = {}
    for label, t in points.items():
        dest = out_dir / f"{slug}_{label}.jpg"
        run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.3f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "3", str(dest),
        ])
        paths[label] = str(dest)
    return paths


def _laplacian_var(gray: np.ndarray) -> float:
    """Laplacian 方差 = 对焦清晰度代理（越大越锐）。糊帧 / 运动模糊 → 极低。"""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# 清晰度门参数。**绝对 Laplacian 值随探测分辨率+场景内容漂移**（实测：同一糊帧全分辨率 2.6 /
# 1280px 12.9 / 320px 218），所以判据用**相对比值**（跟邻近帧比，同分辨率同场景只差几秒）而非绝对阈值。
SHARP_PROBE_W = 1280      # 探测分辨率：1280px 判别力最好（糊/锐 ~10x），比全 4K 解码快
SHARP_MIN_PEAK = 40.0     # 邻近锐帧的清晰度下限（低于此=整体低清场景如平墙，不判糊、不顺移）
SHARP_BLUR_RATIO = 0.4    # t0 清晰度 < 邻近峰值 × 此 → 判为明显糊
SHARP_SETTLE_RATIO = 0.6  # 顺移到首个清晰度 ≥ 邻近峰值 × 此 的帧（"settled sharp"）


def _frame_sharpness_at(src: Path, t: float, probe_w: int = SHARP_PROBE_W) -> float | None:
    """抽 t 时刻单帧算 Laplacian 清晰度；采样失败返回 None（合成/损坏源优雅跳过）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={probe_w}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True, shell=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = cv2.imdecode(np.frombuffer(proc.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return None
    return _laplacian_var(cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY))


def _snap_off_blur(src: Path, result: dict[str, Any], duration_sec: float,
                   *, max_forward: float = 1.5, step: float = 0.2) -> None:
    """清晰度门：检测点若比邻近锐帧明显糊，向后小窗顺移到 settled-sharp 帧。

    治检测器"偏早落在运动模糊帧"的失败（DJI Pocket 慢摇 walkthrough 典型，如 203 检测 1.5s 是糊帧）。
    **相对判据**（比邻近帧，非绝对阈值）——只在 t0 明显比邻近糊、且邻近确有足够锐帧时动，上限 +1.5s，
    绝不跨过房间内容（真正的过度裁切由剪切印张人审兜住）。采样失败（合成源）静默跳过，不炸检测。
    就地改 result（detected_start_time / confidence / start_sharpness）。
    """
    t0 = float(result["detected_start_time"])
    if t0 >= duration_sec - 0.3:
        return
    fwd_times = [round(t0 + step * i, 2) for i in range(1, int(max_forward / step) + 1)
                 if t0 + step * i <= duration_sec - 0.2]
    s0 = _frame_sharpness_at(src, t0)
    fwd = {t: s for t in fwd_times if (s := _frame_sharpness_at(src, t)) is not None}
    if s0 is None or not fwd:
        if s0 is not None:
            result["start_sharpness"] = round(s0, 1)
        return
    peak = max(fwd.values())
    # t0 明显比邻近糊 + 邻近确有足够锐帧（排除整体低清的平墙场景误判）
    if peak >= SHARP_MIN_PEAK and s0 < SHARP_BLUR_RATIO * peak:
        for t in sorted(fwd):
            if fwd[t] >= SHARP_SETTLE_RATIO * peak:
                result["reason"] += (
                    f" （检测点 {t0:.1f}s 明显糊 S={s0:.0f} vs 邻近峰值 {peak:.0f}，"
                    f"清晰度门顺移到 {t:.1f}s 锐帧 S={fwd[t]:.0f}）")
                result["detected_start_time"] = round(t, 2)
                result["confidence"] = min(float(result["confidence"]), 0.5)
                result["start_sharpness"] = round(fwd[t], 1)
                result["blur_snapped"] = True
                return
    result["start_sharpness"] = round(s0, 1)


def save_cut_filmstrip(
    src: Path, start: float, duration_sec: float, out_dir: Path, slug: str,
    *, post_pad: float = 1.5, cols: int = 4, tile_w: int = 340,
) -> str:
    """剪切预览印张：从 0 → start+post_pad 密集抽帧拼图，标注每帧时间 + Laplacian
    清晰度 + 剪(红)/留(绿)，在剪切点着色分界。

    为什么要它：常规 before/selected/after 三帧全落在剪切点±2s，**看不到剪掉了什么**——
    起点设太靠后（如把"走进第一个房间"整段剪没）在那三帧上不可见（209 就这么溜过去的）。
    本印张把"即将扔掉的 0→start 内容"整段铺出来：红标帧若显示的是房间/走进画面 = 过度裁切，
    一眼可判。清晰度已烤进图（Python 算，0 model token），人/主对话读缩略图即可，无需全分辨率帧。
    """
    ensure_dir(out_dir)
    end = min(max(start + post_pad, 1.0), max(0.05, duration_sec - 0.05))
    n = max(3, min(12, int(round(end / 0.4)) + 1))
    times = [round(end * i / (n - 1), 2) for i in range(n)]

    label_h = 26
    tiles = []
    for t in times:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
             "-vf", f"scale={tile_w}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, shell=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            continue
        arr = cv2.imdecode(np.frombuffer(proc.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            continue
        sharp = _laplacian_var(cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY))
        kept = t >= start - 1e-6
        th = arr.shape[0]
        tile = np.zeros((th + label_h, tile_w, 3), dtype=np.uint8)
        # BGR：留=绿底、剪=红底
        tile[:label_h] = (40, 90, 40) if kept else (40, 40, 110)
        tile[label_h:] = arr
        tag = "KEEP" if kept else "CUT"
        cv2.putText(tile, f"{tag} t={t:.1f}s S={sharp:.0f}", (4, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)

    if not tiles:
        return ""
    # 统一高度后按 cols 拼网格
    h0 = tiles[0].shape[0]
    tiles = [t if t.shape[0] == h0 else cv2.resize(t, (tile_w, h0)) for t in tiles]
    rows = []
    for r in range(0, len(tiles), cols):
        row = tiles[r : r + cols]
        while len(row) < cols:
            row.append(np.zeros_like(tiles[0]))
        rows.append(np.hstack(row))
    grid = np.vstack(rows)
    banner = np.zeros((30, grid.shape[1], 3), dtype=np.uint8)
    # cv2 HERSHEY 字体不支持中文 → banner 用 ASCII（RED=剪 / GREEN=留）
    cv2.putText(banner, f"CUT PREVIEW  {slug}   RED=cut 0->{start:.1f}s   GREEN=keep {start:.1f}s->",
                (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 1, cv2.LINE_AA)
    out = np.vstack([banner, grid])
    dest = out_dir / f"{slug}_cutstrip.jpg"
    cv2.imwrite(str(dest), out, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return str(dest)
