"""Bearer-token check and extension-header echo for the WebSocket upgrade."""

import hmac
from collections.abc import Mapping

from connect_a2a.constants import EXTENSION_URI, EXTENSIONS_HEADER


def _get(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def check_bearer(headers: Mapping[str, str], expected_key: str) -> bool:
    """True if ``Authorization: Bearer <key>`` matches, in constant time."""
    value = _get(headers, "authorization")
    if not value or not value.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(value[7:].strip().encode(), expected_key.encode())


def extension_active(headers: Mapping[str, str]) -> bool:
    value = _get(headers, EXTENSIONS_HEADER)
    if not value:
        return False
    return EXTENSION_URI in [v.strip() for v in value.split(",")]


def response_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Headers to echo on the upgrade response to confirm the extension."""
    return [(EXTENSIONS_HEADER, EXTENSION_URI)] if extension_active(headers) else []
