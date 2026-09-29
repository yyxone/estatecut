"""Project-specific exceptions."""


class EstatecutError(Exception):
    """Base error for expected estatecut failures."""


class FFmpegError(EstatecutError):
    """Raised when FFmpeg or FFprobe fails."""


class GeminiUnavailableError(EstatecutError):
    """Raised when Gemini mode is requested but the SDK or API is unavailable."""


class RenderBlockedError(EstatecutError):
    """Raised when rendering must stop for a hard safety or validity reason."""


class ShotDetectionUnavailableError(EstatecutError):
    """Raised when content-aware shot detection is requested but scenedetect is unavailable."""


class GradingError(EstatecutError):
    """Raised when a color-grading preset / LUT is invalid or missing.

    Lives in the shared layer so propcut.grading stays free of propcut-internal
    imports (future talkcut etc. import propcut.grading directly). pipeline wraps
    per-video work in `except Exception`, so this is caught like the old ConfigError.
    """
