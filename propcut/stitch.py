"""多片段拼接（stitch）：把多个源片段按 EDL 拼成一条成片，整条配一首背景音乐。

架构（视频全程只编码一代，零二次画质损失）：
1. 逐段：输入侧 `-ss` + 输出侧 `-t` 帧精确裁剪 + 统一调色/尺寸/帧率，编码到
   output.dir/_stitch_work/seg_NNN.mp4（无音轨）；段级缓存（源/剪点/滤镜/质量
   不变则复用，换音乐重跑秒级完成）。
2. concat demuxer `-c copy` 无损拼接成无声整片。
3. 终混：choose_music（目标时长 = 拼接总长）→ 音乐 filter 链 + `-map 0:v -c:v copy`。

EDL 在配置 stitch 节（clips 有序列表，in/out 秒；省略 = 整段）。
v1 硬切无转场；不保留原声（段间原声不连续，stitch 只支持 original_audio=remove）。
输出总是覆盖（同 reprocess 语义：改 EDL/音乐重跑是核心流程）——出新配乐版请换
stitch.output 版本名，旧版成片不覆盖。
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from estatecut.exceptions import FFmpegError
from estatecut.ffmpeg_tools import probe_media, run_command
from estatecut.utils import ensure_dir, now_iso, read_json, require_outside, write_json

from . import grading, music
from .config import ConfigError, load_config, parse_resolution, resolve_usage_ledger
from .export import (AUDIO_ARGS, _encode_with_fallback, build_measure_command,
                     build_music_audio_chain, loudnorm_filter, measure_timeline_loudness,
                     scale_pad_filter)
from .grading import load_presets
from .utils import src_identity

BT709_TAG_ARGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


def _seg_identity(clip: dict[str, Any], vchain: str, quality: str, color_tag: bool) -> dict[str, Any]:
    """段缓存身份（C2）：源指纹用 src_identity（size+mtime_ns+头尾 hash）——
    旧的秒级浮点 st_mtime 在快速覆盖/跨文件系统拷贝时可撞同值，同名换源会静默
    复用旧段；legacy sidecar（mtime 浮点）自然不匹配 → 该段一次性重编码。"""
    return {"src": str(clip["src"]), "src_identity": src_identity(clip["src"]),
            "in": clip["in"], "out": clip["out"], "vchain": vchain,
            "quality": quality, "color_tag": color_tag}


def _effective_dims(probe: dict[str, Any]) -> tuple[int, int]:
    """解码后（ffmpeg autorotate 之后）的有效宽高：rotation ±90/270 交换 width/height。"""
    w, h = int(probe["width"]), int(probe["height"])
    rot = probe.get("rotation")
    if rot is not None and abs(int(rot)) % 180 == 90:
        return h, w
    return w, h


def _concat_escape(p: Path) -> str:
    """ffconcat 列表路径转义：' → '\\''（同 talkcut/ingest.py 的写法）。"""
    return p.as_posix().replace("'", "'\\''")


def _resolve_clips(stitch_cfg: dict[str, Any], input_dir: Path) -> list[dict[str, Any]]:
    """EDL → 逐段解析：文件存在性 + 剪点对时长的合法性（out 超长钳到时长）。"""
    clips: list[dict[str, Any]] = []
    for idx, c in enumerate(stitch_cfg["clips"]):
        src = (input_dir / c["file"]).resolve()
        # containment 先于任何探测：../ 或绝对路径逃出 input.dir = 读任意媒体，拒绝
        if not src.is_relative_to(input_dir.resolve()):
            raise ConfigError(f"stitch.clips[{idx}].file 逃出 input.dir，拒绝: {src}")
        if not src.is_file():
            raise ConfigError(f"stitch.clips[{idx}].file 不存在于 input.dir: {src}")
        probe = probe_media(src)
        duration = float(probe["duration_sec"])
        if duration <= 0:
            raise ConfigError(f"stitch.clips[{idx}] ffprobe 读不到时长，文件可能损坏: {src}")
        cin = float(c.get("in") or 0.0)
        cout = min(float(c["out"]), duration) if c.get("out") is not None else duration
        if not cin < cout:
            raise ConfigError(
                f"stitch.clips[{idx}] 剪点无效: in={cin} out={cout}（{c['file']} 时长 {duration:.2f}s）")
        clips.append({
            "index": idx, "src": src, "file": c["file"],
            "in": round(cin, 3), "out": round(cout, 3),
            "color_preset": c.get("color_preset"), "probe": probe,
        })
    return clips


def _encode_segment(clip: dict[str, Any], seg_path: Path, vchain: str,
                    quality: str, color_tag: bool, log: Path) -> None:
    """单段：帧精确裁剪 + 调色/规格归一，编码为无音轨中间片（.part 原子替换）。"""
    seg_dur = clip["out"] - clip["in"]
    tag_args = BT709_TAG_ARGS if color_tag else []
    tmp = seg_path.with_name(seg_path.stem + ".part" + seg_path.suffix)

    def build_argv(enc_args: list[str]) -> list[str]:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{clip['in']:.3f}", "-i", str(clip["src"]),
                "-vf", vchain, "-an", *enc_args, "-pix_fmt", "yuv420p", *tag_args,
                "-t", f"{seg_dur:.3f}", "-movflags", "+faststart", str(tmp)]

    try:
        _encode_with_fallback(build_argv, quality, log)
        tmp.replace(seg_path)
    finally:
        tmp.unlink(missing_ok=True)


def run_stitch(config_path: Path) -> dict[str, Any]:
    """stitch 模式主入口：EDL → 段编码（带缓存）→ concat → 配乐终混 → 报告。"""
    config_path = Path(config_path)
    cfg = load_config(config_path)
    st = cfg["stitch"]
    if not st.get("output"):
        raise ConfigError("stitch 模式需要 stitch.output（成片文件名，落 output.dir）")
    if not st.get("clips"):
        raise ConfigError("stitch 模式需要 stitch.clips（有序 EDL，至少一段）")
    if cfg["music"]["enabled"] and cfg["music"]["original_audio"] != "remove":
        raise ConfigError(
            "stitch v1 只支持 music.original_audio=remove：硬切拼接后段间原声不连续，"
            "keep/mix 语义不成立（要原声请走单视频 process 管线）")

    input_dir = Path(cfg["input"]["dir"])
    output_dir = Path(cfg["output"]["dir"])
    require_outside(output_dir, input_dir)  # 硬约束：绝不写进源媒体目录树
    ensure_dir(output_dir)
    reports_dir = ensure_dir(output_dir / "_reports")

    out_path = output_dir / st["output"]
    if out_path.suffix.lower() != ".mp4":
        out_path = out_path.with_suffix(".mp4")
    slug = out_path.stem
    # 中间片按 slug 分目录：多条 stitch 配置共用一个 output.dir 时段缓存互不踩
    work_dir = ensure_dir(output_dir / "_stitch_work" / slug)
    ffmpeg_log = reports_dir / f"stitch_{slug}_ffmpeg.log"

    run_id = uuid.uuid4().hex[:12]  # R5 L6：与 process 管线同源的 run 溯源标识
    entry: dict[str, Any] = {
        "mode": "stitch", "output": str(out_path), "slug": slug, "run_id": run_id,
        "success": False, "error": None, "clips": [], "warnings": [],
        "profile": cfg["music"].get("profile"), "ts": now_iso(),
    }
    try:
        clips = _resolve_clips(st, input_dir)

        # 统一段规格：export.resolution 显式给了用它；original = 首段有效尺寸（rotation 已折算）。
        # 帧率取首段。regularize 是 concat demuxer -c copy 的前提（各段编码参数必须一致）。
        res = parse_resolution(cfg["export"]["resolution"])
        tw, th = res if res is not None else _effective_dims(clips[0]["probe"])
        fps = float(clips[0]["probe"]["fps"]) or 30.0
        scale_f = scale_pad_filter(tw, th, cfg["export"]["fit"])
        entry["target"] = {"width": tw, "height": th, "fps": round(fps, 3)}

        gr = cfg["grading"]
        custom_pf = Path(gr["presets_file"]) if gr.get("presets_file") else None
        presets = (load_presets(custom_pf) if (gr["mode"] in ("preset", "auto") or custom_pf) else {})
        hdr_on = gr.get("hdr", "auto") == "auto"

        # ---- 1) 逐段编码（段级缓存：源/剪点/滤镜/质量不变则复用）----
        seg_paths: list[Path] = []
        for clip in clips:
            color_info = grading.probe_color_info(clip["src"])
            tonemapped = bool(color_info.get("is_hdr") and hdr_on)
            grading_cfg, auto_scene = grading.resolve_grading_choice(
                gr, clip["color_preset"], color_info,
                classify_fn=lambda s=clip["src"]: grading.classify_scene(
                    s, thresholds=gr.get("auto_thresholds")))
            color_chain = grading.build_color_chain(color_info, grading_cfg, presets or None)
            color_tag, tag_warn = grading.sdr_output_tag(color_info, tonemapped)
            if tag_warn:
                entry["warnings"].append(f"clips[{clip['index']}] {tag_warn}")
            vchain = ",".join(p for p in (color_chain, scale_f, f"fps={fps:.3f}") if p)

            seg_path = work_dir / f"seg_{clip['index']:03d}.mp4"
            sidecar = seg_path.with_suffix(".json")
            identity = _seg_identity(clip, vchain, cfg["export"]["quality"], color_tag)
            reused = False
            if seg_path.exists() and sidecar.exists():
                try:
                    reused = read_json(sidecar) == identity
                except (OSError, json.JSONDecodeError):
                    reused = False
            if not reused:
                _encode_segment(clip, seg_path, vchain, cfg["export"]["quality"], color_tag, ffmpeg_log)
                write_json(sidecar, identity)
            seg_probe = probe_media(seg_path)
            seg_paths.append(seg_path)
            clip_entry = {
                "file": clip["file"], "in": clip["in"], "out": clip["out"],
                "seg": seg_path.name, "seg_duration": round(seg_probe["duration_sec"], 3),
                "color_preset": grading_cfg["preset"] if grading_cfg["mode"] == "preset" else None,
                "hdr_tonemapped": tonemapped, "reused_cache": reused,
            }
            if auto_scene is not None:
                clip_entry["auto_scene"] = auto_scene
            entry["clips"].append(clip_entry)
            print(f"[propcut:stitch] [{'cache' if reused else 'enc'}] "
                  f"{clip['file']} {clip['in']:.2f}-{clip['out']:.2f}s → {seg_path.name}")

        # ---- 2) concat demuxer 无损拼接（各段同参数编码，-c copy 零画质损失）----
        list_file = work_dir / f"{slug}_concat.txt"
        list_file.write_text(
            "".join(f"file '{_concat_escape(p)}'\n" for p in seg_paths), encoding="utf-8")
        silent = work_dir / f"{slug}_silent.mp4"
        run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "concat", "-safe", "0", "-i", str(list_file),
                     "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart",
                     str(silent)], ffmpeg_log)
        total_dur = float(probe_media(silent)["duration_sec"])
        entry["total_duration"] = round(total_dur, 3)

        # ---- 3) 选曲 + 终混（视频流 copy，只编音频）----
        seed = cfg["music"].get("seed")
        if seed is None:
            seed = int(time.time())
        entry["music_seed"] = seed
        rng = random.Random(seed)
        ledger_path: Path | None = None
        if cfg["music"]["enabled"]:
            ledger_path, ledger_warn = resolve_usage_ledger(cfg["music"], Path(cfg["_config_dir"]))
            if ledger_warn:
                entry["warnings"].append(ledger_warn)
            choice = music.choose_music(cfg["music"], rng, ledger_path=ledger_path)
        else:
            choice = music.MusicChoice(None, dedup_mode="none")
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

        tmp = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
        try:
            if track is not None:
                try:
                    track_dur = float(probe_media(Path(track))["duration_sec"]) or None
                except Exception:
                    track_dur = None  # 探不出曲长 → 循环消接缝退回现状硬接
                # 响度归一（opt-in）：第一遍测最终音频时间线；失败降级不归一 + warning
                ln_cfg = cfg["music"].get("loudnorm") or {}
                measured = None
                if ln_cfg.get("enabled"):
                    measured, ln_warn = measure_timeline_loudness(build_measure_command(
                        silent, 0.0, total_dur, track, cfg["music"], mix_original=False,
                        music_track_dur=track_dur))
                    entry["loudnorm"] = {"applied": measured is not None,
                                         "target_i": float(ln_cfg["i"])}
                    if measured is not None:
                        entry["loudnorm"]["measured_i"] = float(measured["input_i"])
                    if ln_warn:
                        entry["warnings"].append(ln_warn)
                chain = build_music_audio_chain(
                    cfg["music"], total_dur, mix_original=False,
                    out_label="[premix]" if measured is not None else "[outa]",
                    track_dur=track_dur)
                if measured is not None:
                    chain += f";[premix]{loudnorm_filter(ln_cfg, measured)}[outa]"
                run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                             "-i", str(silent), "-stream_loop", "-1", "-i", str(track),
                             "-filter_complex", chain, "-map", "0:v", "-map", "[outa]",
                             "-c:v", "copy", *AUDIO_ARGS, "-t", f"{total_dur:.3f}",
                             "-movflags", "+faststart", str(tmp)], ffmpeg_log)
            else:  # 音乐关闭 → 无声成片直接 copy
                run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                             "-i", str(silent), "-map", "0:v", "-c", "copy",
                             "-movflags", "+faststart", str(tmp)], ffmpeg_log)

            # ---- 替换前校验（在 .part 上做：时长/音轨/尺寸；坏产物绝不覆盖旧成片）----
            out_probe = probe_media(tmp)
            entry["output_duration"] = round(out_probe["duration_sec"], 3)
            dur_ok = abs(out_probe["duration_sec"] - total_dur) < 0.5
            audio_ok = out_probe["audio_present"] == (track is not None)
            dims_ok = (out_probe["width"], out_probe["height"]) == (tw, th)
            if not (tmp.stat().st_size > 0 and dur_ok and audio_ok and dims_ok):
                raise FFmpegError(
                    f"stitch 导出校验失败（.part 已丢弃，旧成片未覆盖）: "
                    f"时长 {out_probe['duration_sec']:.2f}s vs {total_dur:.2f}s，"
                    f"audio={out_probe['audio_present']}，"
                    f"尺寸 {out_probe['width']}x{out_probe['height']} vs {tw}x{th}")
            # 无声整片只服务本轮终混。先清掉它再发布已验证的 .part，确保 success
            # 永远不伴随一个几乎等大的 _silent 副本；seg_* 仍保留供换音乐复用。
            try:
                silent.unlink()
            except OSError as exc:
                raise FFmpegError(
                    f"stitch 无声中间片清理失败，拒绝发布成品: {silent}: {exc}") from exc
            entry["silent_intermediate_cleaned"] = True
            tmp.replace(out_path)
        finally:
            tmp.unlink(missing_ok=True)

        # ---- 接缝抽帧（拼接点前后各 1 帧，供人工核对；失败只记警告不炸）----
        boundary = 0.0
        for i, ce in enumerate(entry["clips"][:-1], start=1):
            boundary += ce["seg_duration"]
            for tag, t in (("a", boundary - 1.5 / fps), ("b", boundary + 0.5 / fps)):
                jpg = reports_dir / f"stitch_{slug}_seam{i:02d}_{tag}.jpg"
                try:
                    run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                 "-ss", f"{max(0.0, t):.3f}", "-i", str(out_path),
                                 "-frames:v", "1", "-vf", "scale=-2:480", str(jpg)])
                except Exception as exc:  # noqa: BLE001
                    entry["warnings"].append(f"seam{i:02d}_{tag} 抽帧失败: {exc}")

        # ---- 使用台账（成片确认存在后 append；写失败不炸）----
        if track is not None and ledger_path is not None:
            write_warn = music.append_usage_ledger(ledger_path, {
                "at": now_iso(), "run_id": run_id,
                "pipeline": "propcut", "video": out_path.name,
                "source_video": out_path.name,
                "track_path": str(Path(track).resolve()), "track_name": Path(track).name,
                "category": music.track_category(cfg["music"]["library"], track),
                "explicit": choice.explicit, "pool": choice.pool,
                "license_scope": choice.license_scope,
                "commercial_ok": choice.commercial_ok, "config": config_path.name,
            })
            if write_warn:
                entry["music_dedup"]["ledger_write_warning"] = write_warn
            sel_warn = music.append_selection_log(ledger_path, {
                "at": now_iso(), "run_id": run_id, "pipeline": "propcut_stitch",
                "video": out_path.name, "source_video": out_path.name,
                "config": config_path.name,
                "profile": cfg["music"].get("profile"),
                "categories": cfg["music"].get("categories"),
                "pool": choice.pool,
                "license_scope": choice.license_scope,
                "commercial_ok": choice.commercial_ok,
                "dedup_mode": choice.dedup_mode, "explicit": choice.explicit,
                "favorites_mode": choice.favorites_mode,
                "pool_size": choice.pool_size, "excluded_count": choice.excluded_count,
                "selected_track": str(Path(track).resolve()),
                "selected_name": Path(track).name,
                "selected_category": music.track_category(cfg["music"]["library"], track),
            })
            if sel_warn:
                entry["music_dedup"]["selection_log_warning"] = sel_warn

        entry["success"] = True
    except Exception as exc:  # 记录进报告/日志后按失败返回（CLI 据 success 定 exit code）
        if isinstance(exc, (ConfigError, ValueError)):
            raise  # 配置类错误直接抛给用户看全文（与其他模式 load_config 行为一致）
        entry["error"] = f"{type(exc).__name__}: {exc}"

    # ---- 报告落盘：stitch 详情 + run jsonl ----
    write_json(reports_dir / f"stitch_{slug}.json", entry)
    # 文件名带 run_id：同秒内两次 stitch 不再覆写同名 jsonl（round-2 补，与 pipeline 同源）。
    run_log = reports_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{run_id}.jsonl"
    with run_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if entry["success"]:
        print(f"[propcut:stitch] [ok] {out_path.name} — {entry['total_duration']:.1f}s，"
              f"{len(entry['clips'])} 段，音乐: {Path(entry['music']).name if entry['music'] else '无'}")
    else:
        print(f"[propcut:stitch] [FAIL] {out_path.name} — {entry['error']}")
    return entry
