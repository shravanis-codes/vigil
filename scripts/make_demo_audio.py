"""
Create placeholder WAV files for MOCK-mode demos.

These are tones, not voices. In mock mode the outcome comes from the file NAME
(e.g. "synthetic" -> fake). With a real DetectifAI key, use real recordings:
your own voice, and a clone of your own voice made with your consent.

Run: python scripts/make_demo_audio.py
"""
import math
import struct
import sys
import wave
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "demo_audio"


def tone(path: Path, seconds: float, freq: float = 220.0, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * (i / rate))
                                                     * (0.6 + 0.4 * math.sin(i / rate * 3))))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)


def main(out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "genuine_customer.wav": 12,
        "synthetic_clone_of_customer.wav": 12,
        "impostor_other_person.wav": 12,
        "short_clip.wav": 2,
        "enroll_sample_20s.wav": 20,
    }
    for name, secs in files.items():
        tone(out / name, secs)
        print("wrote", out / name)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT)
