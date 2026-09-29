"""调色引擎 — 零 propcut 内部依赖（只用标准库 + yaml + estatecut 共享层）。

三层结构（社区共识"先校正，后风格"）：
1. correction  — HDR/log 源 → SDR tonemap 链（iPhone DV/HLG、DJI HLG 保命，见 S2）；
2. style       — 预设滤镜串 / .cube LUT（configs/propcut_presets.yaml，可扩展）；
3. auto        — signalstats 场景分类 → 选预设（opt-in，见 classify_scene）。

Public API（未来 talkcut 等直接 import 本模块）：
- probe_color_info(src)              读色彩元数据 + 判 HDR/DV
- build_correction_chain(ci, cfg)    tonemap 链或空串
- build_style_chain(gc, presets)     预设滤镜串（= 旧 build_grading_filter）
- build_color_chain(ci, gc, presets) correction + style 总入口
- classify_scene(src, ...)           signalstats 分类 → dark/bright/warm_cast/cool_cast/normal
- resolve_grading_choice(...)        override / auto → 最终 grading_cfg
- sdr_output_tag(ci, tonemapped)     输出侧 bt709 tag 决策
"""

from __future__ import annotations

import math
import statistics
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from estatecut.exceptions import GradingError
from estatecut.ffmpeg_tools import ffprobe_json, probe_media

REPO_ROOT = Path(__file__).resolve().parents[1]
from estatecut.resources_path import resource_path
PRESETS_FILE = resource_path("propcut_presets.yaml")

# HDR transfer 特征：PQ(HDR10) / HLG。primaries=bt2020 或有 Dolby Vision RPU 也判 HDR。
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
# 输出侧可安全 tag bt709 的源色彩（709 或未标注）；其余罕见 SDR（bt601/smpte170m…）不硬 tag。
_TAG_OK_TRANSFER = {None, "", "unknown", "bt709", "iec61966-2-1"}
_TAG_OK_PRIMARIES = {None, "", "unknown", "bt709"}

