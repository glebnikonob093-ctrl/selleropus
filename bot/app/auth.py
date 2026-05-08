"""Telegram WebApp initData verification.

The Mini App receives a string `initData` from Telegram on launch. The string
is a URL-encoded query string with one of the fields being `hash` and another
being `auth_date`. We verify that the hash matches the HMAC-SHA256 of the
sorted, newline-joined key=value pairs (excluding `hash`) using the bot token
as the secret key.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """Raised when initData is missing, malformed or has invalid signature."""


@dataclass(frozen=True)
class InitDataUser:
    id: int
    first_name: str
    last_name: str
    username: str
    language_code: str


@dataclass(frozen=True)
class InitData:
    user: InitDataUser
    auth_date: int
    raw: dict[str, str]
    start_param: str


def parse_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 24 * 60 * 60,
) -> InitData:
    if not init_data:
        raise InitDataError("initData is empty")
    if not bot_token:
        raise InitDataError("bot token is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise InitDataError("hash is missing")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("hash mismatch")

    auth_date_raw = pairs.get("auth_date", "")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise InitDataError("invalid auth_date") from exc

    if max_age_seconds > 0 and time.time() - auth_date > max_age_seconds:
        raise InitDataError("initData expired")

    user_raw = pairs.get("user", "")
    if not user_raw:
        raise InitDataError("user is missing")
    try:
        user_obj = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("user is not valid json") from exc

    if not isinstance(user_obj, dict) or "id" not in user_obj:
        raise InitDataError("user payload is malformed")

    user = InitDataUser(
        id=int(user_obj["id"]),
        first_name=str(user_obj.get("first_name", "") or ""),
        last_name=str(user_obj.get("last_name", "") or ""),
        username=str(user_obj.get("username", "") or ""),
        language_code=str(user_obj.get("language_code", "") or ""),
    )

    return InitData(
        user=user,
        auth_date=auth_date,
        raw=pairs,
        start_param=pairs.get("start_param", ""),
    )
