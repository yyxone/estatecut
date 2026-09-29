"""R1 E1/E2：export_video 替换前校验 + overwrite=false 既有输出体检。

E1：编码产物 .part 必须先通过校验（非空 / 有效视频流 / 时长 / 音轨模式）才 replace
正式输出；校验失败抛 FFmpegError（pipeline 逐视频 catch → entry error），旧正式
输出 byte-for-byte 不变。
E2：output.overwrite=false 的跳过路径必须先探测既有输出可用性，0 字节/不可解码的
坏文件不得记 success/skipped。

encode 与 probe 全 mock，不依赖 ffmpeg（个别 garbage-probe 测试除外，自动 skip）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import propcut.export as export_mod
import propcut.pipeline as pipeline_mod
from estatecut.exceptions import FFmpegError
from estatecut.ffmpeg_tools import ffmpeg_available, probe_media, run_command

EXPORT_CFG = {"resolution": "original", "fit": "pad", "quality": "low"}
MUSIC_REMOVE = {"original_audio": "remove", "volume": 0.8, "fade_in": 0.0,
                "fade_out": 0.0, "music_start": 0.0}


def _fake_encode_writing(payload: bytes):
    """替身编码器：把 payload 写到 build_argv 的输出路径（argv 末位）。"""
    def _fake(build_argv, quality, log=None):
        argv = build_argv(["-c:v", "libx264"])
        Path(argv[-1]).write_bytes(payload)
    return _fake


def test_bad_part_never_replaces_existing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 坏 .part（probe 全 0）→ 必须抛错且旧正式输出保持原字节，.part 清理干净。
    src = tmp_path / "src.mp4"
    src.write_bytes(b"S" * 64)
    out = tmp_path / "cut.mp4"
    out.write_bytes(b"OLD-GOOD-OUTPUT")

    monkeypatch.setattr(export_mod, "_encode_with_fallback", _fake_encode_writing(b"BROKEN"))
    # raising=False：TEST_RED 阶段 export 模块还没有 probe_media（未实现校验）
    monkeypatch.setattr(export_mod, "probe_media", lambda p: {
        "duration_sec": 0.0, "width": 0, "height": 0, "fps": 0.0,
        "audio_present": False}, raising=False)

    with pytest.raises(FFmpegError):
        export_mod.export_video(src, out, 0.0, 10.0, "", EXPORT_CFG, None,
                                MUSIC_REMOVE, audio_present=True)
    assert out.read_bytes() == b"OLD-GOOD-OUTPUT"
    assert not out.with_name("cut.part.mp4").exists()


def test_audio_mode_mismatch_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # remove_silent 预期无音轨：probe 报有音轨 = 编出来的东西不对，拒绝替换。
    src = tmp_path / "src.mp4"
    src.write_bytes(b"S" * 64)
    out = tmp_path / "cut.mp4"
    out.write_bytes(b"OLD")

    monkeypatch.setattr(export_mod, "_encode_with_fallback", _fake_encode_writing(b"HAS-AUDIO"))
    monkeypatch.setattr(export_mod, "probe_media", lambda p: {
        "duration_sec": 10.0, "width": 1280, "height": 720, "fps": 30.0,
        "audio_present": True}, raising=False)

    with pytest.raises(FFmpegError):
        export_mod.export_video(src, out, 0.0, 10.0, "", EXPORT_CFG, None,
                                MUSIC_REMOVE, audio_present=True)
    assert out.read_bytes() == b"OLD"


def test_verified_part_replaces_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 校验通过 → 正常 replace（回归保护：校验不能把好产物也拦下来）。
    src = tmp_path / "src.mp4"
    src.write_bytes(b"S" * 64)
    out = tmp_path / "cut.mp4"
    out.write_bytes(b"OLD")

    monkeypatch.setattr(export_mod, "_encode_with_fallback", _fake_encode_writing(b"NEW-VERIFIED"))
    monkeypatch.setattr(export_mod, "probe_media", lambda p: {
        "duration_sec": 10.0, "width": 1280, "height": 720, "fps": 30.0,
        "audio_present": False}, raising=False)

    result = export_mod.export_video(src, out, 0.0, 10.0, "", EXPORT_CFG, None,
                                     MUSIC_REMOVE, audio_present=True)
    assert out.read_bytes() == b"NEW-VERIFIED"
    assert result["audio_mode"] == "remove_silent"
    assert not out.with_name("cut.part.mp4").exists()


# --- E2：overwrite=false 跳过路径的既有输出体检 ---

def test_existing_output_zero_byte_not_ok(tmp_path: Path) -> None:
    bad = tmp_path / "old_cut.mp4"
    bad.write_bytes(b"")
    assert pipeline_mod._existing_output_ok(bad) is False


def test_existing_output_garbage_not_ok(tmp_path: Path) -> None:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    bad = tmp_path / "old_cut.mp4"
    bad.write_bytes(b"not a video at all")
    assert pipeline_mod._existing_output_ok(bad) is False


def test_existing_output_truncated_tail_not_ok(tmp_path: Path) -> None:
    # +faststart 的 mp4 截断一半后 moov 仍在头部、metadata probe 照常通过——
    # 必须靠尾部真解码拦下，不得记 success（Codex F7）。
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    good = tmp_path / "good_cut.mp4"
    run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(good)])
    assert pipeline_mod._existing_output_ok(good) is True  # 完整文件不得误拦
    data = good.read_bytes()
    bad = tmp_path / "trunc_cut.mp4"
    bad.write_bytes(data[: len(data) // 2])
    meta = probe_media(bad)  # 截断件 metadata probe 照常通过（moov 在头部）——
    assert meta["duration_sec"] > 0  # 证明下行拦截来自尾部真解码，非前置 probe（Codex 第 2 轮）
    assert pipeline_mod._existing_output_ok(bad) is False