# HDR→SDR tonemap 链（CPU zscale，v1 不上 libplacebo；DV 动态元数据丢失是已知限制）。
# hable(filmic)+desat=0 = 暗部/高光都保的稳妥默认；npl=100 目标 SDR 峰值亮度。
_TONEMAP_CHAIN = (
    "zscale=tin={tin}:t=linear:npl=100,format=gbrpf32le,"
    "zscale=p=bt709,tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)

# ---- auto 场景分类默认（研究 C11：YAVG 80-140 正常区间，U/V 中性 128）----
DEFAULT_AUTO_THRESHOLDS = {
    "dark_yavg": 80.0,     # YAVG < 80 → 偏暗
    "bright_yavg": 140.0,  # YAVG > 140 → 偏亮
    "cast_uv": 10.0,       # |UAVG-128| / |VAVG-128| 超过即判冷/暖色偏
}
DEFAULT_AUTO_MAP = {
    "dark": "bright_interior",
    "bright": "bright_airy",
    "warm_cast": "warm_interior",
    "cool_cast": "warm_cozy",
    "normal": "bright_airy",
}
# 分类采样帧尺寸（小图只做统计，比例失真不影响均值/中位数）。
_SIG_W, _SIG_H = 128, 72


# ---------------------------------------------------------------------------
# 预设加载 + style 链
# ---------------------------------------------------------------------------
def load_presets(path: Path | None = None) -> dict[str, dict[str, Any]]:
    presets_path = Path(path) if path else PRESETS_FILE
    if not presets_path.exists():
        raise GradingError(f"调色预设文件不存在: {presets_path}")
    with presets_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise GradingError(f"预设文件根节点必须是 mapping: {presets_path}")
    for preset in data.values():
        if isinstance(preset, dict) and preset.get("lut"):
            lut = Path(preset["lut"]).expanduser()
            if not lut.is_absolute():
                lut = presets_path.resolve().parent / lut
            preset["lut"] = str(lut)
    return data


def _escape_filter_path(path: str) -> str:
    """Windows 路径进 ffmpeg filter 参数：反斜杠转正斜杠、盘符冒号转义。"""
    return path.replace("\\", "/").replace(":", "\\:")


def build_style_chain(
    grading_cfg: dict[str, Any],
    presets: dict[str, dict[str, Any]] | None = None,
) -> str:
    """预设滤镜串（可能为空串 = 不套风格）。preset 支持 filter（eq 等）与 lut（.cube）。"""
    if grading_cfg["mode"] == "none":
        return ""
    name = grading_cfg["preset"]
    presets = presets if presets is not None else load_presets()
    if name not in presets:
        raise GradingError(f"调色预设 `{name}` 不存在，可用: {sorted(presets)}")
    preset = presets[name] or {}
    parts: list[str] = []
    lut = preset.get("lut")
    if lut:
        lut_path = Path(lut)
        if not lut_path.is_absolute():
            lut_path = REPO_ROOT / lut_path
        if not lut_path.exists():
            raise GradingError(
                f"预设 `{name}` 引用的 LUT 文件不存在: {lut_path}。"
                f"请自行取得有权使用的 LUT，并在自定义预设中填写其绝对路径。"
            )
        parts.append(f"lut3d='{_escape_filter_path(str(lut_path))}':interp=tetrahedral")
    flt = (preset.get("filter") or "").strip()
    if flt:
        parts.append(flt)
    return ",".join(parts)


# 兼容别名：pipeline / 旧测试仍用 build_grading_filter 这个名字。
build_grading_filter = build_style_chain


# ---------------------------------------------------------------------------
# 色彩探测 + HDR 分流（correction 链）
# ---------------------------------------------------------------------------
def _has_dolby_vision(stream: dict[str, Any]) -> bool:
    for sd in stream.get("side_data_list") or []:
        t = (sd.get("side_data_type") or "").lower()
        if "dolby" in t or "dovi" in t or "dv_profile" in sd:
            return True
    return False


def _is_hdr(info: dict[str, Any]) -> bool:
    ct = (info.get("color_transfer") or "").lower()
    prim = (info.get("color_primaries") or "").lower()
    return ct in HDR_TRANSFERS or prim == "bt2020" or bool(info.get("dolby_vision"))


def classify_color_stream(stream: dict[str, Any]) -> dict[str, Any]:
    """纯函数：从 ffprobe video stream dict 提取色彩信息（便于单测）。"""
    info: dict[str, Any] = {
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "color_space": stream.get("color_space"),
        "color_range": stream.get("color_range"),
        "dolby_vision": _has_dolby_vision(stream),
    }
    info["is_hdr"] = _is_hdr(info)
    return info


def probe_color_info(src: Path) -> dict[str, Any]:
    """读源色彩元数据（transfer/primaries/space/range + Dolby Vision）。

    探测失败绝不 fail 整条视频（AGENTS 硬约束：HDR 误判宁直通）——返回安全默认 +
    probe_error，调用侧据此走直通并在 jsonl 记 warning。
    """
    try:
        data = ffprobe_json(Path(src))
    except Exception as exc:  # noqa: BLE001 — 探测失败一律降级直通
        return {
            "color_transfer": None, "color_primaries": None, "color_space": None,
            "color_range": None, "dolby_vision": False, "is_hdr": False,
            "probe_error": f"{type(exc).__name__}: {exc}",
        }
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return classify_color_stream(stream)


def build_correction_chain(color_info: dict[str, Any], correction_cfg: dict[str, Any]) -> str:
    """HDR/log 源 → SDR tonemap 链，非 HDR 或 hdr=off 返回空串（直通）。"""
    if correction_cfg.get("hdr", "auto") != "auto":
        return ""
    if not color_info.get("is_hdr"):
        return ""
    ct = (color_info.get("color_transfer") or "").lower()
    # PQ 显式 tin=smpte2084；HLG / DV(8.4 HLG 基底) / 仅 bt2020 primaries → 按 HLG 处理。
    tin = "smpte2084" if ct == "smpte2084" else "arib-std-b67"
    return _TONEMAP_CHAIN.format(tin=tin)


def build_color_chain(
    color_info: dict[str, Any],
    grading_cfg: dict[str, Any],
    presets: dict[str, dict[str, Any]] | None = None,
) -> str:
    """correction + style 拼接总入口（pipeline / 外部调用只需这一个）。"""
    correction = build_correction_chain(color_info, grading_cfg)
    style = build_style_chain(grading_cfg, presets)
    return ",".join(p for p in (correction, style) if p)


def sdr_output_tag(color_info: dict[str, Any], tonemapped: bool) -> tuple[bool, str | None]:
    """输出侧是否 tag bt709：tonemap 后 = 是；源 709/未标注 = 是；罕见非 709 SDR = 否 + warning。"""
    if tonemapped:
        return True, None
    ct = (color_info.get("color_transfer") or "").lower() or None
    prim = (color_info.get("color_primaries") or "").lower() or None
    if ct in _TAG_OK_TRANSFER and prim in _TAG_OK_PRIMARIES:
        return True, None
    return False, f"源色彩非 709（transfer={ct}, primaries={prim}），不强制 tag bt709"


# ---------------------------------------------------------------------------
# auto 场景分类（signalstats 语义：YUV 均值/饱和 → 暗/亮/冷暖/正常）
# ---------------------------------------------------------------------------
def _sample_yuv_stats(src: Path, t: float) -> tuple[float, float, float, float] | None:
    """在时间点 t 抽一帧（小图 yuv444p），返回 (YAVG, UAVG, VAVG, SATAVG)。失败返回 None。

    走 ffmpeg 原始帧解码（复用 analyze.py 已验证的 -ss 快 seek + rawvideo 模式），
    避免 lavfi movie/signalstats 在 Windows 路径上的 filter 转义脆弱性；统计语义等价
    signalstats（YAVG=Y 均值、U/V 均值中性 128、SAT=色度幅度均值）。
    """
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={_SIG_W}:{_SIG_H}",
        "-pix_fmt", "yuv444p", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(argv, capture_output=True, shell=False)
    plane = _SIG_W * _SIG_H
    if proc.returncode != 0 or len(proc.stdout) < 3 * plane:
        return None
    buf = proc.stdout
    y_plane = buf[0:plane]
    u_plane = buf[plane:2 * plane]
    v_plane = buf[2 * plane:3 * plane]
    yavg = sum(y_plane) / plane
    uavg = sum(u_plane) / plane
    vavg = sum(v_plane) / plane
    satavg = sum(math.hypot(u_plane[i] - 128, v_plane[i] - 128) for i in range(plane)) / plane
    return yavg, uavg, vavg, satavg


def classify_measurements(yavg: float, uavg: float, vavg: float, thresholds: dict[str, float]) -> str:
    """纯分类逻辑（便于阈值边界单测）。暗/亮优先，其次冷暖色偏，否则 normal。"""
    if yavg < thresholds["dark_yavg"]:
        return "dark"
    if yavg > thresholds["bright_yavg"]:
        return "bright"
    if (vavg - 128) > thresholds["cast_uv"]:   # V 偏高 = 偏红/暖
        return "warm_cast"
    if (uavg - 128) > thresholds["cast_uv"]:   # U 偏高 = 偏蓝/冷
        return "cool_cast"
    return "normal"


def classify_scene(
    src: Path,
    sample_frames: int = 9,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """均匀采样 sample_frames 帧，取 YUV 统计中位数 → 分类。采样失败降级 normal（不 fail）。"""
    thr = {**DEFAULT_AUTO_THRESHOLDS, **(thresholds or {})}
    try:
        duration = float(probe_media(Path(src)).get("duration_sec") or 0)
    except Exception:  # noqa: BLE001
        duration = 0.0
    if duration <= 0:
        return {"class": "normal", "measurements": None, "sample_count": 0,
                "reason": "无法读取时长，signalstats 分类跳过，用 normal 映射"}
    times = [duration * (i + 0.5) / sample_frames for i in range(sample_frames)]
    samples = [s for s in (_sample_yuv_stats(Path(src), t) for t in times) if s is not None]
    if not samples:
        return {"class": "normal", "measurements": None, "sample_count": 0,
                "reason": "signalstats 采样全部失败，用 normal 映射"}
    yavg = statistics.median(s[0] for s in samples)
    uavg = statistics.median(s[1] for s in samples)
    vavg = statistics.median(s[2] for s in samples)
    satavg = statistics.median(s[3] for s in samples)
    cls = classify_measurements(yavg, uavg, vavg, thr)
    return {
        "class": cls,
        "measurements": {"YAVG": round(yavg, 1), "UAVG": round(uavg, 1),
                         "VAVG": round(vavg, 1), "SATAVG": round(satavg, 1)},
        "sample_count": len(samples),
        "reason": (f"{len(samples)} 帧中位数 YAVG={yavg:.0f}/UAVG={uavg:.0f}/VAVG={vavg:.0f} "
                   f"→ {cls}"),
    }


def resolve_grading_choice(
    grading_cfg: dict[str, Any],
    override_preset: str | None,
    color_info: dict[str, Any],
    classify_fn: Callable[[], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """决定最终 grading_cfg：override 永远压过 auto；auto 走 classify_fn 选预设。

    返回 (chosen_grading_cfg, auto_scene_or_None)。HDR 源 auto 分类会偏（signalstats
    测解码后帧），v1 简化：HDR 源直接用 auto_map['normal']，classify_fn 不调用。
    """
    gc = dict(grading_cfg)
    if override_preset:
        gc.update({"mode": "preset", "preset": override_preset})
        return gc, None
    if gc.get("mode") != "auto":
        return gc, None
    auto_map = {**DEFAULT_AUTO_MAP, **(gc.get("auto_map") or {})}
    if color_info.get("is_hdr"):
        scene = {"class": "normal", "measurements": None, "sample_count": 0,
                 "reason": "HDR 源 signalstats 测量对解码前值偏差，跳过分类直接用 normal 映射"}
    else:
        scene = classify_fn() if classify_fn else classify_scene(Path("."))
    scene["preset"] = auto_map[scene["class"]]
    gc.update({"mode": "preset", "preset": scene["preset"]})
    return gc, scene
