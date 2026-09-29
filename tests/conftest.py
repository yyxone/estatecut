from pathlib import Path
import os
import socket
import pytest
from estatecut.ffmpeg_tools import create_synthetic_clip, ffmpeg_available

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("ESTATECUT_ENCODER", os.environ.get("ESTATECUT_ENCODER") or "libx264")
    def reject(*args, **kwargs):
        raise AssertionError("Tests must not contact external services")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)

@pytest.fixture
def synthetic_clip(tmp_path):
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    path = tmp_path / "001_living_room.mp4"
    create_synthetic_clip(path, duration=6.0, label="living")
    return path
