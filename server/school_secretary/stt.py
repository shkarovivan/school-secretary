"""Local speech-to-text via faster-whisper (no external services).

The model downloads once to the local HF cache and then runs offline.
Default model "small" is a good Russian quality/size trade-off; on a
Raspberry Pi use "base".
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("stt")

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        name = os.getenv("WHISPER_MODEL", "small")
        log.info("загружаю whisper-модель %s (первый раз скачивается)", name)
        _model = WhisperModel(name, device="cpu", compute_type="int8")
    return _model


def transcribe_bytes(audio: bytes, filename: str = "voice.ogg") -> Optional[str]:
    """Transcribe in-memory audio (ogg/opus, m4a, wav, ...) to text."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix="_" + filename, delete=True) as tmp:
        tmp.write(audio)
        tmp.flush()
        segments, info = _get_model().transcribe(tmp.name, language="ru", vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("распознано (%.1fs аудио): %s", info.duration, text[:80])
        return text or None
