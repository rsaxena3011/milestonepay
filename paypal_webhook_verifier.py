#!/usr/bin/env python3
"""C016 MilestonePay: production PayPal webhook verifier + canonical fetcher.

Built 2026-11-04 (see WEBHOOK_PROOF_PREP_2026-11-04.md). SANDBOX ONLY:
every entry point asserts PAYPAL_ENV == "sandbox" and the sandbox API
base before any call is made.

Two injectables for webhook_listener.Pipeline:

  make_paypal_verify(session, access_token, webhook_id) -> verify(event, headers)
      Implements PayPal's documented postback verification:
      POST /v1/notifications/verify-webhook-signature with the seven
      documented fields (auth_algo, cert_url, transmission_id,
      transmission_sig, transmission_time from the PAYPAL-* delivery
      headers; webhook_id of the registered webhook; webhook_event =
      the parsed event body). Returns True only when PayPal answers
      verification_status == "SUCCESS". Fails CLOSED: missing headers,
      transport errors, non-200 responses, and FAILURE all return
      False. Note (PayPal docs): postback verification is NOT supported
      for Webhooks-simulator mock events; those carry webhook id
      "WEBHOOK_ID" and can only be checked with local cryptographic
      verification. Real delivered events are what this verifier is for.

  make_paypal_fetch(session, access_token) -> fetch(invoice_id)
      Canonical invoice state from GET /v2/invoicing/invoices/{id},
      mapped to the Ledger shape {status, amount_due, amount_paid}:
      amount_due is the invoice total (amount.value), amount_paid is
      total minus the still-due amount (due_amount.value). The ledger
      is therefore updated from PayPal's canonical record, never from
      the webhook payload (the design rule proven locally on
      2026-11-02).

Credentials are read from .env next to this file and stay in memory.
Nothing in this module prints or returns a credential or token value.
"""

import os

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
BASE = "https://api-m.sandbox.paypal.com"

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"
WEBHOOKS_PATH = "/v1/notifications/webhooks"
INVOICE_PATH = "/v2/invoicing/invoices/"

# The five delivery headers PayPal's verification API needs, lowercased
# (webhook_listener's handler forwards exactly the paypal-* headers).
HEADER_FIELDS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def load_env(path=ENV_PATH):
    env = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            env[key] = value
    return env


def assert_sandbox(env):
    """Refuse to run against anything but the sandbox. Returns bool."""
    return BASE == "https://api-m.sandbox.paypal.com" and \
        env.get("PAYPAL_ENV", "").lower() == "sandbox"


def get_access_token(session, env):
    """Client-credentials token. Returns (token, granted_scope_string).

    The token itself must never be printed or persisted by callers.
    Raises RuntimeError with a status-only message on failure.
    """
    resp = session.post(
        BASE + "/v1/oauth2/token",
        auth=(env.get("PAYPAL_CLIENT_ID", ""), env.get("PAYPAL_CLIENT_SECRET", "")),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=30,
        allow_redirects=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token request failed: HTTP {resp.status_code}")
    body = resp.json()
    token = body.get("access_token", "")
    if not token:
        raise RuntimeError("token response had no access token")
    return token, body.get("scope", "") or ""


def build_verify_payload(headers, webhook_id, event):
    """Assemble the documented verify-webhook-signature request body.

    Returns None when any required transmission header is missing
    (verification cannot be attempted; the caller must fail closed).
    """
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    payload = {"webhook_id": webhook_id, "webhook_event": event}
    for field, header_name in HEADER_FIELDS.items():
        value = headers.get(header_name)
        if not value:
            return None
        payload[field] = value
    return payload


def make_paypal_verify(session, access_token, webhook_id):
    """Return verify(event, headers) backed by PayPal's sandbox API."""

    def verify(event, headers=None):
        payload = build_verify_payload(headers, webhook_id, event)
        if payload is None:
            return False
        try:
            resp = session.post(
                BASE + VERIFY_PATH,
                headers={
                    "Authorization": "Bearer " + access_token,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException:
            return False
        if resp.status_code != 200:
            return False
        try:
            body = resp.json()
        except ValueError:
            return False
        return isinstance(body, dict) and \
            body.get("verification_status") == "SUCCESS"

    return verify


def make_paypal_fetch(session, access_token):
    """Return fetch(invoice_id) -> {status, amount_due, amount_paid}."""

    def fetch(invoice_id):
        resp = session.get(
            BASE + INVOICE_PATH + invoice_id,
            headers={"Authorization": "Bearer " + access_token},
            timeout=30,
            allow_redirects=False,
        )
        resp.raise_for_status()
        body = resp.json()
        total = float((body.get("amount") or {}).get("value", "0"))
        due = float((body.get("due_amount") or {}).get("value", "0"))
        return {
            "status": body.get("status", ""),
            "amount_due": total,
            "amount_paid": round(total - due, 2),
        }

    return fetch


def list_webhooks(session, access_token):
    """GET the app's registered webhooks. Returns the parsed body dict."""
    resp = session.get(
        BASE + WEBHOOKS_PATH,
        headers={"Authorization": "Bearer " + access_token},
        timeout=30,
        allow_redirects=False,
    )
    return resp.status_code, resp.json() if resp.headers.get(
        "Content-Type", "").startswith("application/json") else {}
