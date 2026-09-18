"""Cookie-based sessions backed by Supabase Auth's magic-link flow.

Supabase Auth (GoTrue) sends the magic-link email and issues the JWTs once
the user clicks it — this module never sees a password. It only:

1. Takes the access/refresh tokens the frontend receives after a
   successful magic-link login and wraps them in HttpOnly, Secure,
   SameSite=None cookies (HttpOnly so the tokens are never readable by
   page JS — no XSS token theft; SameSite=None+Secure because the
   frontend on GitHub Pages and this API on Render are different origins).
2. On every authenticated request, asks Supabase's own /auth/v1/user
   endpoint whether the access-token cookie is still valid, rather than
   verifying a JWT signature locally — this avoids holding a separate
   signing secret and stays correct if Supabase ever rotates keys.
3. Transparently refreshes an expired access token using the longer-lived
   refresh-token cookie, so a logged-in user isn't bounced to the login
   screen every time the ~1h access token expires.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request, Response

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

ACCESS_COOKIE = "sb_access_token"
REFRESH_COOKIE = "sb_refresh_token"

# The refresh token cookie has to outlive the short-lived access token by a
# lot, or "staying logged in" would be pointless.
REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 días


class InvalidSession(Exception):
    pass


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str


def _cookie_kwargs(max_age: int) -> dict:
    return {"httponly": True, "secure": True, "samesite": "none", "max_age": max_age, "path": "/"}


def set_session_cookies(response: Response, access_token: str, refresh_token: str, expires_in: int) -> None:
    response.set_cookie(ACCESS_COOKIE, access_token, **_cookie_kwargs(expires_in))
    response.set_cookie(REFRESH_COOKIE, refresh_token, **_cookie_kwargs(REFRESH_COOKIE_MAX_AGE))


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


def fetch_user(access_token: str) -> AuthUser:
    """Ask Supabase Auth who this access token belongs to. Raises
    InvalidSession if it's missing, expired or otherwise rejected."""
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {access_token}", "apikey": SUPABASE_ANON_KEY},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise InvalidSession(str(exc)) from exc
    if resp.status_code != 200:
        raise InvalidSession(f"supabase rejected the access token ({resp.status_code})")
    data = resp.json()
    return AuthUser(id=data["id"], email=data.get("email") or "")


def _refresh(refresh_token: str) -> dict:
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        json={"refresh_token": refresh_token},
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise InvalidSession(f"refresh rejected ({resp.status_code})")
    return resp.json()


def get_current_user(request: Request, response: Response) -> AuthUser:
    """FastAPI dependency: the logged-in user, or a 401.

    Accepts a `response` param so a transparent refresh can rewrite the
    cookies on the very response being built for this request — the caller
    doesn't need to do anything extra for that to work.
    """
    access_token = request.cookies.get(ACCESS_COOKIE)
    if access_token:
        try:
            return fetch_user(access_token)
        except InvalidSession:
            pass  # fall through and try a refresh

    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No has iniciado sesión")

    try:
        data = _refresh(refresh_token)
        set_session_cookies(response, data["access_token"], data["refresh_token"], data["expires_in"])
        return fetch_user(data["access_token"])
    except InvalidSession:
        raise HTTPException(status_code=401, detail="Tu sesión ha caducado, vuelve a acceder") from None
