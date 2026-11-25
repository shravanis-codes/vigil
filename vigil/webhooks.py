"""
DetectifAI webhook verification (Standard Webhooks scheme, per the DetectifAI docs):
  signed content = "{webhook-id}.{webhook-timestamp}.{raw body}"
  key            = base64-decode(secret without the "whsec_" prefix)
  signature      = base64(HMAC-SHA256(key, signed content)), header holds "v1,<sig>" values
Reject timestamps older than 5 minutes. Verify the RAW body before parsing.
"""
import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional

TOLERANCE_SECONDS = 300


class WebhookVerificationError(Exception):
    pass


def _key(secret: str) -> bytes:
    s = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(s)
    except Exception as e:  # noqa: BLE001
        raise WebhookVerificationError("webhook secret is not valid base64") from e


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    content = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_key(secret), content, hashlib.sha256).digest()).decode()


def verify(secret: str, headers: Mapping[str, str], body: bytes, now: Optional[float] = None) -> None:
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not (msg_id and ts and sigs):
        raise WebhookVerificationError("missing webhook-id / webhook-timestamp / webhook-signature header")
    try:
        ts_int = int(ts)
    except ValueError:
        raise WebhookVerificationError("bad timestamp")
    if abs((now or time.time()) - ts_int) > TOLERANCE_SECONDS:
        raise WebhookVerificationError("timestamp outside the 5 minute tolerance")
    expected = sign(secret, msg_id, ts, body).split(",", 1)[1]
    for part in sigs.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return
    raise WebhookVerificationError("no matching signature")
