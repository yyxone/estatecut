"""propcut 调色引擎单测 — 全 mock，不跑真 ffmpeg（对齐现有 test 模式）。

覆盖：色彩探测/HDR 判定、correction+style 链拼接顺序、presets_file 自定义加载、
auto 分类阈值边界、auto_map 覆盖、overrides 压过 auto、dlog 缺 LUT 报错、export 输出 tag。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from estatecut.exceptions import GradingError
from propcut import grading
from propcut.grading import (
    DEFAULT_AUTO_MAP,
    DEFAULT_AUTO_THRESHOLDS,
    build_color_chain,
    build_correction_chain,
    build_style_chain,
    classify_color_stream,
    classify_measurements,
    load_presets,
    probe_color_info,
    resolve_grading_choice,
    sdr_output_tag,
)

BUNDLED_PRESETS = {
    "neutral", "bright_interior", "warm_interior", "cloudy_exterior", "luxury_clean",
    "real_estate_bright",  # 现有 6
    "bright_airy", "warm_cozy", "clean_gray", "crisp_daylight",
}


# ---------------- 色彩探测 / HDR 判定 ----------------
def test_classify_color_stream_hlg_pq_709_missing_dv() -> None:
    assert classify_color_stream({"color_transfer": "arib-std-b67"})["is_hdr"] is True   # HLG
    assert classify_color_stream({"color_transfer": "smpte2084"})["is_hdr"] is True       # PQ
    assert classify_color_stream({"color_primaries": "bt2020"})["is_hdr"] is True         # 宽色域
    assert classify_color_stream({"color_transfer": "bt709", "color_primaries": "bt709"})["is_hdr"] is False
    assert classify_color_stream({})["is_hdr"] is False                                   # 缺字段直通
    dv = classify_color_stream({"side_data_list": [{"side_data_type": "DOVI configuration record"}]})
    assert dv["dolby_vision"] is True and dv["is_hdr"] is True


def test_probe_color_info_probe_failure_is_passthrough(monkeypatch) -> None:
    def _boom(_p):
        raise RuntimeError("ffprobe exploded")
    monkeypatch.setattr(grading, "ffprobe_json", _boom)
    info = probe_color_info(Path("x.mp4"))
    assert info["is_hdr"] is False and "probe_error" in info  # 不 fail，直通


# ---------------- correction 链（HDR tonemap 分流）----------------
def test_correction_chain_hlg_pq_off_and_non_hdr() -> None:
    hlg = classify_color_stream({"color_transfer": "arib-std-b67"})
    pq = classify_color_stream({"color_transfer": "smpte2084"})
    sdr = classify_color_stream({"color_transfer": "bt709"})

    c_hlg = build_correction_chain(hlg, {"hdr": "auto"})
    assert "tin=arib-std-b67" in c_hlg and "tonemap=hable" in c_hlg and c_hlg.startswith("zscale")
    assert "tin=smpte2084" in build_correction_chain(pq, {"hdr": "auto"})
    assert build_correction_chain(hlg, {"hdr": "off"}) == ""   # off 直通
    assert build_correction_chain(sdr, {"hdr": "auto"}) == ""  # 非 HDR 直通


def test_build_color_chain_correction_before_style() -> None:
    hlg = classify_color_stream({"color_transfer": "arib-std-b67"})
    presets = {"x": {"filter": "eq=contrast=1.1"}}
    chain = build_color_chain(hlg, {"mode": "preset", "preset": "x", "hdr": "auto"}, presets)
    assert chain.startswith("zscale")
    assert chain.index("tonemap") < chain.index("eq=contrast=1.1")  # 先校正后风格


# ---------------- style 链 / 预设 ----------------
def test_style_chain_byte_stable_for_existing_presets() -> None:
    presets = load_presets()
    assert build_style_chain({"mode": "preset", "preset": "neutral"}, presets) == ""
    # 现有实拍验证预设逐字节不变（回归护栏）
    assert build_style_chain({"mode": "preset", "preset": "real_estate_bright"}, presets) == (
        "curves=all='0/0 0.5/0.5 1/0.97',eq=contrast=1.06:saturation=1.11:gamma=1.04,"
        "colorbalance=rm=0.01:bm=-0.01,unsharp=3:3:0.4:3:3:0.0")


def test_all_bundled_presets_loadable_without_external_luts() -> None:
    presets = load_presets()
    assert BUNDLED_PRESETS <= set(presets), sorted(BUNDLED_PRESETS - set(presets))
    assert all(not preset.get("lut") for preset in presets.values())
    for name in BUNDLED_PRESETS:
        preset = presets[name]
        lut = preset.get("lut")
        if lut and not (grading.REPO_ROOT / lut).exists():
            # LUT 未就位 → 报错信息必须给出下载指引
            with pytest.raises(GradingError, match="绝对路径"):
                build_style_chain({"mode": "preset", "preset": name}, presets)
        else:
            out = build_style_chain({"mode": "preset", "preset": name}, presets)
            if lut:
                assert "lut3d=" in out and "tetrahedral" in out


def test_dlog_missing_lut_error_has_hint() -> None:
    presets = {"dlog_x": {"lut": "assets/luts/does_not_exist_zzz.cube"}}
    with pytest.raises(GradingError, match="绝对路径"):
        build_style_chain({"mode": "preset", "preset": "dlog_x"}, presets)


def test_custom_presets_file_load(tmp_path: Path) -> None:
    pf = tmp_path / "my_presets.yaml"
    pf.write_text("mine:\n  filter: \"eq=saturation=1.2\"\n", encoding="utf-8")
    presets = load_presets(pf)
    assert build_style_chain({"mode": "preset", "preset": "mine"}, presets) == "eq=saturation=1.2"


# ---------------- auto 分类 ----------------
def test_classify_measurements_boundaries() -> None:
    thr = DEFAULT_AUTO_THRESHOLDS
    assert classify_measurements(79, 128, 128, thr) == "dark"
    assert classify_measurements(80, 128, 128, thr) == "normal"   # 80 不算暗（严格 < 80）
    assert classify_measurements(140, 128, 128, thr) == "normal"  # 140 不算亮（严格 > 140）
    assert classify_measurements(141, 128, 128, thr) == "bright"
    assert classify_measurements(110, 128, 139, thr) == "warm_cast"  # V 偏高 = 暖
    assert classify_measurements(110, 139, 128, thr) == "cool_cast"  # U 偏高 = 冷


# ---------------- resolve_grading_choice（override / auto / HDR）----------------
def test_override_beats_auto() -> None:
    def _should_not_run():
        raise AssertionError("override 存在时不应分类")
    gc, scene = resolve_grading_choice(
        {"mode": "auto", "auto_map": None}, "warm_interior",
        {"is_hdr": False}, classify_fn=_should_not_run)
    assert gc["preset"] == "warm_interior" and scene is None


def test_auto_uses_default_map() -> None:
    gc, scene = resolve_grading_choice(
        {"mode": "auto"}, None, {"is_hdr": False},
        classify_fn=lambda: {"class": "normal", "measurements": None})
    assert gc["preset"] == DEFAULT_AUTO_MAP["normal"] == "bright_airy"
    assert scene["class"] == "normal" and scene["preset"] == "bright_airy"


def test_auto_map_override() -> None:
    gc, scene = resolve_grading_choice(
        {"mode": "auto", "auto_map": {"normal": "clean_gray"}}, None, {"is_hdr": False},
        classify_fn=lambda: {"class": "normal"})
    assert gc["preset"] == "clean_gray"


def test_auto_hdr_skips_classification() -> None:
    def _should_not_run():
        raise AssertionError("HDR 源不应跑 signalstats 分类")
    gc, scene = resolve_grading_choice(
        {"mode": "auto"}, None, {"is_hdr": True}, classify_fn=_should_not_run)
    assert scene["class"] == "normal" and gc["preset"] == DEFAULT_AUTO_MAP["normal"]
    assert "HDR" in scene["reason"]


# ---------------- 输出侧 bt709 tag 决策 ----------------
def test_sdr_output_tag_conditions() -> None:
    assert sdr_output_tag({"color_transfer": "smpte170m"}, tonemapped=True) == (True, None)  # tonemap 后必 tag
    assert sdr_output_tag({"color_transfer": "bt709", "color_primaries": "bt709"}, False) == (True, None)
    assert sdr_output_tag({}, False) == (True, None)  # 未标注 → 可 tag
    tag, warn = sdr_output_tag({"color_transfer": "smpte170m", "color_primaries": "smpte170m"}, False)
    assert tag is False and "709" in warn  # 罕见非 709 SDR 不硬 tag
