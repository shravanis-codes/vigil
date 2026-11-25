"""
All configuration comes from environment variables (or a .env file).
Nothing secret is ever hard-coded.
"""
import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        # --- DetectifAI ---
        self.detectif_api_key: Optional[str] = os.getenv("DETECTIF_API_KEY") or None
        self.detectif_base_url: str = os.getenv("DETECTIF_BASE_URL", "https://platform.detectif.ai")
        self.detectif_stream_url: str = os.getenv("DETECTIF_STREAM_URL", "wss://platform.detectif.ai/v1/stream")
        self.detectif_webhook_secret: Optional[str] = os.getenv("DETECTIF_WEBHOOK_SECRET") or None

        # "mock" or "detectif". Default: detectif if a key is present, else mock.
        default_provider = "detectif" if self.detectif_api_key else "mock"
        self.provider: str = os.getenv("VIGIL_PROVIDER", default_provider).lower()

        # --- Twilio (live mode) ---
        self.twilio_account_sid: Optional[str] = os.getenv("TWILIO_ACCOUNT_SID") or None
        self.twilio_auth_token: Optional[str] = os.getenv("TWILIO_AUTH_TOKEN") or None
        self.twilio_validate_signature: bool = _bool("TWILIO_VALIDATE_SIGNATURE", True)
        # Public https URL of THIS backend (e.g. your ngrok URL). Needed for live calls.
        self.public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        # "relay": Twilio -> VIGIL -> DetectifAI (results arrive live, no webhook setup)
        # "direct": Twilio -> DetectifAI, results via DetectifAI webhooks
        self.live_mode: str = os.getenv("VIGIL_LIVE_MODE", "relay").lower()
        self.human_agent_number: Optional[str] = os.getenv("HUMAN_AGENT_NUMBER") or None
        self.default_live_speaker: Optional[str] = os.getenv("DEFAULT_LIVE_SPEAKER_ID") or None
        # If the stream has not returned an identity result after this many seconds of
        # caller audio, VIGIL verifies the buffered audio through the REST API.
        self.midcall_verify_seconds: float = float(os.getenv("MIDCALL_VERIFY_SECONDS", "10"))
        self.midcall_verify: bool = _bool("MIDCALL_VERIFY", True)
        self.hold_max_loops: int = int(os.getenv("HOLD_MAX_LOOPS", "8"))

        # --- Storage / files ---
        self.data_dir: Path = Path(os.getenv("VIGIL_DATA_DIR", str(ROOT / "data")))
        self.db_path: Path = self.data_dir / "vigil.db"
        self.upload_dir: Path = self.data_dir / "uploads"
        self.policy_file: Path = Path(os.getenv("VIGIL_POLICY_FILE", str(ROOT / "config" / "policy.yaml")))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def public_summary(self) -> dict:
        """Safe-to-display config (no secrets)."""
        return {
            "provider": self.provider,
            "detectif_key_set": bool(self.detectif_api_key),
            "detectif_base_url": self.detectif_base_url,
            "webhook_secret_set": bool(self.detectif_webhook_secret),
            "twilio_enabled": self.twilio_enabled,
            "live_mode": self.live_mode,
            "public_base_url": self.public_base_url,
            "human_agent_number_set": bool(self.human_agent_number),
            "midcall_verify": self.midcall_verify,
            "policy_file": str(self.policy_file),
        }


settings = Settings()
