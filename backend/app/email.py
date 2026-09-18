"""Sends the "you were fined" transactional email via Resend's API.

Separate from Supabase Auth's mailer on purpose — that one only sends its
own login emails (magic link, etc.), never arbitrary content. This is a
thin, best-effort client: a failed send here must never break the check
pipeline that triggered it, only get logged.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("vigila")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "VigilaMultas <noreply@vigilamultas.com>")


def send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return False
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            timeout=15.0,
        )
    except httpx.HTTPError:
        logger.exception("resend request failed for %s", to)
        return False
    if resp.status_code >= 300:
        logger.warning("resend rejected the email to %s: %s %s", to, resp.status_code, resp.text[:300])
        return False
    return True
