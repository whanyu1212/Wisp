"""OpenAI Codex / ChatGPT subscription OAuth helpers."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import anyio
import httpx

from wisp.auth.storage import OAuthCredential

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
JWT_CLAIM_PATH = "https://api.openai.com/auth"


@dataclass(frozen=True)
class DeviceCodeInfo:
    """Information the user needs to complete a device-code login."""

    user_code: str
    verification_uri: str
    interval_seconds: float
    expires_in_seconds: int


type DeviceCodeCallback = Callable[[DeviceCodeInfo], None]
type DeviceCodeProgressCallback = Callable[[int], None]


async def login_openai_codex_device_code(
    *,
    on_device_code: DeviceCodeCallback | None = None,
    on_progress: DeviceCodeProgressCallback | None = None,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await active_client.post(
            DEVICE_USER_CODE_URL,
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 404:
            raise RuntimeError("OpenAI Codex device code login is not enabled")
        _raise_for_token_response(response, operation="device code request")
        payload = response.json()
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        interval = payload.get("interval")
        interval_seconds = float(interval) if isinstance(interval, str | int | float) else None
        if (
            not isinstance(device_auth_id, str)
            or not isinstance(user_code, str)
            or interval_seconds is None
        ):
            raise RuntimeError(f"Invalid OpenAI Codex device code response: {payload}")
        info = DeviceCodeInfo(
            user_code=user_code,
            verification_uri=DEVICE_VERIFICATION_URI,
            interval_seconds=interval_seconds,
            expires_in_seconds=DEVICE_CODE_TIMEOUT_SECONDS,
        )
        if on_device_code is not None:
            on_device_code(info)
        authorization_code, code_verifier = await _poll_device_code(
            active_client,
            device_auth_id=device_auth_id,
            user_code=user_code,
            interval_seconds=interval_seconds,
            on_progress=on_progress,
        )
        return await _exchange_authorization_code(
            authorization_code,
            code_verifier,
            redirect_uri=DEVICE_REDIRECT_URI,
            client=active_client,
        )
    finally:
        if owns_client:
            await _close_client(active_client)


async def refresh_openai_codex_token(
    credential: OAuthCredential,
    *,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await active_client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh,
                "client_id": CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _read_token_response(response, operation="refresh")
    finally:
        if owns_client:
            await _close_client(active_client)


def account_id_from_access_token(access_token: str) -> str:
    payload = _decode_jwt_payload(access_token)
    auth_claim = payload.get(JWT_CLAIM_PATH)
    account_id = auth_claim.get("chatgpt_account_id") if isinstance(auth_claim, dict) else None
    if not isinstance(account_id, str) or not account_id:
        raise RuntimeError("Failed to extract ChatGPT account id from OpenAI Codex token")
    return account_id


async def _poll_device_code(
    client: httpx.AsyncClient,
    *,
    device_auth_id: str,
    user_code: str,
    interval_seconds: float,
    on_progress: DeviceCodeProgressCallback | None = None,
) -> tuple[str, str]:
    deadline = time.monotonic() + DEVICE_CODE_TIMEOUT_SECONDS
    interval = max(1.0, interval_seconds)
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        response = await client.post(
            DEVICE_TOKEN_URL,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json"},
        )
        if response.is_success:
            payload = response.json()
            authorization_code = payload.get("authorization_code")
            code_verifier = payload.get("code_verifier")
            if isinstance(authorization_code, str) and isinstance(code_verifier, str):
                return authorization_code, code_verifier
            raise RuntimeError(f"Invalid OpenAI Codex device auth token response: {payload}")
        if response.status_code not in {403, 404}:
            payload = _safe_json(response)
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else error
            if code == "slow_down":
                interval += 1
            elif code not in {"deviceauth_authorization_pending", None}:
                raise RuntimeError(
                    f"OpenAI Codex device auth failed with status {response.status_code}: "
                    f"{response.text}"
                )
        if on_progress is not None:
            on_progress(attempt)
        await anyio.sleep(interval)
    raise RuntimeError("OpenAI Codex device code login timed out")


async def _exchange_authorization_code(
    code: str,
    verifier: str,
    *,
    redirect_uri: str,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await active_client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _read_token_response(response, operation="exchange")
    finally:
        if owns_client:
            await _close_client(active_client)


def _read_token_response(response: httpx.Response, *, operation: str) -> OAuthCredential:
    _raise_for_token_response(response, operation=operation)
    payload = response.json()
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access, str)
        or not isinstance(refresh, str)
        or not isinstance(expires_in, int | float)
    ):
        raise RuntimeError(f"OpenAI Codex token {operation} response missing fields: {payload}")
    return OAuthCredential(
        access=access,
        refresh=refresh,
        expires=int(time.time() * 1000 + float(expires_in) * 1000),
        account_id=account_id_from_access_token(access),
    )


def _raise_for_token_response(response: httpx.Response, *, operation: str) -> None:
    if response.is_success:
        return
    raise RuntimeError(
        f"OpenAI Codex token {operation} failed ({response.status_code}): "
        f"{response.text or response.reason_phrase}"
    )


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("OpenAI Codex token is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        raw = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("OpenAI Codex token payload is invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("OpenAI Codex token payload is not an object")
    return cast(dict[str, Any], raw)


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


async def _close_client(client: httpx.AsyncClient) -> None:
    with anyio.CancelScope(shield=True):
        await client.aclose()


__all__ = [
    "DeviceCodeInfo",
    "account_id_from_access_token",
    "login_openai_codex_device_code",
    "refresh_openai_codex_token",
]
