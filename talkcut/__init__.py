"""talkcut — 中文口播/讲解视频剪辑助手（7 阶段）。

ingest → transcribe → paper-edit → rough-cut → fine-cut → subtitle → qa-export

复用 estatecut 包的 FFmpeg 基础设施（estatecut.ffmpeg_tools）。
ASR 走外部 adapter 调 transcription conda env，不在本环境装 torch/faster-whisper。
"""

__version__ = "0.1.0"
STAGES = ["ingest", "transcribe", "paper_edit", "rough_cut", "fine_cut", "subtitle", "qa_export"]
