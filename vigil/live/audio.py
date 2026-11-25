"""Twilio sends 8 kHz mu-law audio. These helpers turn buffered call audio into a WAV
file for the mid-call speaker verification fallback. (audioop was removed in Python 3.13,
so mu-law decoding is implemented here.)"""
import io
import struct
import wave


def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


_ULAW_TABLE = [_ulaw_to_linear(i) for i in range(256)]


def mulaw_to_pcm16(data: bytes) -> bytes:
    return struct.pack("<%dh" % len(data), *(_ULAW_TABLE[b] for b in data))


def pcm16_to_wav(pcm: bytes, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def mulaw_to_wav(data: bytes, sample_rate: int = 8000) -> bytes:
    return pcm16_to_wav(mulaw_to_pcm16(data), sample_rate)


def wav_duration(path: str) -> float:
    """Duration of a WAV file in seconds (raises for non-WAV files)."""
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())
