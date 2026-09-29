"""propcut 配置加载与校验（fail-fast，缺什么明说什么）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from estatecut.resources_path import resource_path

VALID_GRADING_MODES = {"preset", "none", "auto"}
VALID_HDR_MODES = {"auto", "off"}
VALID_MUSIC_SELECT = {"file", "random", "category"}
VALID_LICENSE_SCOPES = {"personal", "commercial", "user_selected"}
VALID_ORIGINAL_AUDIO = {"keep", "remove", "mix"}
VALID_FIT = {"pad", "crop"}
VALID_QUALITY = {"high", "medium", "low"}

# music profile 可设键 = music 节的子集（notes 是给人读的选曲指引，机器不套用）。
# library / select / file / category / enabled / profiles_file / pools_file / license_scope
# 是运行侧决策，profile 无权设。
# meta 键不进运行配置：notes 给人读；selection 给推荐器（propcut.suggest）读的结构化选曲元数据。
_PROFILE_NOTES_KEY = "notes"
_PROFILE_META_KEYS = {"notes", "selection"}
ALLOWED_PROFILE_KEYS = {
    "categories", "pool", "volume", "fade_in", "fade_out", "loudnorm",
    "music_start", "original_audio", "original_volume", "seed", *_PROFILE_META_KEYS,
}

# music.loudnorm 默认：两遍 loudnorm 响度归一（EBU R128），默认关（opt-in）。
# i/tp/lra 是**内部 mastering 基准**（成片间一致性用），不是任何平台的投稿规格——
# 平台规格导出时实查（AGENTS.md 硬约束 8），别把这组数当"抖音/小红书要求"。
_LOUDNORM_DEFAULTS: dict[str, Any] = {"enabled": False, "i": -14.0, "tp": -1.5, "lra": 11.0}

# music.favorites 默认：favorites 偏好加权选曲（P1-1），默认关（opt-in）。
# 偏好只认 selection_log 的 explicit 换曲序列（Steven 点名 = 正、被换掉的自动选 = 负），
# 使用频次不算偏好（那是随机选择的结果）。boost = 正信号权重倍数（负信号取倒数）；
# exploration = 探索位概率（从无信号新曲里均匀选，保新曲能被听到）。
_FAVORITES_DEFAULTS: dict[str, Any] = {"enabled": False, "boost": 3.0, "exploration": 0.25}

DEFAULTS: dict[str, dict[str, Any]] = {
    "input": {"recursive": False, "extensions": [".mp4", ".mov", ".m4v"]},
    "output": {"suffix": "_cut", "overwrite": False},
    "detect": {
        "scan_seconds": 30.0,
        "sample_fps": 8,
        "stable_window": 2.0,
        "max_trim_seconds": None,
    },
    "overrides": {"enabled": True, "file": "overrides.json"},
    "grading": {
        "mode": "preset",          # preset | none | auto（auto = signalstats 场景分类选预设）
        "preset": "neutral",
        "hdr": "auto",             # auto = HDR/log 源自动 tonemap 转 SDR；off = 直通
        "presets_file": None,      # 自定义预设文件（多类型视频接口）；None = 仓库自带
        "auto_thresholds": None,   # auto 模式分类阈值覆盖（dict，缺省用硬编码）
        "auto_map": None,          # auto 分类 → 预设映射覆盖（dict，缺省用硬编码）
    },
    "music": {
        "enabled": True,
        "profile": None,          # 选曲画像名（见 configs/music_profiles.yaml）；None = 不用 profile
        "profiles_file": None,    # profile 定义文件；None = 仓库自带 configs/music_profiles.yaml
        "pool": None,             # 精选曲池名（见 pools_file）；优先于 categories/category/random
        "pools_file": None,       # 精选曲池文件；None = 仓库自带 configs/music_curated_pools.yaml
        "license_scope": "commercial",  # commercial = 只用 commercial_ok；personal = 曲池全量
        "select": "random",
        "file": None,
        "category": None,
        "seed": None,
        "volume": 0.8,
        "fade_in": 0.0,           # 音乐淡入秒数（0 = 现状：不淡入）
        "original_audio": "remove",
        "original_volume": 1.0,
        "fade_out": 2.0,
        "music_start": 0.0,       # 跳过曲子开头 N 秒空 intro（0 = 现状：从头播）
        "no_repeat_window": 30,   # 近 N 条视频用过的曲子不再选（0 = 关闭去重）；显式指定的曲子放行
        "usage_ledger": "off",   # auto = 从 library 标准布局推导曲库 exports/ 台账；off = 关；或显式路径
        "loudnorm": None,         # 成片响度归一（两遍 loudnorm）；None/缺省 = 关。见 _LOUDNORM_DEFAULTS
        "favorites": None,        # favorites 偏好加权选曲；None/缺省 = 关。见 _FAVORITES_DEFAULTS
    },
    "export": {"resolution": "original", "fit": "pad", "quality": "high"},
    "run": {"detect_only": False},
    # stitch 模式（多片段拼接成单条成片）专用；其他模式忽略本节。
    "stitch": {
        "output": None,   # 成片文件名（落 output.dir）；stitch 模式必填
        "clips": [],      # 有序 EDL：[{file, in, out, color_preset?}]，in/out 秒，省略 = 整段
    },
}


class ConfigError(ValueError):
    pass


def _path_inside(path: Path, tree: Path) -> bool:
    """path 是否位于 tree 子树内（含 tree 自身）。resolve 失败 → False。"""
    try:
        path.resolve().relative_to(tree.resolve())
        return True
    except (ValueError, OSError):
        return False


def _merge_defaults(user: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for section, defaults in DEFAULTS.items():
        merged = dict(defaults)
        user_section = user.get(section) or {}
        if not isinstance(user_section, dict):
            raise ConfigError(f"配置节 `{section}` 必须是 mapping，拿到 {type(user_section).__name__}")
        merged.update(user_section)
        cfg[section] = merged
    return cfg


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def parse_resolution(value: str) -> tuple[int, int] | None:
    """"original" → None；"1920x1080" → (1920, 1080)。"""
    if value == "original":
        return None
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", str(value))
    if not m:
        raise ConfigError(f"export.resolution 须为 original 或 WxH（如 1920x1080），拿到: {value!r}")
    return int(m.group(1)), int(m.group(2))


def _repo_profiles_file() -> Path:
    """仓库自带的选曲画像文件：<repo>/configs/music_profiles.yaml。"""
    return resource_path("music_profiles.yaml")


def _repo_pools_file() -> Path:
    """仓库自带的精选曲池文件：<repo>/configs/music_curated_pools.yaml。"""
    return resource_path("music_curated_pools.yaml")


def _apply_music_profile(cfg: dict[str, Any], raw: dict[str, Any], config_dir: Path) -> None:
    """展开 music.profile → 把画像值填进 music 节。

    优先级：DEFAULTS ← profile ← 用户在 music 节显式给的非 null 键。
    显式键值为 null 视为"未设置"，不遮蔽 profile 值（想覆盖就写具体值）。
    profile 负责选曲（有 categories/pool）时，用户再显式指定另一套选曲方式 = 冲突。
    未指定 profile 时直接返回（categories 键仅在用户直接配时存在，兼容现状）。
    """
    mus = cfg["music"]
    profile_name = mus.get("profile")
    if profile_name is None:
        return

    pf_value = mus.get("profiles_file")
    if pf_value is None:
        profiles_file = _repo_profiles_file()
    else:
        profiles_file = Path(pf_value).expanduser()
        if not profiles_file.is_absolute():  # 相对路径相对配置文件所在目录（与 overrides.file 语义一致）
            profiles_file = config_dir / profiles_file
    _require(profiles_file.is_file(),
             f"music.profile=`{profile_name}` 但 profiles 文件不存在或不是文件: {profiles_file}")

    with profiles_file.open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    _require(isinstance(profiles, dict) and bool(profiles),
             f"profiles 文件缺少 `profiles` 映射: {profiles_file}")
    if profile_name not in profiles:
        raise ConfigError(
            f"music.profile=`{profile_name}` 不在 {profiles_file}，可用: {sorted(profiles)}")

    profile = profiles[profile_name] or {}
    _require(isinstance(profile, dict),
             f"profile `{profile_name}` 必须是 mapping，拿到 {type(profile).__name__}")
    bad = sorted(set(profile) - ALLOWED_PROFILE_KEYS)
    _require(not bad,
             f"profile `{profile_name}` 不允许设置键 {bad}（library/select/file/category/enabled/"
             f"profiles_file/pools_file/license_scope 是运行侧决策，profile 无权设）；可设键: "
             f"{sorted(ALLOWED_PROFILE_KEYS - _PROFILE_META_KEYS)}")

    # profile 负责选曲时，用户不能再显式（非 null）指定另一套选曲方式 → 冲突 fail-fast
    user_music = raw.get("music") or {}
    if profile.get("categories") or profile.get("pool"):
        conflicting = [
            k for k in ("select", "category", "file", "pool", "categories")
            if user_music.get(k) is not None
        ]
        if conflicting:
            raise ConfigError(
                f"music.profile=`{profile_name}` 与显式 {'/'.join(conflicting)} 冲突："
                f"profile 已负责选曲，删掉显式 {'/'.join(conflicting)} 行交给 profile，"
                f"或去掉 profile 自己指定选曲")

    # DEFAULTS ← profile ← 用户显式非 null music 键（null = 未设置，不遮蔽 profile）
    effective = dict(DEFAULTS["music"])
    for key, value in profile.items():
        if key in _PROFILE_META_KEYS:  # 人读指引 / 推荐器元数据，不进运行配置
            continue
        effective[key] = value
    for key, value in user_music.items():
        if value is None:  # 显式 null = 未设置，交给 profile / 默认值
            continue
        effective[key] = value
    cfg["music"] = effective


def load_config(path: Path) -> dict[str, Any]:
    path = Path(path)
    _require(path.exists(), f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    _require(isinstance(raw, dict), f"配置根节点必须是 mapping: {path}")
    cfg = _merge_defaults(raw)

    inp, out = cfg["input"], cfg["output"]
    _require(bool(inp.get("dir")), "input.dir 必填（输入视频目录）")
    _require(bool(out.get("dir")), "output.dir 必填（导出目录）")
    input_dir = Path(inp["dir"]).expanduser()
    _require(input_dir.is_dir(), f"input.dir 不是目录: {input_dir}")
    inp["dir"] = str(input_dir.resolve())
    out["dir"] = str(Path(out["dir"]).expanduser().resolve())
    suffix = out["suffix"]
    _require(isinstance(suffix, str) and not re.search(r"[\\/:]|\.\.", suffix),
             f"output.suffix 不能包含路径分隔符 / 盘符 / ..（防止写出 output.dir 外），拿到: {suffix!r}")
    inp["extensions"] = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in inp["extensions"]]

    det = cfg["detect"]
    _require(float(det["scan_seconds"]) > 0, "detect.scan_seconds 必须 > 0")
    _require(2 <= int(det["sample_fps"]) <= 30, "detect.sample_fps 需在 2-30 之间")
    _require(float(det["stable_window"]) > 0, "detect.stable_window 必须 > 0")

    gr = cfg["grading"]
    _require(gr["mode"] in VALID_GRADING_MODES, f"grading.mode 须为 {sorted(VALID_GRADING_MODES)}，拿到: {gr['mode']!r}")
    _require(gr["hdr"] in VALID_HDR_MODES, f"grading.hdr 须为 {sorted(VALID_HDR_MODES)}，拿到: {gr['hdr']!r}")
    if gr.get("auto_thresholds") is not None:
        _require(isinstance(gr["auto_thresholds"], dict), "grading.auto_thresholds 须为 mapping")
    if gr.get("auto_map") is not None:
        _require(isinstance(gr["auto_map"], dict), "grading.auto_map 须为 mapping")
    if gr.get("presets_file"):
        pf_path = Path(gr["presets_file"]).expanduser()
        if not pf_path.is_absolute():  # 相对路径相对配置文件所在目录（与 overrides.file 语义一致）
            pf_path = path.resolve().parent / pf_path
        _require(pf_path.exists(), f"grading.presets_file 不存在: {pf_path}")
        gr["presets_file"] = str(pf_path.resolve())

    _apply_music_profile(cfg, raw, path.resolve().parent)
    mus = cfg["music"]
    if mus["enabled"]:
        _require(bool(mus.get("library")), "music.enabled=true 时 music.library 必填（音乐库目录）")
        library = Path(mus["library"]).expanduser()
        _require(library.is_dir(), f"music.library 不是目录: {library}")
        mus["library"] = str(library.resolve())
        _require(mus["select"] in VALID_MUSIC_SELECT, f"music.select 须为 {sorted(VALID_MUSIC_SELECT)}，拿到: {mus['select']!r}")
        _require(mus.get("license_scope") in VALID_LICENSE_SCOPES,
                 f"music.license_scope 须为 {sorted(VALID_LICENSE_SCOPES)}，拿到: {mus.get('license_scope')!r}")
        if mus.get("license_scope") == "user_selected":
            _require(bool(mus.get("pool")), "user_selected 仅用于用户明确认可的具名曲池")
        if mus.get("pool") is not None:
            _require(isinstance(mus["pool"], str) and bool(mus["pool"].strip()),
                     f"music.pool 须为非空字符串或 null，拿到: {mus['pool']!r}")
            pools_value = mus.get("pools_file")
            pools_file = _repo_pools_file() if pools_value is None else Path(pools_value).expanduser()
            if not pools_file.is_absolute():
                pools_file = path.resolve().parent / pools_file
            _require(pools_file.is_file(),
                     f"music.pool=`{mus['pool']}` 但 pools 文件不存在或不是文件: {pools_file}")
            mus["pools_file"] = str(pools_file.resolve())
        if mus["select"] == "file":
            _require(bool(mus.get("file")), "music.select=file 时 music.file 必填")
        if mus["select"] == "category":
            _require(bool(mus.get("category")), "music.select=category 时 music.category 必填")
        _require(0 < float(mus["volume"]) <= 2.0, "music.volume 需在 (0, 2] 之间")
        if "categories" in mus:
            cats = mus["categories"]
            _require(isinstance(cats, list) and bool(cats)
                     and all(isinstance(c, str) and c for c in cats),
                     f"music.categories 须为非空字符串列表，拿到: {cats!r}")
        nrw = mus["no_repeat_window"]
        _require(isinstance(nrw, int) and not isinstance(nrw, bool) and nrw >= 0,
                 f"music.no_repeat_window 须为 >=0 的整数，拿到: {nrw!r}")
        _require(isinstance(mus["usage_ledger"], str) and bool(mus["usage_ledger"]),
                 f"music.usage_ledger 须为 auto / off / 路径字符串，拿到: {mus['usage_ledger']!r}")
        # 守卫：输出目录 / 显式台账不得指进曲库曲目区（music.library 树），防误写/误删曲子。
        # auto 台账在曲库 exports/（music/ 之外）天然不撞，无需检查。
        lib_tree = Path(mus["library"])
        if _path_inside(Path(out["dir"]), lib_tree):
            raise ConfigError(f"output.dir 不得指向曲库曲目区（{lib_tree}）")
        led_val = mus["usage_ledger"]
        if led_val not in ("auto", "off"):
            led_path = Path(led_val).expanduser()
            if not led_path.is_absolute():
                led_path = path.resolve().parent / led_path
            if _path_inside(led_path, lib_tree):
                raise ConfigError(f"music.usage_ledger 不得指向曲库曲目区（{lib_tree}）")
    _require(mus["original_audio"] in VALID_ORIGINAL_AUDIO,
             f"music.original_audio 须为 {sorted(VALID_ORIGINAL_AUDIO)}，拿到: {mus['original_audio']!r}")
    _require(isinstance(mus["fade_in"], (int, float)) and float(mus["fade_in"]) >= 0,
             f"music.fade_in 必须是 >=0 的数值，拿到: {mus['fade_in']!r}")
    _require(isinstance(mus["music_start"], (int, float)) and float(mus["music_start"]) >= 0,
             f"music.music_start 必须是 >=0 的数值，拿到: {mus['music_start']!r}")
    _require(isinstance(mus["fade_out"], (int, float)) and float(mus["fade_out"]) >= 0,
             f"music.fade_out 必须是 >=0 的数值，拿到: {mus['fade_out']!r}")

    # loudnorm 子节：None → 默认关；dict → 白名单键 + 范围校验（范围 = ffmpeg loudnorm 滤镜合法域）
    ln = mus.get("loudnorm")
    if ln is None:
        mus["loudnorm"] = dict(_LOUDNORM_DEFAULTS)
    else:
        _require(isinstance(ln, dict), f"music.loudnorm 须为 mapping 或 null，拿到: {type(ln).__name__}")
        unknown = set(ln) - set(_LOUDNORM_DEFAULTS)
        _require(not unknown, f"music.loudnorm 未知键: {sorted(unknown)}（可用: {sorted(_LOUDNORM_DEFAULTS)}）")
        merged_ln = {**_LOUDNORM_DEFAULTS, **ln}
        _require(isinstance(merged_ln["enabled"], bool),
                 f"music.loudnorm.enabled 须为 bool，拿到: {merged_ln['enabled']!r}")
        for key, lo, hi in (("i", -70.0, -5.0), ("tp", -9.0, 0.0), ("lra", 1.0, 50.0)):
            val = merged_ln[key]
            _require(isinstance(val, (int, float)) and not isinstance(val, bool) and lo <= float(val) <= hi,
                     f"music.loudnorm.{key} 须在 [{lo:g}, {hi:g}] 内，拿到: {val!r}")
        mus["loudnorm"] = merged_ln

    # favorites 子节：None → 默认关；dict → 白名单键 + 范围校验
    fav = mus.get("favorites")
    if fav is None:
        mus["favorites"] = dict(_FAVORITES_DEFAULTS)
    else:
        _require(isinstance(fav, dict), f"music.favorites 须为 mapping 或 null，拿到: {type(fav).__name__}")
        unknown = set(fav) - set(_FAVORITES_DEFAULTS)
        _require(not unknown, f"music.favorites 未知键: {sorted(unknown)}（可用: {sorted(_FAVORITES_DEFAULTS)}）")
        merged_fav = {**_FAVORITES_DEFAULTS, **fav}
        _require(isinstance(merged_fav["enabled"], bool),
                 f"music.favorites.enabled 须为 bool，拿到: {merged_fav['enabled']!r}")
        boost = merged_fav["boost"]
        _require(isinstance(boost, (int, float)) and not isinstance(boost, bool)
                 and 1.0 <= float(boost) <= 100.0,
                 f"music.favorites.boost 须在 [1, 100] 内，拿到: {boost!r}")
        expl = merged_fav["exploration"]
        _require(isinstance(expl, (int, float)) and not isinstance(expl, bool)
                 and 0.0 <= float(expl) <= 1.0,
                 f"music.favorites.exploration 须在 [0, 1] 内，拿到: {expl!r}")
        mus["favorites"] = merged_fav

    exp = cfg["export"]
    parse_resolution(exp["resolution"])  # 只校验格式
    _require(exp["fit"] in VALID_FIT, f"export.fit 须为 {sorted(VALID_FIT)}，拿到: {exp['fit']!r}")
    _require(exp["quality"] in VALID_QUALITY, f"export.quality 须为 {sorted(VALID_QUALITY)}，拿到: {exp['quality']!r}")

    # stitch 节（类型/剪点合法性在此；文件存在性在 run_stitch 里对 input.dir 校验）
    st = cfg["stitch"]
    if st.get("output") is not None:
        _require(isinstance(st["output"], str) and not re.search(r"[\\/:]|\.\.", st["output"]),
                 f"stitch.output 是文件名（落 output.dir），不能包含路径分隔符 / 盘符 / ..，拿到: {st['output']!r}")
    _require(isinstance(st.get("clips"), list), f"stitch.clips 须为列表，拿到: {type(st.get('clips')).__name__}")
    for idx, clip in enumerate(st["clips"]):
        _require(isinstance(clip, dict), f"stitch.clips[{idx}] 须为 mapping（{{file, in, out}}）")
        _require(isinstance(clip.get("file"), str) and bool(clip.get("file")),
                 f"stitch.clips[{idx}].file 必填（相对 input.dir 的文件名/路径）")
        for key in ("in", "out"):
            val = clip.get(key)
            if val is not None:
                _require(isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) >= 0,
                         f"stitch.clips[{idx}].{key} 须为 >=0 的数值，拿到: {val!r}")
        if clip.get("in") is not None and clip.get("out") is not None:
            _require(float(clip["in"]) < float(clip["out"]),
                     f"stitch.clips[{idx}] 剪点无效: in={clip['in']} 须 < out={clip['out']}")
        if clip.get("color_preset") is not None:
            _require(isinstance(clip["color_preset"], str) and bool(clip["color_preset"]),
                     f"stitch.clips[{idx}].color_preset 须为非空字符串")

    cfg["_config_dir"] = str(path.resolve().parent)
    return cfg


def load_overrides(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """人工覆盖表：{"video_001.mp4": {"start_time": 7.8, ...}}。

    键可以是文件名（basename）或相对 input.dir 的路径；不存在文件时返回空表。
    """
    ov_cfg = cfg["overrides"]
    if not ov_cfg.get("enabled"):
        return {}
    ov_path = Path(ov_cfg["file"])
    if not ov_path.is_absolute():
        ov_path = Path(cfg["_config_dir"]) / ov_path
    if not ov_path.exists():
        return {}
    data = json.loads(ov_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"overrides 文件根节点必须是 mapping: {ov_path}")
    for key, entry in data.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"override `{key}` 必须是 mapping（如 {{\"start_time\": 7.8}}）")
        if "start_time" in entry:
            st = entry["start_time"]
            if not isinstance(st, (int, float)) or st < 0:
                raise ConfigError(f"override `{key}` 的 start_time 必须是 >=0 的数字，拿到: {st!r}")
    return data


def match_override(overrides: dict[str, dict[str, Any]], rel_path: Path) -> dict[str, Any] | None:
    """先按相对路径精确匹配，再按 basename 匹配（正斜杠归一，大小写不敏感）。"""
    rel_norm = str(rel_path).replace("\\", "/").lower()
    name = rel_path.name.lower()
    by_rel, by_name = None, None
    for key, entry in overrides.items():
        key_norm = key.replace("\\", "/").lower()
        if key_norm == rel_norm:
            by_rel = entry
        elif key_norm == name:
            by_name = entry
    return by_rel if by_rel is not None else by_name


def resolve_usage_ledger(music_cfg: dict[str, Any], config_dir: Path) -> tuple[Path | None, str | None]:
    """选曲去重 / 用量记录台账路径 → (path | None, warning | None)。

    - `off` → (None, None)：关闭台账（不去重、不写入）。
    - `auto`（默认）→ 从 `music.library` 推导：标准布局 `.../music/01_approved/<tier>` 时
      台账 = `Path(library).parents[2]/exports/project_usage_logs/usage_ledger.jsonl`；
      推导不出（自建库布局）→ (None, warning)，视为 off + run 日志警告一次。
    - 显式路径 → 直接用（相对路径相对配置文件所在目录；父目录不存在写入时再建）。
    """
    value = music_cfg.get("usage_ledger", "auto")
    if value == "off":
        return None, None
    if value == "auto":
        library = Path(music_cfg["library"])
        parts = library.parts
        if len(parts) >= 3 and parts[-3] == "music" and parts[-2] == "01_approved":
            ledger = library.parents[2] / "exports" / "project_usage_logs" / "usage_ledger.jsonl"
            return ledger, None
        return None, (
            f"music.usage_ledger=auto 但 music.library 非标准布局（.../music/01_approved/<tier>），"
            f"无法推导曲库台账路径 → 选曲去重已关闭（如需去重请显式配 music.usage_ledger 路径）: {library}")
    ledger = Path(value).expanduser()
    if not ledger.is_absolute():
        ledger = config_dir / ledger
    return ledger, None
