from pathlib import Path
import sys
import subprocess
import pytest

def test_bundled_data_is_inside_package():
    import estatecut
    from propcut.config import _repo_profiles_file, _repo_pools_file
    from propcut.grading import PRESETS_FILE, load_presets
    from talkcut.qaexport import XHS_COMPLIANCE
    from talkcut.subtitle import CONFIG_CORRECTIONS
    package = Path(estatecut.__file__).parent.resolve()
    for path in [_repo_profiles_file(), _repo_pools_file(), PRESETS_FILE, XHS_COMPLIANCE, CONFIG_CORRECTIONS]:
        assert path.is_file()
        assert path.resolve().is_relative_to(package)
    assert "neutral" in load_presets()

def test_asr_default_has_no_external_process(monkeypatch):
    from talkcut import asr_adapter as adapter
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("mock spawned process"))
    assert adapter.transcribe_wav(Path("missing.wav"))["asr"]["mode"] == "mock"

def test_asr_interpreter_resolved_at_call_time(monkeypatch, tmp_path):
    from talkcut import asr_adapter as adapter
    captured = {}
    monkeypatch.delenv("ESTATECUT_ASR_TIMEOUT", raising=False)
    def runner(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"asr":{},"segments":[]}', '')
    monkeypatch.setenv("ESTATECUT_ASR_PYTHON", sys.executable)
    monkeypatch.setattr(subprocess, "run", runner)
    result = adapter.transcribe_wav(tmp_path/"dummy.wav", mode="real", device="cpu")
    assert captured["argv"][0] == sys.executable
    assert captured["shell"] is False
    assert captured["timeout"] is None
    assert result["asr"]["mode"] == "real"

def test_asr_bad_interpreter_fails_before_spawn(monkeypatch, tmp_path):
    from talkcut import asr_adapter as adapter
    monkeypatch.setenv("ESTATECUT_ASR_PYTHON", str(tmp_path/"missing-python"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(FileNotFoundError):
        adapter.transcribe_wav(tmp_path/"dummy.wav", mode="real")

def test_cpu_encoder_override_avoids_hardware_probe(monkeypatch):
    import estatecut.ffmpeg_tools as ff
    monkeypatch.setenv("ESTATECUT_ENCODER", "libx264")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("hardware probe"))
    assert ff.preferred_encoder() == "libx264"

def test_encoder_override_rejects_unknown(monkeypatch):
    import estatecut.ffmpeg_tools as ff
    monkeypatch.setenv("ESTATECUT_ENCODER", "unknown")
    with pytest.raises(ValueError):
        ff.preferred_encoder()

def test_asr_configured_timeout_is_reported(monkeypatch, tmp_path):
    from talkcut import asr_adapter as adapter
    monkeypatch.setenv("ESTATECUT_ASR_PYTHON", sys.executable)
    monkeypatch.setenv("ESTATECUT_ASR_TIMEOUT", "12.5")
    def timeout(argv, **kwargs):
        assert kwargs["timeout"] == 12.5
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="ESTATECUT_ASR_TIMEOUT"):
        adapter.transcribe_wav(tmp_path/"dummy.wav", mode="real", device="cpu")

@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_bad_asr_timeout_rejected_before_spawn(monkeypatch, tmp_path, value):
    from talkcut import asr_adapter as adapter
    monkeypatch.setenv("ESTATECUT_ASR_PYTHON", sys.executable)
    monkeypatch.setenv("ESTATECUT_ASR_TIMEOUT", value)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ValueError, match="ESTATECUT_ASR_TIMEOUT"):
        adapter.transcribe_wav(tmp_path/"dummy.wav", mode="real")

def test_custom_lut_resolves_relative_to_presets(monkeypatch, tmp_path):
    from propcut.grading import load_presets, build_style_chain
    folder = tmp_path/"presets"; folder.mkdir()
    lut = folder/"identity.cube"
    lut.write_text('LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n')
    config = folder/"custom.yaml"; config.write_text('custom:\n  lut: identity.cube\n')
    monkeypatch.chdir(tmp_path)
    presets = load_presets(config)
    assert Path(presets["custom"]["lut"]) == lut
    assert "lut3d=" in build_style_chain({"mode":"preset","preset":"custom"}, presets)

def test_explicit_timeline_font(monkeypatch, tmp_path):
    from talkcut import timeline_view
    font = tmp_path/"custom.ttf"; font.write_bytes(b"synthetic test font")
    monkeypatch.setenv("ESTATECUT_FONT", str(font))
    monkeypatch.setattr(timeline_view.ImageFont, "truetype", lambda path,size: (path,size))
    assert timeline_view._load_font(18) == (str(font),18)

def test_missing_explicit_font_reports_error(monkeypatch, tmp_path):
    from talkcut import timeline_view
    monkeypatch.setenv("ESTATECUT_FONT", str(tmp_path/"missing.ttf"))
    with pytest.raises(FileNotFoundError):
        timeline_view._load_font(18)
