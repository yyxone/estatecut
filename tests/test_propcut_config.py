"""propcut 配置 / overrides / 选曲 / 滤镜与命令构造单测 — 不跑真 ffmpeg。"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from propcut.config import ConfigError, load_config, load_overrides, match_override, parse_resolution
from propcut.export import build_export_command
from propcut.grading import build_grading_filter
from propcut.music import choose_music, scan_library


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "in").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f"music:\n  enabled: false\n{extra}",
        encoding="utf-8")
    return cfg


def test_config_missing_input_dir_fails(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text('output:\n  dir: "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="input.dir"):
        load_config(cfg)


def test_config_defaults_merged(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path))
    assert cfg["detect"]["scan_seconds"] == 30.0
    assert cfg["export"]["quality"] == "high"
    assert cfg["output"]["overwrite"] is False


def test_config_bad_resolution_fails(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "export:\n  resolution: huge\n")
    with pytest.raises(ConfigError, match="resolution"):
        load_config(path)


def test_config_suffix_path_escape_rejected(tmp_path: Path) -> None:
    for bad in ("../x", "a/b", "a\\b", "C:evil"):
        # YAML 单引号：反斜杠无转义语义，"a\b" 原样进 suffix
        path = _write_config(tmp_path, f"output:\n  dir: \"{(tmp_path / 'out').as_posix()}\"\n  suffix: '{bad}'\n")
        with pytest.raises(ConfigError, match="suffix"):
            load_config(path)


def _write_music_config(tmp_path: Path, lib: Path, *, output_dir: Path, ledger: str | None) -> Path:
    (tmp_path / "in").mkdir(exist_ok=True)
    led_line = f"  usage_ledger: \"{ledger}\"\n" if ledger is not None else ""
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{output_dir.as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n'
        f'  select: category\n  category: calm\n{led_line}',
        encoding="utf-8")
    return cfg


def test_config_output_dir_in_library_rejected(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "calm").mkdir(parents=True)
    (lib / "calm" / "x.mp3").write_bytes(b"x")
    out_in_lib = lib / "sub_out"          # 输出目录指进曲库
    cfg = _write_music_config(tmp_path, lib, output_dir=out_in_lib, ledger=None)
    with pytest.raises(ConfigError, match="曲库"):
        load_config(cfg)


def test_config_ledger_in_library_rejected(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "calm").mkdir(parents=True)
    (lib / "calm" / "x.mp3").write_bytes(b"x")
    (tmp_path / "out").mkdir()
    ledger_in_lib = (lib / "calm" / "ledger.jsonl").resolve().as_posix()  # 显式台账指进曲库曲目区
    cfg = _write_music_config(tmp_path, lib, output_dir=tmp_path / "out", ledger=ledger_in_lib)
    with pytest.raises(ConfigError, match="曲库"):
        load_config(cfg)


def test_config_auto_ledger_and_outside_output_ok(tmp_path: Path) -> None:
    # auto 台账在曲库 exports/（music/ 之外）+ 输出目录在曲库外 → 守卫不触发
    lib = tmp_path / "eml" / "music" / "01_approved" / "commercial"
    (lib / "calm").mkdir(parents=True)
    (lib / "calm" / "x.mp3").write_bytes(b"x")
    (tmp_path / "out").mkdir()
    cfg = _write_music_config(tmp_path, lib, output_dir=tmp_path / "out", ledger="auto")
    loaded = load_config(cfg)
    assert loaded["music"]["usage_ledger"] == "auto"


def test_slug_conflict_fails_fast(tmp_path: Path) -> None:
    from propcut.pipeline import _check_slug_conflicts
    root = tmp_path / "in"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b.mp4").write_bytes(b"x")
    (root / "a__b.mp4").write_bytes(b"x")
    with pytest.raises(ValueError, match="冲突"):
        _check_slug_conflicts([root / "a" / "b.mp4", root / "a__b.mp4"], root)
    _check_slug_conflicts([root / "a" / "b.mp4"], root)  # 无冲突不抛


def test_parse_resolution() -> None:
    assert parse_resolution("original") is None
    assert parse_resolution("1080x1920") == (1080, 1920)


def test_overrides_match_by_relpath_and_basename(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path))
    ov_file = tmp_path / "overrides.json"
    ov_file.write_text(json.dumps({
        "video_001.mp4": {"start_time": 7.8},
        "sub/video_002.mp4": {"start_time": 11.2},
    }), encoding="utf-8")
    cfg["overrides"]["file"] = str(ov_file.resolve())
    overrides = load_overrides(cfg)
    assert match_override(overrides, Path("video_001.mp4"))["start_time"] == 7.8
    assert match_override(overrides, Path("sub") / "video_002.mp4")["start_time"] == 11.2
    # basename 兜底 + 大小写不敏感
    assert match_override(overrides, Path("deep") / "VIDEO_001.MP4")["start_time"] == 7.8
    assert match_override(overrides, Path("video_003.mp4")) is None


def test_overrides_negative_start_rejected(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path))
    ov_file = tmp_path / "overrides.json"
    ov_file.write_text(json.dumps({"a.mp4": {"start_time": -3}}), encoding="utf-8")
    cfg["overrides"]["file"] = str(ov_file.resolve())
    with pytest.raises(ConfigError, match="start_time"):
        load_overrides(cfg)


def _make_library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    (lib / "calm").mkdir(parents=True)
    (lib / "upbeat").mkdir(parents=True)
    (lib / "calm" / "piano.mp3").write_bytes(b"x")
    (lib / "calm" / "strings.mp3").write_bytes(b"x")
    (lib / "upbeat" / "pop.mp3").write_bytes(b"x")
    (lib / "root_track.mp3").write_bytes(b"x")
    (lib / "notes.txt").write_bytes(b"x")  # 非音乐文件应被忽略
    return lib


def test_scan_library_categories(tmp_path: Path) -> None:
    catalog = scan_library(_make_library(tmp_path))
    assert sorted(k for k in catalog if k) == ["calm", "upbeat"]
    assert len(catalog["calm"]) == 2
    assert len(catalog[""]) == 1  # 根目录直属


def test_choose_music_modes(tmp_path: Path) -> None:
    lib = _make_library(tmp_path)
    base = {"enabled": True, "library": str(lib), "file": None, "category": None}
    rng = random.Random(42)
    picked = choose_music({**base, "select": "category", "category": "calm"}, rng).path
    assert picked.parent.name == "calm"
    picked = choose_music({**base, "select": "file", "file": "upbeat/pop.mp3"}, rng).path
    assert picked.name == "pop.mp3"
    # 固定 seed 的 random 可复现
    a = choose_music({**base, "select": "random"}, random.Random(1)).path
    b = choose_music({**base, "select": "random"}, random.Random(1)).path
    assert a == b
    # override 优先于配置
    picked = choose_music({**base, "select": "random"}, rng, override_music="calm/piano.mp3").path
    assert picked.name == "piano.mp3"
    assert choose_music({"enabled": False}, rng).path is None
    with pytest.raises(ConfigError, match="分类"):
        choose_music({**base, "select": "category", "category": "nope"}, rng)


def test_grading_presets() -> None:
    from estatecut.exceptions import GradingError

    presets = {"neutral": {"filter": ""}, "bright": {"filter": "eq=brightness=0.06"}}
    assert build_grading_filter({"mode": "none", "preset": "bright"}, presets) == ""
    assert build_grading_filter({"mode": "preset", "preset": "neutral"}, presets) == ""
    assert build_grading_filter({"mode": "preset", "preset": "bright"}, presets) == "eq=brightness=0.06"
    # grading.py 解耦后抛 GradingError（estatecut 共享层），不再依赖 propcut.config.ConfigError
    with pytest.raises(GradingError, match="不存在"):
        build_grading_filter({"mode": "preset", "preset": "missing"}, presets)


def test_bundled_presets_all_load() -> None:
    from propcut.grading import load_presets
    presets = load_presets()
    for name in ("neutral", "bright_interior", "warm_interior", "cloudy_exterior", "luxury_clean"):
        assert name in presets
        build_grading_filter({"mode": "preset", "preset": name}, presets)


def test_grading_filter_self_loads_when_presets_none() -> None:
    # 全局 grading.mode=none（presets 没预加载）+ override 点了 preset → 传 None 自行加载
    assert build_grading_filter({"mode": "preset", "preset": "bright_interior"}, None) != ""


MUSIC_CFG = {"volume": 0.8, "original_volume": 1.0, "fade_out": 2.0, "original_audio": "remove"}
EXPORT_CFG = {"resolution": "original", "fit": "pad", "quality": "high"}


def test_export_command_music_remove(tmp_path: Path) -> None:
    build, target, mode = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 4.0, 10.0, "", EXPORT_CFG,
        Path("m.mp3"), MUSIC_CFG, audio_present=True)
    argv = build(["-c:v", "libx264"])
    assert mode == "remove"
    assert target == 6.0
    assert argv[argv.index("-ss") + 1] == "4.000"
    assert "-stream_loop" in argv and argv[argv.index("-t") + 1] == "6.000"
    fc = argv[argv.index("-filter_complex") + 1]
    assert "volume=0.8" in fc and "afade=t=out" in fc and "amix" not in fc


def test_export_command_mix_downgrades_without_source_audio() -> None:
    _, _, mode = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", EXPORT_CFG,
        Path("m.mp3"), {**MUSIC_CFG, "original_audio": "mix"}, audio_present=False)
    assert mode == "remove"


def test_export_command_mix_with_source_audio() -> None:
    build, _, mode = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", EXPORT_CFG,
        Path("m.mp3"), {**MUSIC_CFG, "original_audio": "mix"}, audio_present=True)
    fc = build([])[build([]).index("-filter_complex") + 1]
    # longest：源音轨短于视频流时音乐不跟着断（-t 兜底截断）；normalize=0：不被 amix 减半音量
    assert "amix=inputs=2:duration=longest" in fc and "normalize=0" in fc


def test_export_command_keep_ignores_music() -> None:
    build, _, mode = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", EXPORT_CFG,
        Path("m.mp3"), {**MUSIC_CFG, "original_audio": "keep"}, audio_present=True)
    argv = build([])
    assert mode == "keep"
    # keep 只 map 第一条音频流：防 iPhone .MOV 的第二条 apac 空间音频轨（无解码器）拖垮重编码
    assert "-stream_loop" not in argv and "0:a:0?" in argv and "0:a?" not in argv


def test_export_command_color_tag_flag() -> None:
    # 默认不 tag（向后兼容）；color_tag=True 加 bt709 输出 tag
    build, _, _ = build_export_command(Path("i.mp4"), Path("o.mp4"), 0.0, 10.0, "",
                                       EXPORT_CFG, None, MUSIC_CFG, True)
    assert "-colorspace" not in build([])
    build, _, _ = build_export_command(Path("i.mp4"), Path("o.mp4"), 0.0, 10.0, "",
                                       EXPORT_CFG, None, MUSIC_CFG, True, color_tag=True)
    argv = build([])
    assert argv[argv.index("-colorspace") + 1] == "bt709"
    assert "-color_primaries" in argv and "-color_trc" in argv


def test_export_command_remove_without_music_is_silent() -> None:
    build, _, mode = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", EXPORT_CFG,
        None, MUSIC_CFG, audio_present=True)
    assert mode == "remove_silent"
    assert "-an" in build([])


def test_export_command_scale_pad_and_crop() -> None:
    cfg = {**EXPORT_CFG, "resolution": "1080x1920", "fit": "pad"}
    build, _, _ = build_export_command(Path("i.mp4"), Path("o.mp4"), 0.0, 10.0,
                                       "eq=contrast=1.1", cfg, None, MUSIC_CFG, True)
    fc = build([])[build([]).index("-filter_complex") + 1]
    assert "eq=contrast=1.1" in fc and "pad=1080:1920" in fc
    cfg["fit"] = "crop"
    build, _, _ = build_export_command(Path("i.mp4"), Path("o.mp4"), 0.0, 10.0,
                                       "", cfg, None, MUSIC_CFG, True)
    fc = build([])[build([]).index("-filter_complex") + 1]
    assert "crop=1080:1920" in fc
