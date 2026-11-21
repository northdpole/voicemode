"""Voice barge-in: speak over Claude to cut its reply and take the turn.

While a TTS utterance plays, a monitor keeps the microphone open and runs
voice-activity detection. Sustained speech calls
``ControlState.request_skip_forward()``, the same latched barge-in the media
keys use, so playback aborts and ``converse`` advances straight to listening.

Off by default (``VOICEMODE_VOICE_BARGE_IN``). Without echo cancellation, a
speaker's own output triggers it, so use headphones.

Known limit: the first ~``VOICE_BARGE_IN_MS`` of your interruption is spent
detecting it and is not in the recording that follows.
"""

import logging
from contextlib import contextmanager

from . import config
from ._sounddevice import load as _load_sounddevice
from .control_channel import get_control_state

sd = _load_sounddevice()
logger = logging.getLogger("voicemode.barge_in")

RATE = 16000        # webrtcvad supports 8/16/32/48 kHz
FRAME_MS = 30       # webrtcvad frames: 10, 20 or 30 ms


class BargeInMonitor:
    """Counts consecutive voiced frames; fires once when the run is long enough."""

    def __init__(self, vad, on_speech, trigger_ms: int):
        self._vad = vad
        self._on_speech = on_speech
        self._need = max(1, trigger_ms // FRAME_MS)
        self._run = 0
        self.fired = False

    def feed(self, frame: bytes) -> None:
        if self.fired:
            return
        try:
            voiced = self._vad.is_speech(frame, RATE)
        except Exception:  # malformed frame: treat as silence, never raise into PortAudio
            voiced = False
        self._run = self._run + 1 if voiced else 0
        if self._run >= self._need:
            self.fired = True
            self._on_speech()


@contextmanager
def voice_barge_in():
    """Listen for speech for the duration of the block. No-op unless enabled."""
    if not config.VOICE_BARGE_IN:
        yield
        return
    stream = None
    try:
        import webrtcvad
        monitor = BargeInMonitor(
            webrtcvad.Vad(config.VAD_AGGRESSIVENESS),
            lambda: get_control_state().request_skip_forward(),
            config.VOICE_BARGE_IN_MS,
        )
        frame_samples = RATE * FRAME_MS // 1000

        def callback(indata, frames, time_info, status):
            monitor.feed(bytes(indata))

        stream = sd.InputStream(
            samplerate=RATE, channels=1, dtype="int16",
            blocksize=frame_samples, callback=callback,
        )
        stream.start()
    except Exception as e:  # no mic / no VAD: playback must still work
        logger.warning("voice barge-in unavailable: %s", e)
        stream = None
    try:
        yield
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.debug("barge-in stream close failed", exc_info=True)
