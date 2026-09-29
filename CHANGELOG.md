# Changelog

## 0.1.0

First public export of Estatecut's locally maintained `talkcut` and `propcut` workflows.

- Transcript-driven editing with explicit transcript, cut, and subtitle review gates.
- Property-video start detection, grading, optional user-supplied music, and export checks.
- Installable Python package with portable bundled defaults and configurable ASR execution.
- 274 synthetic regression tests passing on Windows and Linux, including Python 3.11 and 3.13 in GitHub Actions.
- Portable looped-music overlap mixing avoids missing audio on FFmpeg 6.1.1 while retaining export validation.

Limitations: real ASR and macOS have not been validated; no models, music, fonts, FFmpeg binaries, or private operational assets are included. No external adoption is claimed.
