"""Provider adapter interface. The rest of VIGIL never talks to DetectifAI directly."""
from typing import Any, Dict, List, Optional

from ..models import Evidence


class VoiceProvider:
    name = "base"

    async def analyze_recording(self, session_id: str, audio_path: str, speaker_id: Optional[str],
                                idempotency_key: str) -> List[Evidence]:
        """Run Aletheia (+ Eidos when speaker_id is given) on an uploaded file."""
        raise NotImplementedError

    async def verify_audio(self, session_id: str, wav_bytes: bytes, speaker_id: str,
                           idempotency_key: str) -> List[Evidence]:
        """Speaker verification on in-memory audio (used mid-call)."""
        raise NotImplementedError

    async def enroll(self, speaker_id: str, audio_path: str, display_name: Optional[str],
                     idempotency_key: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def list_speakers(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def create_stream_token(self) -> str:
        raise NotImplementedError

    async def health(self) -> Dict[str, Any]:
        raise NotImplementedError
