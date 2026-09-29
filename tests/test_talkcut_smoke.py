"""talkcut 7 阶段 smoke 集成测试。合成片 + mock ASR，永不真调模型/Gemini。"""
import pytest

from estatecut.ffmpeg_tools import ffmpeg_available


@pytest.mark.skipif(not ffmpeg_available(), reason="needs ffmpeg/ffprobe")
def test_talkcut_smoke_7_stages(tmp_path):
    from talkcut.cli import _smoke
    assert _smoke(tmp_path / "smoke") == 0


def test_lock_gates_block(tmp_path):
    """门控回归：transcript_lock 未上 → paper-edit 拒绝；human_locked 未置 → rough-cut 拒绝。"""
    import json
    from talkcut import STAGES, paperedit, cut
    out = tmp_path / "o"; out.mkdir()
    (out / "transcript.json").write_text(json.dumps({
        "project": "t", "source": {"path": "s.mp4", "duration_sec": 5, "clips": []},
        "asr": {"engine": "mock", "model": "m", "device": "cpu"}, "segments": [], "gap_audit": [],
        "corrections_version": "x"}), encoding="utf-8")
    # R3 起 load_state 结构显式校验——stages 必须 7 阶段齐全，不能再写 {}
    (out / "state.json").write_text(json.dumps({
        "project": "t",
        "stages": {s: {"status": "pending", "outputs": [], "verified": False} for s in STAGES},
        "locks": {"transcript_lock": False, "cut_lock": False, "subtitle_lock": False}}),
        encoding="utf-8")
    with pytest.raises(RuntimeError):  # transcript_lock 未上
        paperedit.propose_edl(out, "t")
    # cut_lock 未上 → rough-cut 拒绝
    (out / "edl.json").write_text(json.dumps({
        "project": "t", "source_timeline": "s.mp4", "human_locked": True,
        "keep_ranges": [{"src_start": 0, "src_end": 1, "label": "a"}], "cuts": [], "splices": [], "broll_cover": []}),
        encoding="utf-8")
    with pytest.raises(RuntimeError):
        cut.rough_cut(out, "t")


def test_asr_default_is_mock():
    """硬约束：asr_adapter 默认 mock，绝不默认真跑模型。"""
    from talkcut import asr_adapter
    from pathlib import Path as P
    assert asr_adapter.transcribe_wav(P("nope.wav"))["asr"]["engine"] == "mock"


@pytest.mark.skipif(not ffmpeg_available(), reason="needs ffmpeg/ffprobe")
def test_artifacts_match_schemas(tmp_path):
    import json
    from pathlib import Path
    jsonschema = pytest.importorskip("jsonschema")
    from talkcut.cli import _smoke
    out = tmp_path / "smoke"
    assert _smoke(out) == 0
    root = Path(__file__).resolve().parents[1]
    for art, sch in [("transcript.json", "transcript"), ("edl.json", "edl"), ("state.json", "state")]:
        data = json.loads((out / art).read_text(encoding="utf-8"))
        schema = json.loads((root / "schemas" / f"{sch}.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(data, schema)
