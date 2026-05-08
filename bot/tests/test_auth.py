from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.auth import InitDataError, parse_init_data

BOT_TOKEN = "1234:ABC"


def _build_init_data(
    *,
    user: dict,
    auth_date: int | None = None,
    extra: dict | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    auth_date = auth_date if auth_date is not None else int(time.time())
    pairs: dict[str, str] = {
        "auth_date": str(auth_date),
        "query_id": "AAH",
        "user": json.dumps(user, separators=(",", ":")),
    }
    if extra:
        pairs.update({k: str(v) for k, v in extra.items()})

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    pairs["hash"] = h
    return urlencode(pairs)


def test_parse_init_data_happy_path() -> None:
    user = {"id": 42, "first_name": "Anna", "username": "annab", "language_code": "ru"}
    raw = _build_init_data(user=user)
    parsed = parse_init_data(raw, BOT_TOKEN)

    assert parsed.user.id == 42
    assert parsed.user.first_name == "Anna"
    assert parsed.user.username == "annab"


def test_parse_init_data_rejects_wrong_hash() -> None:
    user = {"id": 1, "first_name": "X"}
    raw = _build_init_data(user=user, bot_token="other")
    with pytest.raises(InitDataError):
        parse_init_data(raw, BOT_TOKEN)


def test_parse_init_data_rejects_expired() -> None:
    user = {"id": 1, "first_name": "X"}
    raw = _build_init_data(user=user, auth_date=int(time.time()) - 10**6)
    with pytest.raises(InitDataError):
        parse_init_data(raw, BOT_TOKEN, max_age_seconds=60)


def test_parse_init_data_rejects_missing_hash() -> None:
    with pytest.raises(InitDataError):
        parse_init_data("auth_date=1&user=%7B%22id%22%3A1%7D", BOT_TOKEN)


def test_parse_init_data_rejects_missing_user() -> None:
    auth_date = int(time.time())
    pairs = {"auth_date": str(auth_date)}
    data_check_string = f"auth_date={auth_date}"
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    pairs["hash"] = h
    raw = urlencode(pairs)
    with pytest.raises(InitDataError):
        parse_init_data(raw, BOT_TOKEN)
