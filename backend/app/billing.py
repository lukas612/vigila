"""Stripe subscription billing: checkout, billing portal, and webhook
handling. Raw httpx calls against Stripe's REST API, no SDK — same pattern
as email.py's Resend client, and the surface used here (checkout sessions,
billing portal, one webhook) is too small to justify the SDK's dependency
weight.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from .models import Plan, SubscriptionStatus, User

logger = logging.getLogger("vigila")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# The billing_portal.configuration that lets a customer switch between the
# Individual/Familiar prices (upgrade/downgrade), not just cancel or update
# a payment method — that has to be created once (see the "Billing (Stripe)"
# README section) since the portal's default config is cancel-only.
STRIPE_PORTAL_CONFIGURATION_ID = os.environ.get("STRIPE_PORTAL_CONFIGURATION_ID", "")

# price -> plan, both directions. A plan with no price id configured just
# can't be checked out (create_checkout_session raises), which is a config
# problem to fix in the environment, not something to fall back silently on.
PLAN_PRICE_IDS = {
    Plan.individual: os.environ.get("STRIPE_PRICE_ID_INDIVIDUAL", ""),
    Plan.familiar: os.environ.get("STRIPE_PRICE_ID_FAMILIAR", ""),
}
PRICE_ID_TO_PLAN = {price_id: plan for plan, price_id in PLAN_PRICE_IDS.items() if price_id}

# How many targets each plan allows once its subscription is active/trialing
# (see main._max_targets). No plan, or no active subscription, means 0.
PLAN_TARGET_LIMITS = {
    Plan.individual: 1,
    Plan.familiar: 5,
}

# For the admin panel's MRR estimate (main.admin_billing) — the current
# list price per plan, not each customer's actual invoiced amount (which
# could differ after a discount/coupon). Good enough for a founder-scale
# dashboard; pull real invoice totals from Stripe if that gap ever matters.
PLAN_PRICES_EUR = {
    Plan.individual: 3.99,
    Plan.familiar: 6.99,
}

_API_BASE = "https://api.stripe.com/v1"
_WEBHOOK_TOLERANCE_SECONDS = 300

_STATUS_MAP = {
    "trialing": SubscriptionStatus.trialing,
    "active": SubscriptionStatus.active,
    "past_due": SubscriptionStatus.past_due,
    "unpaid": SubscriptionStatus.past_due,
    "canceled": SubscriptionStatus.canceled,
    "incomplete_expired": SubscriptionStatus.canceled,
    "paused": SubscriptionStatus.canceled,
}


class StripeError(RuntimeError):
    """A Stripe API call failed, or a webhook's signature didn't check out."""


def _request(method: str, path: str, **form_data: str) -> dict:
    if not STRIPE_SECRET_KEY:
        raise StripeError("STRIPE_SECRET_KEY no configurada")
    try:
        resp = httpx.request(
            method,
            f"{_API_BASE}{path}",
            auth=(STRIPE_SECRET_KEY, ""),
            data=form_data,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("stripe %s %s failed to connect", method, path)
        raise StripeError("No se pudo contactar con Stripe") from exc
    if resp.status_code >= 300:
        logger.warning("stripe %s %s rejected: %s %s", method, path, resp.status_code, resp.text[:500])
        raise StripeError(f"Stripe rechazó la solicitud ({resp.status_code})")
    return resp.json()


def create_checkout_session(user: User, plan: Plan, success_url: str, cancel_url: str) -> str:
    """Starts a monthly subscription checkout for `plan`. Collects the
    customer's full name and billing address (billing_address_collection),
    a phone number (phone_number_collection), and offers a CIF/VAT field
    (tax_id_collection) — a subscriber who's given a name and phone reads
    as a real, accountable customer, not just an email address, and
    business customers need the CIF for a valid invoice. All of this comes
    back on the checkout.session.completed webhook's `customer_details`
    and is copied onto our own User row there (see apply_event)."""
    price_id = PLAN_PRICE_IDS.get(plan)
    if not price_id:
        raise StripeError(f"No hay price de Stripe configurado para el plan {plan.value}")

    params = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": user.id,
        "billing_address_collection": "required",
        "phone_number_collection[enabled]": "true",
        "tax_id_collection[enabled]": "true",
        "metadata[user_id]": user.id,
        "metadata[plan]": plan.value,
        "subscription_data[metadata][user_id]": user.id,
        "subscription_data[metadata][plan]": plan.value,
    }
    if user.stripe_customer_id:
        params["customer"] = user.stripe_customer_id
    else:
        params["customer_email"] = user.email
    session = _request("POST", "/checkout/sessions", **params)
    return session["url"]


def create_portal_session(customer_id: str, return_url: str) -> str:
    params = {"customer": customer_id, "return_url": return_url}
    if STRIPE_PORTAL_CONFIGURATION_ID:
        params["configuration"] = STRIPE_PORTAL_CONFIGURATION_ID
    session = _request("POST", "/billing_portal/sessions", **params)
    return session["url"]


def verify_webhook_signature(payload: bytes, sig_header: str) -> dict:
    """Checks Stripe's Stripe-Signature header by hand: HMAC-SHA256 of
    "{timestamp}.{payload}" against STRIPE_WEBHOOK_SECRET, constant-time
    compared, with a 5-minute tolerance against replay. Returns the parsed
    event on success; raises StripeError otherwise."""
    if not STRIPE_WEBHOOK_SECRET:
        raise StripeError("STRIPE_WEBHOOK_SECRET no configurada")
    parts = dict(item.split("=", 1) for item in sig_header.split(",") if "=" in item)
    timestamp, signature = parts.get("t"), parts.get("v1")
    if not timestamp or not signature:
        raise StripeError("Firma de webhook mal formada")
    if abs(time.time() - int(timestamp)) > _WEBHOOK_TOLERANCE_SECONDS:
        raise StripeError("Firma de webhook expirada")
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise StripeError("Firma de webhook inválida")
    return json.loads(payload)


def _plan_from_subscription(sub: dict) -> Plan | None:
    try:
        price_id = sub["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    return PRICE_ID_TO_PLAN.get(price_id)


def apply_event(db: Session, event: dict) -> None:
    """Updates the local User row from a verified Stripe event. Unknown
    event types (we only subscribe to three, but Stripe retries old
    deliveries too) are logged and ignored rather than erroring, since the
    caller returns 200 either way — Stripe would just keep retrying."""
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        user_id = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
        if not user_id:
            logger.warning("checkout.session.completed with no user id: %s", obj.get("id"))
            return
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning("checkout.session.completed for unknown user %s", user_id)
            return
        if obj.get("customer"):
            user.stripe_customer_id = obj["customer"]
        customer_details = obj.get("customer_details") or {}
        if customer_details.get("name"):
            user.name = customer_details["name"]
        if customer_details.get("phone"):
            user.phone = customer_details["phone"]
        plan_value = (obj.get("metadata") or {}).get("plan")
        if plan_value:
            try:
                user.plan = Plan(plan_value)
            except ValueError:
                logger.warning("checkout.session.completed with unknown plan %r", plan_value)
        user.subscription_status = SubscriptionStatus.active
        if user.subscribed_at is None:
            user.subscribed_at = datetime.utcnow()
        db.commit()

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = obj.get("customer")
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            logger.warning("%s for unknown customer %s", event_type, customer_id)
            return
        user.subscription_status = _STATUS_MAP.get(obj.get("status", ""), SubscriptionStatus.none)
        if user.subscription_status == SubscriptionStatus.active and user.subscribed_at is None:
            user.subscribed_at = datetime.utcnow()
        plan = _plan_from_subscription(obj)
        if plan:
            user.plan = plan
        db.commit()

    elif event_type == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            logger.warning("subscription.deleted for unknown customer %s", customer_id)
            return
        user.subscription_status = SubscriptionStatus.canceled
        db.commit()

    else:
        logger.info("unhandled stripe event type: %s", event_type)
