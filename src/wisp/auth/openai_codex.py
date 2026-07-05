"""OpenAI Codex / ChatGPT subscription OAuth helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import anyio
import httpx

from wisp.auth.storage import OAuthCredential

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
OPENAI_CODEX_SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"


class OpenAICodexLoginMethod(StrEnum):
    """Supported OpenAI Codex login methods."""

    browser = "browser"
    device_code = "device-code"


@dataclass(frozen=True)
class DeviceCodeInfo:
    """Information the user needs to complete a device-code login."""

    user_code: str
    verification_uri: str
    interval_seconds: float
    expires_in_seconds: int


type AuthUrlCallback = Callable[[str], None]
type DeviceCodeCallback = Callable[[DeviceCodeInfo], None]
type PromptCallback = Callable[[str], str]


async def login_openai_codex(
    *,
    method: OpenAICodexLoginMethod = OpenAICodexLoginMethod.browser,
    on_auth_url: AuthUrlCallback | None = None,
    on_device_code: DeviceCodeCallback | None = None,
    prompt: PromptCallback | None = None,
    open_browser: bool = True,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Run an OpenAI Codex login flow and return OAuth credentials.

    The browser flow intentionally supports manual code/redirect URL paste so it
    can work even when the local callback port is unavailable.
    """

    if method is OpenAICodexLoginMethod.device_code:
        return await login_openai_codex_device_code(
            on_device_code=on_device_code,
            client=client,
        )
    return await login_openai_codex_browser(
        on_auth_url=on_auth_url,
        prompt=prompt,
        open_browser=open_browser,
        client=client,
    )


async def login_openai_codex_browser(
    *,
    on_auth_url: AuthUrlCallback | None = None,
    prompt: PromptCallback | None = None,
    open_browser: bool = True,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    verifier, challenge = _generate_pkce()
    state = secrets.token_hex(16)
    authorize_url = _authorization_url(challenge=challenge, state=state)
    if on_auth_url is not None:
        on_auth_url(authorize_url)
    if open_browser:
        webbrowser.open(authorize_url)
    if prompt is None:
        raise RuntimeError("OpenAI Codex browser login requires a prompt callback")
    raw_code = prompt("Paste the authorization code or full redirect URL")
    parsed_code, parsed_state = _parse_authorization_input(raw_code)
    if parsed_state is not None and parsed_state != state:
        raise RuntimeError("OpenAI Codex login failed: state mismatch")
    if not parsed_code:
        raise RuntimeError("OpenAI Codex login failed: missing authorization code")
    return await _exchange_authorization_code(
        parsed_code,
        verifier,
        redirect_uri=REDIRECT_URI,
        client=client,
    )


async def login_openai_codex_device_code(
    *,
    on_device_code: DeviceCodeCallback | None = None,
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
            raise RuntimeError(
                "OpenAI Codex device code login is not enabled; use browser login instead"
            )
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
        )
        return await _exchange_authorization_code(
            authorization_code,
            code_verifier,
            redirect_uri=DEVICE_REDIRECT_URI,
            client=active_client,
        )
    finally:
        if owns_client:
            await active_client.aclose()


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
            await active_client.aclose()


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
) -> tuple[str, str]:
    deadline = time.monotonic() + DEVICE_CODE_TIMEOUT_SECONDS
    interval = max(1.0, interval_seconds)
    while time.monotonic() < deadline:
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
            await active_client.aclose()


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


def _authorization_url(*, challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": OPENAI_CODEX_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "wisp",
    }
    return f"{AUTHORIZE_URL}?{httpx.QueryParams(params)}"


def _generate_pkce() -> tuple[str, str]:
    verifier = _base64url(secrets.token_bytes(32))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, _base64url(digest)


def _parse_authorization_input(value: str) -> tuple[str | None, str | None]:
    stripped = value.strip()
    if not stripped:
        return None, None
    try:
        url = httpx.URL(stripped)
        code = url.params.get("code")
        state = url.params.get("state")
        if code or state:
            return code, state
    except httpx.InvalidURL:
        pass
    if "#" in stripped:
        code, state = stripped.split("#", 1)
        return code or None, state or None
    if "code=" in stripped:
        params = httpx.QueryParams(stripped)
        return params.get("code"), params.get("state")
    return stripped, None


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


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


__all__ = [
    "DeviceCodeInfo",
    "OpenAICodexLoginMethod",
    "account_id_from_access_token",
    "login_openai_codex",
    "login_openai_codex_browser",
    "login_openai_codex_device_code",
    "refresh_openai_codex_token",
]
