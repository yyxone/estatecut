"""批量编排：扫描 → 检测（或复用缓存）→ overrides 合并 → 导出 → 报告。

三种模式：
- detect    只分析 + 关键帧截图 + 落盘检测结果，不导出；
- process   完整处理（检测缓存存在则复用，--force-detect 强制重测）；
- reprocess 只用已有检测结果 + overrides 重新导出，不重分析。

产物（output.dir 下）：
- <slug>_cut.mp4                      成片
- _reports/<slug>.json                检测结果缓存（reprocess 复用）
- _reports/<slug>_{before,selected,after}.jpg   检测点关键帧（override 生效时另存 *_override_*.jpg）
- _reports/run_<ts>.jsonl             本次运行逐视频日志
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from estatecut.ffmpeg_tools import probe_media, run_command
from estatecut.utils import ensure_dir, now_iso, read_json, require_outside, write_json

from . import analyze, export, grading, music
from .analyze import ANALYZER_VERSION
from .config import load_config, load_overrides, match_override, resolve_usage_ledger
from .grading import load_presets
from .utils import src_identity


def scan_videos(cfg: dict[str, Any]) -> list[Path]:
    input_dir = Path(cfg["input"]["dir"])
    exts = set(cfg["input"]["extensions"])
    pattern = "**/*" if cfg["input"]["recursive"] else "*"
    return sorted(p for p in input_dir.glob(pattern) if p.is_file() and p.suffix.lower() in exts)


def _slug(rel_path: Path) -> str:
    return str(rel_path.with_suffix("")).replace("\\", "__").replace("/", "__")


DETECT_CFG_KEYS = ("scan_seconds", "sample_fps", "stable_window", "max_trim_seconds")


def _load_detection_cache(reports_dir: Path, slug: str) -> dict[str, Any] | None:
    cache = reports_dir / f"{slug}.json"
    if not cache.exists():
        return None
    data = read_json(cache)
    return data if isinstance(data, dict) and "detection" in data else None


def _cache_matches(cached: dict[str, Any], src: Path, duration: float, detect_cfg: dict[str, Any]) -> bool:
    """缓存身份校验：源路径 / 源内容指纹 / 时长 / 检测配置任一变了都不能复用旧裁点。

    路径+时长兜不住"同名替换"（重拍同机位时长几乎一样）——src_identity
    （size+mtime_ns+头尾 hash）才认内容；legacy 缓存（无 src_identity）一律重测。
    """
    if cached.get("video") != str(src):
        return False
    if cached.get("src_identity") != src_identity(src):
        return False
    # detect_cfg 只覆盖用户配置项，analyze 内置阈值/融合逻辑改版靠 ANALYZER_VERSION 判失效
    if cached.get("analyzer_version") != ANALYZER_VERSION:
        return False
    if abs(float(cached.get("probe", {}).get("duration_sec", -1)) - duration) > 0.05:
        return False
    return cached.get("detect_cfg") == {k: detect_cfg.get(k) for k in DETECT_CFG_KEYS}


INTENT_VERSION = 5


def _export_intent(
    src: Path,
    entry: dict[str, Any],
    grading_cfg: dict[str, Any],
    grading_filter: str | None,
    color_tag: Any,
    export_cfg: dict[str, Any],
    music_cfg: dict[str, Any],
    override_music: Any = None,
) -> dict[str, Any]:
    """输出身份意图面 v4：会改变成片内容的每个参数都在此（Codex R3 F6 + round-2/3 P1）。

    v2 漏了 music.enabled（决定是否选曲）、loudnorm 面（两遍响度归一改最终音频链）
    与确定性选曲请求（select/file/category + per-video override_music）——这些改了
    旧片都会被当成当前参数的产物 skipped-success。v3 又漏了 music.library：
    choose_music 把相对 file/override 拼到当前曲库下解析，换曲库、同相对文件名 =
    物理曲目已变，intent 必须失配（round-3 P1），故 library 按 resolve() 归一记入。
    **随机选曲的结果**（具体选中哪首）仍不进意图面：同配置合法轮换，实际曲目单独记
    sidecar `selected_music`（provenance，换曲重导走 reprocess）。intent_version
    升级后旧 sidecar 必失配 → 跳过判定按"不符"报错，reprocess 一次即补记。
    """
    lib = music_cfg.get("library")
    pools_file = music_cfg.get("pools_file") if music_cfg.get("pool") else None
    return {
        "intent_version": INTENT_VERSION,
        "src_identity": src_identity(src),
        "start_time_used": entry["start_time_used"],
        "grading": {"mode": grading_cfg["mode"], "preset": entry["color_preset"],
                    "hdr_tonemapped": entry["hdr_tonemapped"],
                    "filter": grading_filter, "color_tag": color_tag},
        "quality": export_cfg["quality"],
        "resolution": export_cfg.get("resolution"),
        "fit": export_cfg.get("fit"),
        "music": {**{k: music_cfg.get(k) for k in (
            "enabled", "original_audio", "volume", "original_volume",
            "fade_in", "fade_out", "music_start", "profile", "categories",
            "pool", "license_scope", "select", "file", "category", "loudnorm")},
            "library": str(Path(lib).resolve()) if lib else None,
            "pools_file": str(Path(pools_file).resolve()) if pools_file else None,
            "pools_file_identity": src_identity(Path(pools_file)) if pools_file else None,
            "override_music": override_music},
    }


def _existing_output_ok(out_path: Path) -> bool:
    """overwrite=false 跳过前体检：已存在输出必须非空、可 probe、尾部可真解码。

    0 字节先短路（不用 ffmpeg）；probe 失败 / 无宽度 / 无时长都算坏文件。
    probe 只读 metadata——+faststart 的 mp4 截断后 probe 照常通过，所以再抽
    尾部 2s 真解码（写入中断/复制截断先坏尾部）；全片逐帧解码留给显式 QA，
    跳过路径不背全量成本。坏文件不能记 success/skipped，否则用户拿着
    "已完成"的半截片去发布。
    """
    try:
        if out_path.stat().st_size <= 0:
            return False
        info = probe_media(out_path)
        if int(info.get("width") or 0) <= 0 or float(info.get("duration_sec") or 0.0) <= 0:
            return False
        run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
                     "-sseof", "-2", "-i", str(out_path), "-f", "null", "-"])
        return True
    except Exception:
        return False


def _check_slug_conflicts(videos: list[Path], input_dir: Path) -> None:
    """slug 碰撞（如 a/b.mp4 与 a__b.mp4）会互相覆盖检测缓存和输出 → 开跑前 fail-fast。"""
    seen: dict[str, Path] = {}
    for v in videos:
        s = _slug(v.relative_to(input_dir))
        if s in seen:
            raise ValueError(f"输出名冲突：{seen[s]} 与 {v} 都映射到 `{s}`，请重命名其一或调整目录结构")
        seen[s] = v


def _process_one(
    src: Path,
    rel: Path,
    cfg: dict[str, Any],
    mode: str,
    overrides: dict[str, dict[str, Any]],
    presets: dict[str, Any],
    rng: random.Random,
    reports_dir: Path,
    force_detect: bool,
    ledger_path: Path | None,
    config_name: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    slug = _slug(rel)
    entry: dict[str, Any] = {
        "video": str(src), "rel_path": str(rel), "slug": slug,
        "success": False, "skipped": False, "error": None,
        "override_used": False, "output": None,
        "profile": cfg["music"].get("profile"),  # detect-only / 错误路径条目也带该键，schema 一致
        "ts": now_iso(),
    }
    probe = probe_media(src)
    duration = probe["duration_sec"]
    entry["duration_sec"] = round(duration, 3)
    if duration <= 0:
        entry["error"] = "ffprobe 读不到时长，文件可能损坏"
        return entry

    # ---- 检测（或复用缓存；缓存身份不匹配 = 源文件被换/配置变了，绝不静默复用）----
    cached = _load_detection_cache(reports_dir, slug)
    cache_ok = cached is not None and _cache_matches(cached, src, duration, cfg["detect"])
    if mode == "reprocess":
        if cached is None:
            entry["error"] = f"reprocess 需要已有检测结果，但 _reports/{slug}.json 不存在（先跑 detect/process）"
            return entry
        if not cache_ok:
            entry["error"] = (f"_reports/{slug}.json 与当前源文件/检测配置不匹配（源被替换或配置已改），"
                              f"reprocess 拒绝用旧裁点——先跑 process --force-detect 重测")
            return entry
        detection = cached["detection"]
        keyframes = cached.get("keyframes", {})
    elif cache_ok and not force_detect:
        detection = cached["detection"]
        keyframes = cached.get("keyframes", {})
    else:
        detection = analyze.detect_start(src, cfg["detect"], duration)
        keyframes = analyze.save_keyframes(src, detection["detected_start_time"], duration, reports_dir, slug)
        write_json(reports_dir / f"{slug}.json", {
            "video": str(src), "rel_path": str(rel),
            "src_identity": src_identity(src),
            "analyzer_version": ANALYZER_VERSION,
            "detection": detection, "keyframes": keyframes,
            "detect_cfg": {k: cfg["detect"].get(k) for k in DETECT_CFG_KEYS},
            "probe": {k: probe[k] for k in ("duration_sec", "width", "height", "fps", "audio_present")},
            "created": now_iso(),
        })

    ov = match_override(overrides, rel) or {}
    start = float(ov["start_time"]) if "start_time" in ov else float(detection["detected_start_time"])
    entry["override_used"] = "start_time" in ov
    entry.update({
        "detected_start_time": detection["detected_start_time"],
        "start_time_used": round(start, 3),
        "trimmed_seconds": round(start, 3),
        "confidence": detection["confidence"],
        "reason": detection["reason"],
        "keyframes": keyframes,
    })
    if start >= duration - 0.1:
        entry["error"] = f"start_time {start:.2f}s 超出视频长度 {duration:.2f}s，请检查 override"
        return entry
    if entry["override_used"]:
        # override 起点和检测点不同 → 另拍一组关键帧供复查（不覆盖检测快照）
        entry["keyframes_override"] = analyze.save_keyframes(src, start, duration, reports_dir, f"{slug}_override")

    # 剪切预览印张：铺出 0→start 被剪掉的内容（红=剪/绿=留 + 清晰度），让过度裁切一眼可见——
    # 常规 before/selected/after 三帧看不到"剪了什么"，209 型误剪就靠这条印张防住
    entry["cut_filmstrip"] = analyze.save_cut_filmstrip(src, start, duration, reports_dir, slug)

    # ---- 色彩探测 + 调色决策（detect 模式也算，供人核对；export 复用）----
    color_info = grading.probe_color_info(src)
    entry["color_transfer"] = color_info.get("color_transfer")
    if color_info.get("probe_error"):
        entry["color_probe_warning"] = color_info["probe_error"]
    hdr_on = cfg["grading"].get("hdr", "auto") == "auto"
    entry["hdr_tonemapped"] = bool(color_info.get("is_hdr") and hdr_on)

    grading_cfg, auto_scene = grading.resolve_grading_choice(
        cfg["grading"], ov.get("color_preset"), color_info,
        classify_fn=lambda: grading.classify_scene(src, thresholds=cfg["grading"].get("auto_thresholds")))
    entry["color_preset"] = grading_cfg["preset"] if grading_cfg["mode"] == "preset" else None
    if auto_scene is not None:
        entry["auto_scene"] = auto_scene

    if mode == "detect" or cfg["run"]["detect_only"]:
        entry["success"] = True
        entry["skipped"] = True  # 未导出
        return entry

    # ---- 导出 ----
    # correction(HDR tonemap) + style(预设) 拼接；presets 为空（全局 mode=none）但 override/auto
    # 点了 preset → 传 None 让 grading 自行加载预设文件。
    # 在 overwrite=false 跳过判定之前算——意图面（F6）要比对实际调色滤镜串。
    grading_filter = grading.build_color_chain(color_info, grading_cfg, presets or None)
    color_tag, tag_warn = grading.sdr_output_tag(color_info, entry["hdr_tonemapped"])
    if tag_warn:
        entry["color_warning"] = tag_warn

    # reprocess 的语义就是"重新导出"（改 override 后重导是核心流程）→ 该模式总是允许覆盖
    out_path = Path(cfg["output"]["dir"]) / f"{slug}{cfg['output']['suffix']}.mp4"
    allow_overwrite = cfg["output"]["overwrite"] or mode == "reprocess"
    if out_path.exists() and not allow_overwrite:
        if not _existing_output_ok(out_path):
            entry["error"] = (f"输出已存在但校验不通过（0 字节/不可解码），不记 success；"
                              f"用 reprocess 或 output.overwrite=true 重导: {out_path}")
            entry["output"] = str(out_path)
            return entry
        # 健康还要同源同参：sidecar 记录的"源+参数"必须与本次意图一致，否则是
        # 换源同名/改参后的旧产物——记 skipped-success 等于替用户确认了错东西（C3）。
        # 无 sidecar 的 legacy 输出同样 fail-closed（F7）：warning 没人看，
        # skipped-success 就是在替用户背书一份来历不明的片子。
        sidecar_path = reports_dir / f"{slug}.output.json"
        if not sidecar_path.exists():
            entry["error"] = (f"输出已存在但无身份 sidecar（_reports/{slug}.output.json），"
                              f"无法证明这份旧片由当前源+参数生成，不记 success；"
                              f"用 reprocess 或 output.overwrite=true 重导补记: {out_path}")
            entry["output"] = str(out_path)
            return entry
        try:
            recorded = read_json(sidecar_path)
        except (OSError, json.JSONDecodeError):
            recorded = None
        intent = _export_intent(src, entry, grading_cfg, grading_filter, color_tag,
                                cfg["export"], cfg["music"], override_music=ov.get("music"))
        if not isinstance(recorded, dict) or {k: recorded.get(k) for k in intent} != intent:
            entry["error"] = (f"输出已存在但与当前源/参数不符（非本意图产物）——"
                              f"用 reprocess 或 output.overwrite=true 重导: {out_path}")
            entry["output"] = str(out_path)
            return entry
        # 意图一致还要成片本身没被动过：out_identity 防"导出后被同名替换"的旧片过关（F5 同族）
        if recorded.get("out_identity") != src_identity(out_path):
            entry["error"] = (f"输出文件与 sidecar 记录的指纹不符——成片在导出后被替换/改动过，"
                              f"不记 success；用 reprocess 或 output.overwrite=true 重导: {out_path}")
            entry["output"] = str(out_path)
            return entry
        entry["error"] = f"输出已存在且 output.overwrite=false，跳过: {out_path}"
        entry["skipped"] = True
        entry["success"] = True
        entry["output"] = str(out_path)
        return entry

    # keep = 保留原声不加音乐（export 会丢弃 music_path）→ 不选曲、不占去重窗、不写台账
    if cfg["music"]["original_audio"] == "keep":
        choice = music.MusicChoice(None, dedup_mode="none")
    else:
        choice = music.choose_music(cfg["music"], rng, override_music=ov.get("music"),
                                    ledger_path=ledger_path)
    track = choice.path
    entry["music"] = str(track) if track else None
    entry["music_dedup"] = {
        "mode": choice.dedup_mode, "excluded": choice.excluded_count,
        "explicit": choice.explicit, "favorites_mode": choice.favorites_mode,
        "pool": choice.pool, "license_scope": choice.license_scope,
        "commercial_ok": choice.commercial_ok,
    }
    if choice.warning:
        entry["music_dedup"]["warning"] = choice.warning

    ffmpeg_log = reports_dir / f"{slug}_ffmpeg.log"
    result = export.export_video(
        src, out_path, start, duration, grading_filter,
        cfg["export"], track, cfg["music"], probe["audio_present"],
        log=ffmpeg_log, color_tag=color_tag)
    entry.update({"output": result["output"], "target_duration": result["target_duration"],
                  "audio_mode": result["audio_mode"]})
    if "loudnorm" in result:
        entry["loudnorm"] = result["loudnorm"]

    # 导出后校验：文件非空、时长接近目标、音频轨符合模式
    out_probe = probe_media(out_path)
    entry["output_duration"] = round(out_probe["duration_sec"], 3)
    dur_ok = abs(out_probe["duration_sec"] - result["target_duration"]) < 1.0
    # keep 模式跟随源（源无音轨则输出也无）；remove/mix 有音乐必有；remove_silent 必无
    expected_audio = (result["audio_mode"] in ("remove", "mix")
                      or (result["audio_mode"] == "keep" and probe["audio_present"]))
    audio_ok = out_probe["audio_present"] == expected_audio
    if not (out_path.stat().st_size > 0 and out_probe["width"] > 0 and dur_ok and audio_ok):
        entry["error"] = (f"导出校验失败: 时长 {out_probe['duration_sec']:.2f}s vs 目标 "
                          f"{result['target_duration']:.2f}s，audio={out_probe['audio_present']}")
        return entry

    # ---- 输出身份 sidecar（C3/F6）：这份成片由哪个源 + 哪套完整参数生成——overwrite=false
    # 跳过前据此校验意图一致 + out_identity 防成片被换。旧版 intent_version 与当前
    # 意图面必不相等 → 跳过判定按"不符"报错，reprocess 一次即补记。selected_music =
    # 实际选中曲目的 provenance（P1-3），只记录不参与意图比对（同配置合法轮换）。
    write_json(reports_dir / f"{slug}.output.json", {
        **_export_intent(src, entry, grading_cfg, grading_filter, color_tag,
                         cfg["export"], cfg["music"], override_music=ov.get("music")),
        "selected_music": None if track is None else {
            "path": str(Path(track).resolve()),
            "name": Path(track).name,
            "category": music.track_category(cfg["music"]["library"], track),
            "identity": src_identity(Path(track)),
            "explicit": choice.explicit,
            "pool": choice.pool,
            "license_scope": choice.license_scope,
            "commercial_ok": choice.commercial_ok,
        },
        "out_identity": src_identity(out_path),
        "created": now_iso(),
    })

    # ---- 导出成功（成品已确认存在）→ append 使用台账（去重窗口 + 跨管线用量记录）----
    # 台账在曲库项目 exports/（非曲目区），是"音乐库永不写入"约束的唯一 append-only 例外；写失败不炸。
    if track is not None and ledger_path is not None:
        write_warn = music.append_usage_ledger(ledger_path, {
            "at": now_iso(),
            "run_id": run_id,  # R5 L6：同秒多 run 追加同一台账时按 run 溯源
            "pipeline": "propcut",
            "video": out_path.name,
            "source_video": src.name,
            "track_path": str(Path(track).resolve()),
            "track_name": Path(track).name,
            "category": music.track_category(cfg["music"]["library"], track),
            "explicit": choice.explicit,
            "pool": choice.pool,
            "license_scope": choice.license_scope,
            "commercial_ok": choice.commercial_ok,
            "config": config_name,
        })
        if write_warn:
            entry["music_dedup"]["ledger_write_warning"] = write_warn
        sel_warn = music.append_selection_log(ledger_path, {
            "at": now_iso(),
            "run_id": run_id,
            "pipeline": "propcut",
            "video": out_path.name,
            "source_video": src.name,
            "config": config_name,
            "profile": cfg["music"].get("profile"),
            "categories": cfg["music"].get("categories"),
            "pool": choice.pool,
            "license_scope": choice.license_scope,
            "commercial_ok": choice.commercial_ok,
            "dedup_mode": choice.dedup_mode,
            "explicit": choice.explicit,
            "favorites_mode": choice.favorites_mode,
            "pool_size": choice.pool_size,
            "excluded_count": choice.excluded_count,
            "selected_track": str(Path(track).resolve()),
            "selected_name": Path(track).name,
            "selected_category": music.track_category(cfg["music"]["library"], track),
        })
        if sel_warn:
            entry["music_dedup"]["selection_log_warning"] = sel_warn

    entry["success"] = True
    return entry


def run(config_path: Path, mode: str = "process", only: str | None = None,
        force_detect: bool = False) -> list[dict[str, Any]]:
    assert mode in {"detect", "process", "reprocess"}
    config_path = Path(config_path)
    cfg = load_config(config_path)
    input_dir = Path(cfg["input"]["dir"])
    output_dir = Path(cfg["output"]["dir"])
    require_outside(output_dir, input_dir)  # 硬约束：绝不写进源媒体目录树
    reports_dir = ensure_dir(output_dir / "_reports")

    videos = scan_videos(cfg)
    if only:
        videos = [v for v in videos if only.lower() in v.name.lower()]
    if not videos:
        print(f"[propcut] 没有找到匹配的视频（input={input_dir}, only={only!r}）")
        return []
    _check_slug_conflicts(videos, input_dir)

    overrides = load_overrides(cfg)
    # preset / auto 都要预设表；自定义 presets_file 存在时也预加载（override 可指向其中预设）
    gr = cfg["grading"]
    custom_pf = Path(gr["presets_file"]) if gr.get("presets_file") else None
    presets = (load_presets(custom_pf) if (gr["mode"] in ("preset", "auto") or custom_pf) else {})
    seed = cfg["music"].get("seed")
    if seed is None:
        seed = int(time.time())
    rng = random.Random(seed)

    # 选曲去重 / 用量台账：music 关或推导不出（自建库布局）→ 台账关闭（去重退化、不写入），警告一次
    ledger_path: Path | None = None
    if cfg["music"]["enabled"]:
        ledger_path, ledger_warn = resolve_usage_ledger(cfg["music"], Path(cfg["_config_dir"]))
        if ledger_warn:
            print(f"[propcut] {ledger_warn}")
    config_name = config_path.name
    run_id = uuid.uuid4().hex[:12]  # R5 L6：贯穿本次 run 的台账/日志行，消同秒歧义

    # 文件名带 run_id：同秒内两次 run 不再覆写同名 jsonl（round-2 补——run_id 之前只进
    # 行内容，同秒并发/连跑仍会撞同一文件名互相截断）。
    run_log = reports_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{run_id}.jsonl"
    results: list[dict[str, Any]] = []
    for src in videos:
        rel = src.relative_to(input_dir)
        try:
            entry = _process_one(src, rel, cfg, mode, overrides, presets, rng,
                                 reports_dir, force_detect, ledger_path, config_name,
                                 run_id=run_id)
        except Exception as exc:  # 单条失败不中断批处理
            entry = {"video": str(src), "rel_path": str(rel), "success": False,
                     "skipped": False, "error": f"{type(exc).__name__}: {exc}", "ts": now_iso()}
        entry["mode"] = mode
        entry["music_seed"] = seed
        entry["run_id"] = run_id
        results.append(entry)
        with run_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        flag = "ok" if entry["success"] and not entry.get("error") else ("skip" if entry.get("skipped") else "FAIL")
        detail = entry.get("error") or (
            f"start={entry.get('start_time_used', '?')}s conf={entry.get('confidence', '?')}"
            + (" [override]" if entry.get("override_used") else ""))
        print(f"[propcut:{mode}] [{flag}] {rel} — {detail}")

    ok = sum(1 for e in results if e["success"] and not e.get("error"))
    fail = sum(1 for e in results if not e["success"])
    print(f"[propcut] 完成 {ok}/{len(results)}（失败 {fail}）；日志: {run_log}")
    return results
