"""propcut 选曲画像（music profile）单测 — 不跑真 ffmpeg。

覆盖：profile 展开优先级 / categories 有序 fallback / 不允许键 / profile 名不存在 /
fade_in·music_start 的 export 回归与生效。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from propcut.config import ConfigError, load_config
from propcut.export import build_export_command
from propcut.music import choose_music


def _write_profile_config(tmp_path: Path, profiles_body: str, music_extra: str = "",
                          with_profiles_file: bool = True) -> Path:
    """写一份 music.enabled=true 的配置 + 一个 profiles.yaml，返回配置路径。

    music_extra 里的行必须缩进 2 空格（music 节下）。with_profiles_file=False 时
    不写 music.profiles_file（用仓库自带默认文件）。
    """
    (tmp_path / "in").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    (lib / "chill").mkdir(parents=True, exist_ok=True)
    (lib / "instrumental").mkdir(parents=True, exist_ok=True)
    (lib / "instrumental" / "a.mp3").write_bytes(b"x")

    pf = tmp_path / "profiles.yaml"
    pf.write_text(profiles_body, encoding="utf-8")

    # profiles.yaml 与配置同目录 → 用裸文件名，验证"相对路径相对配置文件所在目录"解析
    pf_line = '  profiles_file: "profiles.yaml"\n' if with_profiles_file else ""
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n'
        f"{pf_line}{music_extra}",
        encoding="utf-8")
    return cfg


_DEMO_PROFILE = (
    "profiles:\n"
    "  demo:\n"
    "    categories: [chill, instrumental]\n"
    "    volume: 0.5\n"
    "    fade_in: 1.5\n"
    "    fade_out: 2.5\n"
    "    music_start: 3.0\n"
    "    original_audio: remove\n"
    '    notes: "just a note"\n'
)


def test_profile_expansion_priority(tmp_path: Path) -> None:
    # volume 用户显式给 → 覆盖 profile；fade_in/music_start/fade_out 来自 profile；
    # original_volume 谁都没给 → DEFAULTS。
    cfg = load_config(_write_profile_config(
        tmp_path, _DEMO_PROFILE, "  profile: demo\n  volume: 0.9\n"))
    mus = cfg["music"]
    assert mus["volume"] == 0.9              # 用户显式 > profile
    assert mus["fade_in"] == 1.5            # profile > DEFAULTS
    assert mus["music_start"] == 3.0        # profile
    assert mus["fade_out"] == 2.5           # profile
    assert mus["original_volume"] == 1.0    # DEFAULTS（谁都没设）
    assert mus["categories"] == ["chill", "instrumental"]
    assert mus["profile"] == "demo"
    assert "notes" not in mus               # notes 是给人读的，不进运行配置


def test_no_profile_keeps_current_behavior(tmp_path: Path) -> None:
    # 不设 profile → 不出现 categories 键（兼容现状），fade_in/music_start 默认 0
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    lib = tmp_path / "lib"
    (lib / "chill").mkdir(parents=True)
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n',
        encoding="utf-8")
    mus = load_config(cfg_path)["music"]
    assert "categories" not in mus
    assert mus["fade_in"] == 0.0 and mus["music_start"] == 0.0
    assert mus["profile"] is None


def test_bundled_profile_modern_apartment(tmp_path: Path) -> None:
    # 用仓库自带 configs/music_profiles.yaml（不指定 profiles_file）验证真实文件可解析
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    lib = tmp_path / "lib"
    (lib / "luxury_elegant").mkdir(parents=True)
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n'
        f"  profile: modern_apartment\n",
        encoding="utf-8")
    mus = load_config(cfg_path)["music"]
    assert mus["categories"] == ["luxury_elegant", "cozy_warm"]
    assert mus["volume"] == 0.8
    assert mus["fade_in"] == 1.0
    assert mus["profile"] == "modern_apartment"


def test_categories_ordered_fallback(tmp_path: Path) -> None:
    # 首选 chill 是空目录 → 落到第二个 instrumental
    lib = tmp_path / "lib"
    (lib / "chill").mkdir(parents=True)
    (lib / "instrumental").mkdir(parents=True)
    (lib / "instrumental" / "a.mp3").write_bytes(b"x")
    cfg = {"enabled": True, "library": str(lib), "select": "random",
           "categories": ["chill", "instrumental"]}
    picked = choose_music(cfg, random.Random(0)).path
    assert picked.parent.name == "instrumental"


def test_categories_all_missing_errors_with_available(tmp_path: Path) -> None:
    # 想要的分类全空/不存在 → 报错并列出实际可用分类
    lib = tmp_path / "lib"
    (lib / "other").mkdir(parents=True)
    (lib / "other" / "a.mp3").write_bytes(b"x")
    cfg = {"enabled": True, "library": str(lib), "select": "random",
           "categories": ["chill", "jazz"]}
    with pytest.raises(ConfigError, match="other") as exc:
        choose_music(cfg, random.Random(0))
    assert "chill" in str(exc.value)  # 报错含想要的分类


def test_profile_disallowed_key_rejected(tmp_path: Path) -> None:
    body = (
        "profiles:\n"
        "  bad:\n"
        "    categories: [chill]\n"
        '    library: "/x"\n'          # library 是运行侧决策，profile 无权设
    )
    with pytest.raises(ConfigError, match="library"):
        load_config(_write_profile_config(tmp_path, body, "  profile: bad\n"))


def test_profile_name_not_found_lists_available(tmp_path: Path) -> None:
    body = "profiles:\n  demo:\n    categories: [chill]\n"
    with pytest.raises(ConfigError, match="ghost") as exc:
        load_config(_write_profile_config(tmp_path, body, "  profile: ghost\n"))
    assert "demo" in str(exc.value)  # 列出可用 profile 名


def test_profile_file_missing_rejected(tmp_path: Path) -> None:
    # profile 指定但 profiles_file 指向不存在的文件 → 报错
    cfg = _write_profile_config(tmp_path, "profiles:\n  demo:\n    categories: [chill]\n",
                                "  profile: demo\n")
    # 覆写成不存在的 profiles_file
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{(tmp_path / "lib").as_posix()}"\n'
        f'  profiles_file: "{(tmp_path / "nope.yaml").as_posix()}"\n  profile: demo\n',
        encoding="utf-8")
    with pytest.raises(ConfigError, match="profiles 文件不存在"):
        load_config(cfg)


_EXPORT_CFG = {"resolution": "original", "fit": "pad", "quality": "high"}
_MUSIC_BASE = {"volume": 0.8, "original_volume": 1.0, "fade_out": 2.0, "original_audio": "remove"}


def test_export_defaults_no_fade_in_no_music_start(tmp_path: Path) -> None:
    # fade_in/music_start 未给（默认 0）→ 命令不含新片段（与现状一致）
    build, _, _ = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", _EXPORT_CFG,
        Path("m.mp3"), _MUSIC_BASE, audio_present=True)
    fc = build([])[build([]).index("-filter_complex") + 1]
    assert "afade=t=in" not in fc
    assert "atrim" not in fc and "asetpts" not in fc
    # 音乐链仍以 volume 开头，与现状字节一致
    assert "[1:a]volume=0.8" in fc


def test_export_fade_in_and_music_start_present() -> None:
    cfg = {**_MUSIC_BASE, "fade_in": 1.5, "music_start": 2.0}
    build, _, _ = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", _EXPORT_CFG,
        Path("m.mp3"), cfg, audio_present=True)
    fc = build([])[build([]).index("-filter_complex") + 1]
    # music_start 用 atrim+asetpts（与 -stream_loop 组合正确），在 volume 之前
    assert "atrim=start=2.000,asetpts=PTS-STARTPTS,volume=0.8" in fc
    # fade_in 在 volume 之后、fade_out 之前
    assert "afade=t=in:st=0:d=1.500" in fc
    assert fc.index("afade=t=in") < fc.index("afade=t=out")


# ---- FIX-1: 用户显式 null 键不遮蔽 profile ----

def test_null_user_key_does_not_shadow_profile(tmp_path: Path) -> None:
    # demo profile 设 music_start=3.0；用户写 music_start: null（= 未设置）不应遮蔽 profile
    cfg = load_config(_write_profile_config(
        tmp_path, _DEMO_PROFILE, "  profile: demo\n  music_start: null\n"))
    mus = cfg["music"]
    assert mus["music_start"] == 3.0    # null 未遮蔽 → profile 值保留
    assert mus["volume"] == 0.5         # profile
    assert mus["fade_out"] == 2.5       # profile


# ---- FIX-2: profile 选曲与用户显式选曲互斥 ----

def test_profile_conflict_with_explicit_selection(tmp_path: Path) -> None:
    # profile 有 categories + 用户显式 select/category → fail-fast
    with pytest.raises(ConfigError, match="冲突") as exc:
        load_config(_write_profile_config(
            tmp_path, _DEMO_PROFILE,
            "  profile: demo\n  select: category\n  category: chill\n"))
    assert "select" in str(exc.value)


def test_profile_conflict_with_explicit_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="冲突"):
        load_config(_write_profile_config(
            tmp_path, _DEMO_PROFILE, "  profile: demo\n  file: chill/x.mp3\n"))


def test_profile_with_null_selection_keys_ok(tmp_path: Path) -> None:
    # select/category/file 显式为 null → 不算冲突（null = 未设置），profile 选曲仍生效
    cfg = load_config(_write_profile_config(
        tmp_path, _DEMO_PROFILE,
        "  profile: demo\n  select: null\n  category: null\n  file: null\n"))
    mus = cfg["music"]
    assert mus["categories"] == ["chill", "instrumental"]  # profile 负责选曲
    assert mus["select"] == "random"   # null 未遮蔽 → 回落默认


def test_no_profile_explicit_select_category_regression(tmp_path: Path) -> None:
    # 无 profile 的老配置显式 select=category 完全不受 FIX-2 影响
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    lib = tmp_path / "lib"
    (lib / "chill").mkdir(parents=True)
    (lib / "chill" / "a.mp3").write_bytes(b"x")
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n'
        f"  select: category\n  category: chill\n",
        encoding="utf-8")
    mus = load_config(cfg_path)["music"]
    assert mus["select"] == "category" and mus["category"] == "chill"
    assert "categories" not in mus     # 无 profile → 无 categories 键


# ---- FIX-5: fade_out ≥0 校验 + profiles_file 须为文件 ----

def test_fade_out_negative_rejected(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n  fade_out: -1.0\n',
        encoding="utf-8")
    with pytest.raises(ConfigError, match="fade_out"):
        load_config(cfg_path)


def test_profiles_file_directory_rejected(tmp_path: Path) -> None:
    # profiles_file 指向目录 → ConfigError（而非裸 PermissionError）
    a_dir = tmp_path / "profiles_dir"
    a_dir.mkdir()
    _write_profile_config(tmp_path, "profiles:\n  demo:\n    categories: [chill]\n",
                          "  profile: demo\n")
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{(tmp_path / "lib").as_posix()}"\n'
        f'  profiles_file: "{a_dir.as_posix()}"\n  profile: demo\n',
        encoding="utf-8")
    with pytest.raises(ConfigError, match="profiles 文件"):
        load_config(cfg)


# ---- FIX-4: fade_in 钳制到 target_dur/2 ----

def test_export_fade_in_clamped_to_half_duration() -> None:
    cfg = {**_MUSIC_BASE, "fade_in": 100.0}
    build, target_dur, _ = build_export_command(
        Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, "", _EXPORT_CFG,
        Path("m.mp3"), cfg, audio_present=True)
    assert target_dur == 10.0
    fc = build([])[build([]).index("-filter_complex") + 1]
    assert "afade=t=in:st=0:d=5.000" in fc   # 钳到 target_dur/2 = 5.0


# ---- FIX-3: 仓库真实 example.yaml 可解析，且调性行注释化后 profile 值胜出 ----

def test_example_yaml_loads_and_profile_wins(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parent.parent / "configs" / "propcut.example.yaml"
    text = example.read_text(encoding="utf-8")
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    text = (text
            .replace('"./input"', f'"{(tmp_path / "in").as_posix()}"')
            .replace('"./output"', f'"{(tmp_path / "out").as_posix()}"')
            .replace('"./music"',
                     f'"{lib.as_posix()}"'))

    # profile: null（出厂）→ 正常解析
    base_cfg = tmp_path / "base.yaml"
    base_cfg.write_text(text, encoding="utf-8")
    base = load_config(base_cfg)["music"]
    assert base["profile"] is None
    assert base["volume"] == 0.8 and base["fade_out"] == 2.0   # 默认值

    # 切到 luxury profile → profile 值胜出（调性行已注释，不再遮蔽）
    lux_cfg = tmp_path / "lux.yaml"
    lux_cfg.write_text(text.replace("profile: null", "profile: luxury"), encoding="utf-8")
    mus = load_config(lux_cfg)["music"]
    assert mus["profile"] == "luxury"
    assert mus["volume"] == 0.75    # luxury（若 volume: 0.8 行未注释会被遮蔽成 0.8）
    assert mus["fade_out"] == 2.5   # luxury（同上，2.0 会遮蔽）
    assert mus["categories"] == ["luxury_elegant"]
