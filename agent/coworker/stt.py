"""Offline speech-to-text for Telegram voice notes.

Two free, open-source engines, both fully local - nothing is uploaded and
there is no API bill:

  faster-whisper (MIT, SYSTRAN/faster-whisper)
      CTranslate2 build of Whisper. ``base`` is ~145 MB and auto-detects the
      language, which matters here because the speaker mixes Uzbek and
      Russian mid-sentence. This is the default.

  vosk (Apache-2.0, alphacephei/vosk-api)
      ~45 MB per language and runs comfortably on an old laptop, but each
      model is single-language. There is a purpose-built Uzbek model, which
      beats Whisper on pure Uzbek.

Neither is a hard dependency: if both are missing the agent degrades to
"please type it instead" rather than crashing.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .config import config_dir

log = logging.getLogger("stt")

SAMPLE_RATE = 16000

VOSK_MODELS = {
    "uz": "vosk-model-small-uz-0.22",
    "ru": "vosk-model-small-ru-0.22",
    "en": "vosk-model-small-en-us-0.15",
}
VOSK_BASE_URL = "https://alphacephei.com/vosk/models"


class Transcriber:
    """Lazily loads whichever engine is available and keeps it warm."""

    def __init__(self, engine: str = "auto", model: str = "base", language: str = "uz") -> None:
        self.engine = engine
        self.model_name = model
        self.language = language
        self._impl = None
        self._resolved = ""
        self._error = ""

    # ------------------------------------------------------------- lifecycle

    @property
    def available(self) -> bool:
        return self.engine != "off" and (self._impl is not None or self._probe() != "")

    @property
    def status(self) -> str:
        if self.engine == "off":
            return "o'chirilgan"
        if self._impl is not None:
            return f"{self._resolved} ({self.model_name}) tayyor"
        found = self._probe()
        if not found:
            return "o'rnatilmagan - pip install faster-whisper"
        return f"{found} o'rnatilgan, model birinchi ovozda yuklanadi"

    def _probe(self) -> str:
        """Which engine could we use, without loading anything heavy?"""
        import importlib.util

        wanted = self.engine
        if wanted in ("auto", "faster-whisper") and importlib.util.find_spec("faster_whisper"):
            return "faster-whisper"
        if wanted in ("auto", "vosk") and importlib.util.find_spec("vosk"):
            return "vosk"
        return ""

    def load(self, progress=None) -> bool:
        """Load the model. Safe to call repeatedly; blocking, so call off-thread."""
        if self._impl is not None:
            return True
        if self.engine == "off":
            return False

        choice = self._probe()
        try:
            if choice == "faster-whisper":
                self._impl = _Whisper(self.model_name, progress)
            elif choice == "vosk":
                self._impl = _Vosk(self.language, progress)
            else:
                self._error = "STT kutubxonasi o'rnatilmagan"
                return False
        except Exception as exc:
            self._error = str(exc)[:200]
            log.warning("STT load failed: %s", exc)
            return False

        self._resolved = choice
        return True

    # ------------------------------------------------------------- transcribe

    def transcribe(self, audio: bytes, mime: str = "audio/ogg") -> tuple[str, str]:
        """Returns (text, error). Blocking - run it in a worker thread."""
        if self.engine == "off":
            return "", "ovozli xabar o'chirilgan"
        if not self.load():
            return "", self._error or "STT mavjud emas"

        try:
            pcm, samples = decode_audio(audio)
        except Exception as exc:
            return "", f"audio o'qilmadi: {exc}"
        if not samples:
            return "", "audio bo'sh"

        try:
            return self._impl.run(pcm, samples), ""
        except Exception as exc:
            log.warning("transcribe failed: %s", exc)
            return "", str(exc)[:200]


# ------------------------------------------------------------------- engines

class _Whisper:
    def __init__(self, size: str, progress=None) -> None:
        from faster_whisper import WhisperModel

        if progress:
            progress(f"Whisper «{size}» modeli yuklanmoqda (birinchi marta ~1-2 daqiqa)...")
        # int8 on CPU is the right trade-off for an office laptop.
        self.model = WhisperModel(
            size, device="cpu", compute_type="int8",
            download_root=str(_models_dir() / "whisper"),
        )
        if progress:
            progress("Whisper tayyor.")

    def run(self, pcm: bytes, samples: list[float]) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(
            audio,
            beam_size=1,               # greedy is plenty for short commands
            vad_filter=True,           # trims the silence Telegram pads in
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class _Vosk:
    def __init__(self, language: str, progress=None) -> None:
        import json as _json

        import vosk

        vosk.SetLogLevel(-1)
        path = _ensure_vosk_model(language, progress)
        self.model = vosk.Model(str(path))
        self.vosk = vosk
        self.json = _json

    def run(self, pcm: bytes, samples: list[float]) -> str:
        rec = self.vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.SetWords(False)
        chunk = 4000
        for i in range(0, len(pcm), chunk):
            rec.AcceptWaveform(pcm[i : i + chunk])
        final = self.json.loads(rec.FinalResult())
        return (final.get("text") or "").strip()


def _models_dir() -> Path:
    d = config_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_vosk_model(language: str, progress=None) -> Path:
    name = VOSK_MODELS.get(language, VOSK_MODELS["ru"])
    target = _models_dir() / name
    if target.is_dir():
        return target

    import urllib.request

    url = f"{VOSK_BASE_URL}/{name}.zip"
    if progress:
        progress(f"Vosk «{language}» modeli yuklanmoqda (~50 MB)...")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        urllib.request.urlretrieve(url, tmp.name)
        zip_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(_models_dir())
    finally:
        zip_path.unlink(missing_ok=True)
    if progress:
        progress("Vosk modeli tayyor.")
    return target


# ------------------------------------------------------------ audio decoding

def decode_audio(raw: bytes) -> tuple[bytes, list[float]]:
    """OGG/Opus (what Telegram sends) -> 16 kHz mono signed 16-bit PCM.

    Tries three decoders in order of how likely they are to already be
    present, so the user never has to install ffmpeg by hand if PyAV or
    libsndfile came along with something else.
    """
    for decoder in (_decode_av, _decode_soundfile, _decode_ffmpeg):
        try:
            pcm = decoder(raw)
            if pcm:
                return pcm, [1.0] * (len(pcm) // 2)
        except Exception:
            continue
    raise RuntimeError("audio dekoder topilmadi (pip install av)")


def _decode_av(raw: bytes) -> bytes:
    import av

    with av.open(io.BytesIO(raw)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        chunks: list[bytes] = []
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(bytes(out.planes[0]))
        # Flush whatever the resampler is still holding.
        for out in resampler.resample(None):
            chunks.append(bytes(out.planes[0]))
    return b"".join(chunks)


def _decode_soundfile(raw: bytes) -> bytes:
    import numpy as np
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate != SAMPLE_RATE:
        # Linear resample: fine for speech at these rates.
        n = int(len(mono) * SAMPLE_RATE / rate)
        mono = np.interp(
            np.linspace(0, len(mono), n, endpoint=False),
            np.arange(len(mono)),
            mono,
        )
    return (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _decode_ffmpeg(raw: bytes) -> bytes:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg yo'q")
    proc = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=raw, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:200])
    return proc.stdout
