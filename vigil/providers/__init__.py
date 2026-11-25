from .base import VoiceProvider
from .detectif import DetectifProvider
from .mock import MockProvider


def make_provider(settings, store) -> VoiceProvider:
    if settings.provider == "detectif":
        return DetectifProvider(settings.detectif_api_key, settings.detectif_base_url)
    return MockProvider(store)
