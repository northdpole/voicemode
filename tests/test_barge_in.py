"""Voice barge-in: VAD run-length trigger, off-by-default, safe on failure."""
from unittest.mock import MagicMock, patch

from voice_mode import barge_in as B

VOICED, QUIET = b"v", b"q"


class FakeVad:
    def is_speech(self, frame, rate):
        return frame == VOICED


def monitor(trigger_ms=90):
    hits = []
    return B.BargeInMonitor(FakeVad(), lambda: hits.append(1), trigger_ms), hits


def test_fires_after_sustained_speech_once():
    m, hits = monitor(90)  # 3 frames
    for f in [VOICED, VOICED, VOICED, VOICED, VOICED]:
        m.feed(f)
    assert hits == [1]


def test_short_blip_does_not_fire():
    m, hits = monitor(90)
    for f in [VOICED, VOICED, QUIET, VOICED, VOICED, QUIET]:
        m.feed(f)
    assert hits == []


def test_vad_error_is_silence():
    m, hits = monitor(30)
    m._vad = MagicMock(is_speech=MagicMock(side_effect=ValueError))
    m.feed(VOICED)
    assert hits == []


def test_disabled_by_default_opens_no_stream():
    with patch.object(B.config, "VOICE_BARGE_IN", False), patch.object(B.sd, "InputStream") as s:
        with B.voice_barge_in():
            pass
    s.assert_not_called()


def test_enabled_opens_and_closes_stream():
    with patch.object(B.config, "VOICE_BARGE_IN", True), \
         patch.object(B.sd, "InputStream") as s:
        with B.voice_barge_in():
            s.return_value.start.assert_called_once()
        s.return_value.stop.assert_called_once()
        s.return_value.close.assert_called_once()


def test_mic_failure_does_not_break_playback():
    with patch.object(B.config, "VOICE_BARGE_IN", True), \
         patch.object(B.sd, "InputStream", side_effect=RuntimeError("no mic")):
        with B.voice_barge_in():
            ran = True
    assert ran


def test_speech_requests_skip_forward():
    state = B.get_control_state()
    state.reset()
    with patch.object(B.config, "VOICE_BARGE_IN", True), \
         patch.object(B.config, "VOICE_BARGE_IN_MS", 30), \
         patch.object(B.sd, "InputStream") as s:
        with B.voice_barge_in():
            cb = s.call_args.kwargs["callback"]
            with patch("webrtcvad.Vad.is_speech", return_value=True):
                cb(b"\x00" * 960, 480, None, None)
    assert state.snapshot().is_skip_forward
    state.reset()
