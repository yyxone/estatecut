"""propcut 选曲去重（30 视频窗口 LRU）+ 使用台账单测 — 全 mock、tmp_path，不跑真 ffmpeg。

覆盖：窗口排除生效 / window=0 关闭 / 显式指定绕过 / 池排空 LRU 兜底 /
台账缺失当空 / 坏行跳过 / auto 路径推导（标准布局命中 + 非标准降级 off）/ 台账 append 行 schema。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from propcut.config import resolve_usage_ledger
from propcut.music import append_usage_ledger, choose_music, track_category


def _track(lib: Path, category: str, name: str) -> Path:
    (lib / category).mkdir(parents=True, exist_ok=True)
    p = lib / category / name
    p.write_bytes(b"x")
    return p


def _row(track: Path, at: str, *, explicit: bool = False, category: str = "",
         source_video: str = "in.mp4") -> dict:
    return {"at": at, "pipeline": "propcut", "video": "out.mp4", "source_video": source_video,
            "track_path": str(track.resolve()), "track_name": track.name,
            "category": category, "explicit": explicit, "config": "c.yaml"}


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ---- 窗口排除生效 ----

def test_dedup_excludes_recent(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:00+00:00")])  # a 近期用过 → 应被排除
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    for seed in range(6):  # 池 2 首排掉 1 首 → 无论种子必落 b
        ch = choose_music(cfg, random.Random(seed), ledger_path=ledger)
        assert ch.path.name == "b.mp3", (seed, ch)
        assert ch.dedup_mode == "normal"
        assert ch.excluded_count == 1


def test_dedup_only_uses_last_window_rows(tmp_path: Path) -> None:
    # a 的使用记录在窗口(=1)之外 → 不排除 a；池 [a] 仍可选 a
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    b = _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:00+00:00"),
                         _row(b, "2026-07-10T00:00:01+00:00")])  # 末 1 行只含 b
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 1}
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "a.mp3"      # 只排 b（末 1 行），a 仍在
    assert ch.dedup_mode == "normal" and ch.excluded_count == 1


# ---- 去重窗口按"视频"不按"行"（reprocess 刷行不挤窗） ----

def test_dedup_window_by_video_not_row(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    t0 = _track(lib, "calm", "t0.mp3")
    t1 = _track(lib, "calm", "t1.mp3")
    t2 = _track(lib, "calm", "t2.mp3")
    _track(lib, "calm", "t3.mp3")
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        _row(t0, "2026-07-10T00:00:00+00:00", source_video="v0.mp4"),
        _row(t1, "2026-07-10T00:00:01+00:00", source_video="v1.mp4"),
        _row(t2, "2026-07-10T00:00:02+00:00", source_video="v2.mp4"),
    ]
    # v0 被 reprocess 5 次（刷 5 行，都用 t0）→ 行窗(=3)会把 v1/v2 挤出，视频窗不会
    for i in range(5):
        rows.append(_row(t0, f"2026-07-10T00:01:{i:02d}+00:00", source_video="v0.mp4"))
    _write_rows(ledger, rows)  # 8 行 / 3 视频
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 3}
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    # 视频折叠 → 近 3 视频 v0/v1/v2 的曲 t0/t1/t2 全排除 → 只剩 t3；
    # 若按行窗则只排 t0（末 3 行都是 t0），会误选 t1/t2
    assert ch.path.name == "t3.mp3"
    assert ch.dedup_mode == "normal" and ch.excluded_count == 3


# ---- window=0 关闭去重 ----

def test_window_zero_disables_dedup(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")     # 唯一曲子
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:00+00:00")])
    base = {"enabled": True, "library": str(lib), "select": "category", "category": "calm"}
    # window=0 → 不读台账、不去重
    ch = choose_music({**base, "no_repeat_window": 0}, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "a.mp3" and ch.dedup_mode == "disabled" and ch.excluded_count == 0
    # window=30 且唯一曲子已用过 → 池排空 → LRU 兜底仍选它（不卡死）
    ch = choose_music({**base, "no_repeat_window": 30}, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "a.mp3" and ch.dedup_mode == "lru_fallback"


def test_no_ledger_path_disables_dedup(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "a.mp3")
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), ledger_path=None)  # 台账关（off/推导不出）
    assert ch.path.name == "a.mp3" and ch.dedup_mode == "disabled"


# ---- 显式指定绕过去重 ----

def test_explicit_override_bypasses_dedup(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    piano = _track(lib, "calm", "piano.mp3")
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(piano, "2026-07-10T00:00:00+00:00")])  # 近期用过
    cfg = {"enabled": True, "library": str(lib), "select": "random", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), override_music="calm/piano.mp3", ledger_path=ledger)
    assert ch.path.name == "piano.mp3"   # 显式指定 → 放行不去重
    assert ch.explicit is True and ch.dedup_mode == "explicit"


def test_explicit_selectfile_bypasses_dedup(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    piano = _track(lib, "calm", "piano.mp3")
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(piano, "2026-07-10T00:00:00+00:00")])
    cfg = {"enabled": True, "library": str(lib), "select": "file",
           "file": "calm/piano.mp3", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "piano.mp3" and ch.explicit is True and ch.dedup_mode == "explicit"


# ---- 显式指定库外曲：放行 + 警告（不硬拦） ----

def test_explicit_out_of_library_warns(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "inlib.mp3")
    outside = (tmp_path / "outside.mp3").resolve()   # 库外文件（绝对路径，不被拼进 library）
    outside.write_bytes(b"x")
    cfg = {"enabled": True, "library": str(lib), "select": "random", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), override_music=str(outside), ledger_path=None)
    assert ch.path == outside and ch.explicit is True and ch.dedup_mode == "explicit"
    assert ch.warning is not None and "不在配置曲库" in ch.warning  # 放行但警告


def test_explicit_in_library_no_warning(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "inlib.mp3")
    cfg = {"enabled": True, "library": str(lib), "select": "file",
           "file": "calm/inlib.mp3", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0))
    assert ch.explicit is True and ch.warning is None  # 库内 → 无警告


# ---- 池排空 → LRU 兜底选最久未用 ----

def test_lru_fallback_picks_oldest(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    b = _track(lib, "calm", "b.mp3")
    c = _track(lib, "calm", "c.mp3")
    ledger = tmp_path / "ledger.jsonl"
    # 3 个不同视频各用一首 → 全池被排除（视频折叠后仍 3 个视频，非同一视频刷行）
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:01+00:00", source_video="v1.mp4"),  # a 最久未用
                         _row(b, "2026-07-10T00:00:02+00:00", source_video="v2.mp4"),
                         _row(c, "2026-07-10T00:00:03+00:00", source_video="v3.mp4")])  # c 最近用
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    for seed in range(4):  # 全池被排空 → LRU 确定性选最旧 a（不受种子影响）
        ch = choose_music(cfg, random.Random(seed), ledger_path=ledger)
        assert ch.path.name == "a.mp3", (seed, ch)
        assert ch.dedup_mode == "lru_fallback" and ch.excluded_count == 3


# ---- 台账缺失 / 坏行 ----

def test_ledger_missing_treated_empty(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "a.mp3")
    ledger = tmp_path / "does_not_exist.jsonl"   # 首跑：文件还没建
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "a.mp3" and ch.dedup_mode == "normal"
    assert ch.excluded_count == 0 and ch.warning is None


def test_ledger_bad_lines_skipped(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "ledger.jsonl"
    good = json.dumps(_row(a, "2026-07-10T00:00:00+00:00"), ensure_ascii=False)
    ledger.write_text(good + "\nnot json\n{broken\n\n42\n", encoding="utf-8")  # 坏行 + 空行 + 非 dict
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.path.name == "b.mp3"        # 好行仍生效 → a 被排除
    assert ch.dedup_mode == "normal" and ch.warning is not None and "损坏" in ch.warning


# ---- auto 路径推导 ----

def test_resolve_ledger_auto_standard_layout(tmp_path: Path) -> None:
    lib = tmp_path / "estate-music-library" / "music" / "01_approved" / "commercial"
    lib.mkdir(parents=True)
    path, warn = resolve_usage_ledger({"library": str(lib), "usage_ledger": "auto"}, tmp_path)
    assert warn is None
    assert path == (tmp_path / "estate-music-library" / "exports"
                    / "project_usage_logs" / "usage_ledger.jsonl")


def test_resolve_ledger_auto_nonstandard_off(tmp_path: Path) -> None:
    lib = tmp_path / "mylib"
    lib.mkdir()
    path, warn = resolve_usage_ledger({"library": str(lib), "usage_ledger": "auto"}, tmp_path)
    assert path is None
    assert warn is not None and "非标准布局" in warn


def test_resolve_ledger_off(tmp_path: Path) -> None:
    assert resolve_usage_ledger({"library": str(tmp_path), "usage_ledger": "off"}, tmp_path) == (None, None)


def test_resolve_ledger_explicit_relative(tmp_path: Path) -> None:
    path, warn = resolve_usage_ledger({"library": str(tmp_path), "usage_ledger": "sub/led.jsonl"}, tmp_path)
    assert warn is None and path == tmp_path / "sub" / "led.jsonl"


# ---- 台账 append 行 schema ----

_SCHEMA_KEYS = {"at", "pipeline", "video", "source_video", "track_path",
                "track_name", "category", "explicit", "config"}


def test_append_ledger_schema(tmp_path: Path) -> None:
    ledger = tmp_path / "sub" / "led.jsonl"   # 父目录不存在 → append 时自动建
    row = {"at": "2026-07-10T12:00:00+00:00", "pipeline": "propcut", "video": "客厅_cut.mp4",
           "source_video": "客厅.mp4", "track_path": "D:/m/calm/x.mp3", "track_name": "x.mp3",
           "category": "calm", "explicit": False, "config": "propcut.yaml"}
    assert append_usage_ledger(ledger, row) is None
    raw = ledger.read_text(encoding="utf-8")
    assert "客厅" in raw            # ensure_ascii=False：中文不转义
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert set(parsed) == _SCHEMA_KEYS
    assert parsed["video"] == "客厅_cut.mp4" and parsed["explicit"] is False

    # append-only：再写一行不覆盖
    append_usage_ledger(ledger, {**row, "video": "b_cut.mp4"})
    assert len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]) == 2


def test_track_category(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    nested = _track(lib, "calm", "x.mp3")
    root = lib / "root.mp3"
    root.write_bytes(b"x")
    assert track_category(lib, nested) == "calm"
    assert track_category(lib, root) == ""            # 库根直属 → 无分类
    assert track_category(lib, tmp_path / "outside.mp3") == ""  # 库外 → ""


# ---- 选曲决策记录（P0-4）----

def test_selection_log_append_and_path(tmp_path: Path) -> None:
    from propcut.music import append_selection_log, selection_log_path

    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    assert selection_log_path(ledger) == ledger.parent / "selection_log.jsonl"
    row = {"at": "2026-07-16T12:00:00+00:00", "pipeline": "propcut", "video": "客厅_cut.mp4",
           "source_video": "客厅.mp4", "config": "propcut.yaml", "profile": "modern_apartment",
           "categories": ["luxury_elegant", "cozy_warm"], "dedup_mode": "normal",
           "explicit": False, "pool_size": 12, "excluded_count": 3,
           "selected_track": "D:/m/calm/x.mp3", "selected_name": "x.mp3",
           "selected_category": "calm"}
    assert append_selection_log(ledger, row) is None
    raw = selection_log_path(ledger).read_text(encoding="utf-8")
    assert "客厅" in raw  # ensure_ascii=False
    parsed = json.loads(raw.strip())
    assert parsed["profile"] == "modern_apartment" and parsed["pool_size"] == 12

    # append-only：再写一行不覆盖，且不碰使用台账文件
    append_selection_log(ledger, {**row, "video": "b_cut.mp4"})
    lines = [l for l in selection_log_path(ledger).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert not ledger.exists()


def test_selection_log_write_failure_returns_warning(tmp_path: Path) -> None:
    from propcut.music import append_selection_log

    blocker = tmp_path / "exports"
    blocker.write_bytes(b"x")  # 父目录位置被文件占 → mkdir 必炸 → warning 不异常
    warn = append_selection_log(blocker / "usage_ledger.jsonl", {"at": "t"})
    assert warn is not None and "选曲决策记录" in warn


def test_choose_music_exposes_pool_size(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    _track(lib, "calm", "b.mp3")
    cfg = {"enabled": True, "library": str(lib), "select": "category",
           "category": "calm", "no_repeat_window": 30}
    # disabled（无台账）
    ch = choose_music(cfg, random.Random(0))
    assert ch.dedup_mode == "disabled" and ch.pool_size == 2
    # normal（有台账排除 1 首）
    ledger = tmp_path / "ledger.jsonl"
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:00+00:00")])
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.dedup_mode == "normal" and ch.pool_size == 2
    # lru_fallback（池被排空）
    _write_rows(ledger, [_row(a, "2026-07-10T00:00:00+00:00", source_video="v1.mp4"),
                         _row(lib / "calm" / "b.mp3", "2026-07-11T00:00:00+00:00",
                              source_video="v2.mp4")])
    ch = choose_music(cfg, random.Random(0), ledger_path=ledger)
    assert ch.dedup_mode == "lru_fallback" and ch.pool_size == 2
    # explicit → pool_size 0（不组池）
    ch = choose_music(cfg, random.Random(0), override_music=str(a.resolve()), ledger_path=ledger)
    assert ch.explicit and ch.pool_size == 0
