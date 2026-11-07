#!/usr/bin/env python3
"""C016 MilestonePay: live-session API helper (2026-11-05).

SANDBOX ONLY -- every command asserts PAYPAL_ENV == "sandbox" and the
sandbox API base (via paypal_webhook_verifier.assert_sandbox) before
any call. Credentials are read from .env and stay in memory; the OAuth
token and the webhook id are never printed. The webhook id is written
only to /tmp/c016_webhook_id (mode 600) for the listener launcher.

Commands:
  register <tunnel_base_url>  POST /v1/notifications/webhooks with the
                              six valid INVOICING.* event types (a
                              seventh name was rejected live on
                              2026-11-05; event-types listing 404s on
                              this app, so the POST itself validates
                              the names).
  list                        GET webhooks; print count + URLs only.
  invoice <label>             Create + send a USD 100.00 invoice (the
                              proven 2026-11-02 retest pattern) and read
                              it back; prints invoice id, status, and
                              the payer-view link for the buyer step.
  extpay <invoice_id>         Record an external (outside PayPal)
                              partial payment of USD 40.00 via POST
                              /v2/invoicing/invoices/{id}/payments,
                              then read the invoice back canonically.
"""

import os
import sys
import uuid

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paypal_webhook_verifier import (  # noqa: E402
    BASE, assert_sandbox, get_access_token, list_webhooks, load_env,
)

EVENT_TYPES = [
    "INVOICING.INVOICE.CREATED",
    "INVOICING.INVOICE.UPDATED",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.REFUNDED",
    "INVOICING.INVOICE.CANCELLED",
    "INVOICING.INVOICE.SCHEDULED",
    # "INVOICING.PAYMENT.PAYMENT.CREATED" was REJECTED by the live
    # registration POST on 2026-11-05 (400 VALIDATION_ERROR:
    # event_types[6].name "Not a valid event name"), so it is not
    # subscribed; the other six INVOICING.* names validated fine.
]

WEBHOOK_ID_TMP = "/tmp/c016_webhook_id"


def make_session():
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    return session


def auth_headers(token, request_id=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    return headers


def cmd_register(session, token, tunnel_base):
    url = tunnel_base.rstrip("/") + "/webhook"
    payload = {
        "url": url,
        "event_types": [{"name": name} for name in EVENT_TYPES],
    }
    resp = session.post(
        BASE + "/v1/notifications/webhooks",
        headers=auth_headers(token),
        json=payload,
        timeout=30,
        allow_redirects=False,
    )
    print("REGISTER_HTTP_STATUS:", resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 201:
        print("REGISTER_ERROR_NAME:", body.get("name", "unknown"))
        print("REGISTER_ERROR_MESSAGE:", body.get("message", ""))
        details = body.get("details", [])
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict):
                    print("REGISTER_ERROR_DETAIL:",
                          d.get("field", ""), "|", d.get("issue", ""))
        return 1
    webhook_id = body.get("id", "")
    if not webhook_id:
        print("RESULT: registration returned no webhook id.")
        return 1
    fd = os.open(WEBHOOK_ID_TMP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(webhook_id)
    # O_CREAT mode does not tighten an already-existing file's perms.
    os.chmod(WEBHOOK_ID_TMP, 0o600)
    names = [e.get("name", "") for e in body.get("event_types", [])
             if isinstance(e, dict)]
    print("REGISTERED_URL:", body.get("url", url))
    print("REGISTERED_EVENT_TYPES:", len(names))
    for name in names:
        print("  EVENT:", name)
    print("RESULT: webhook registered; id captured to listener env file.")
    return 0


def cmd_list(session, token):
    status, body = list_webhooks(session, token)
    print("LIST_HTTP_STATUS:", status)
    hooks = body.get("webhooks", []) if isinstance(body, dict) else []
    print("WEBHOOK_COUNT:", len(hooks))
    for hook in hooks:
        if isinstance(hook, dict):
            names = [e.get("name", "") for e in hook.get("event_types", [])
                     if isinstance(e, dict)]
            print("  WEBHOOK_URL:", hook.get("url", ""), "| events:", len(names))
    return 0 if status == 200 else 1


def extract_id_from_href(href):
    if isinstance(href, str) and "/invoices/" in href:
        return href.rsplit("/invoices/", 1)[-1].strip("/")
    return ""


def cmd_invoice(session, token, label):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "detail": {
            "invoice_number": "C016-LIVE-" + label + "-" + suffix,
            "currency_code": "USD",
            "note": "MilestonePay live webhook proof session, 2026-11-05.",
            "payment_term": {"term_type": "NET_10"},
        },
        "invoicer_info": {
            "name": {"given_name": "MilestonePay", "surname": "Sandbox"},
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "name": {"given_name": "Sandbox", "surname": "Buyer"},
                    "email_address": "c016-buyer@personal.example.com",
                }
            }
        ],
        "items": [
            {
                "name": "Milestone 1: evidence pack delivery",
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": "100.00"},
            }
        ],
    }
    draft = session.post(
        BASE + "/v2/invoicing/invoices",
        headers=auth_headers(token, "C016-LIVE-CREATE-" + suffix),
        json=payload,
        timeout=30,
        allow_redirects=False,
    )
    print("DRAFT_HTTP_STATUS:", draft.status_code)
    if draft.status_code != 201:
        try:
            err = draft.json()
        except ValueError:
            err = {}
        print("DRAFT_ERROR_NAME:", err.get("name", "unknown"))
        print("DRAFT_ERROR_MESSAGE:", err.get("message", ""))
        print("DRAFT_ERROR_DEBUG_ID:", err.get("debug_id", ""))
        return 1
    try:
        draft_body = draft.json()
    except ValueError:
        draft_body = {}
    invoice_id = draft_body.get("id", "") or \
        extract_id_from_href(draft_body.get("href", ""))
    if not invoice_id:
        print("RESULT: draft response had no invoice id.")
        return 1
    print("INVOICE_ID:", invoice_id)

    send = session.post(
        BASE + "/v2/invoicing/invoices/" + invoice_id + "/send",
        headers=auth_headers(token, "C016-LIVE-SEND-" + suffix),
        json={"send_to_invoicer": True, "send_to_recipient": True},
        timeout=30,
        allow_redirects=False,
    )
    print("SEND_HTTP_STATUS:", send.status_code)
    if send.status_code not in (200, 202, 204):
        try:
            err = send.json()
        except ValueError:
            err = {}
        print("SEND_ERROR_NAME:", err.get("name", "unknown"))
        print("SEND_ERROR_MESSAGE:", err.get("message", ""))
        print("SEND_ERROR_DEBUG_ID:", err.get("debug_id", ""))
        return 1

    read = session.get(
        BASE + "/v2/invoicing/invoices/" + invoice_id,
        headers=auth_headers(token),
        timeout=30,
        allow_redirects=False,
    )
    print("READ_HTTP_STATUS:", read.status_code)
    if read.status_code != 200:
        return 1
    body = read.json()
    print("INVOICE_STATUS:", body.get("status", ""))
    amount = body.get("amount") or {}
    due = body.get("due_amount") or {}
    print("INVOICE_AMOUNT:", amount.get("currency_code", ""),
          amount.get("value", ""))
    print("INVOICE_DUE:", due.get("currency_code", ""), due.get("value", ""))
    for link in body.get("links", []):
        if isinstance(link, dict) and "payer" in str(link.get("rel", "")):
            print("PAYER_LINK:", link.get("href", ""))
    return 0


def cmd_extpay(session, token, invoice_id):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "method": "OTHER",
        "amount": {"currency_code": "USD", "value": "40.00"},
        "note": "Partial external payment, live webhook proof 2026-11-05.",
    }
    resp = session.post(
        BASE + "/v2/invoicing/invoices/" + invoice_id + "/payments",
        headers=auth_headers(token, "C016-LIVE-EXTPAY-" + suffix),
        json=payload,
        timeout=30,
        allow_redirects=False,
    )
    print("EXTPAY_HTTP_STATUS:", resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code not in (200, 201):
        print("EXTPAY_ERROR_NAME:", body.get("name", "unknown"))
        print("EXTPAY_ERROR_MESSAGE:", body.get("message", ""))
        print("EXTPAY_ERROR_DEBUG_ID:", body.get("debug_id", ""))
        return 1
    href = body.get("href", "") if isinstance(body, dict) else ""
    if href:
        print("EXTPAY_PAYMENT_ID:", href.rstrip("/").rsplit("/", 1)[-1])
    read = session.get(
        BASE + "/v2/invoicing/invoices/" + invoice_id,
        headers=auth_headers(token),
        timeout=30,
        allow_redirects=False,
    )
    print("READ_HTTP_STATUS:", read.status_code)
    if read.status_code == 200:
        rbody = read.json()
        due = rbody.get("due_amount") or {}
        print("INVOICE_STATUS:", rbody.get("status", ""))
        print("INVOICE_DUE:", due.get("currency_code", ""),
              due.get("value", ""))
    return 0


def main():
    env = load_env()
    if not assert_sandbox(env):
        print("ABORT: PAYPAL_ENV is not 'sandbox' or base is not the sandbox API.")
        return 2
    if len(sys.argv) < 2:
        print("usage: live_session_api.py register|list|invoice|extpay ...")
        return 2
    session = make_session()
    try:
        token, _scopes = get_access_token(session, env)
    except RuntimeError as exc:
        print("ABORT:", exc)
        return 1
    cmd = sys.argv[1]
    if cmd == "register" and len(sys.argv) == 3:
        return cmd_register(session, token, sys.argv[2])
    if cmd == "list":
        return cmd_list(session, token)
    if cmd == "invoice" and len(sys.argv) == 3:
        return cmd_invoice(session, token, sys.argv[2])
    if cmd == "extpay" and len(sys.argv) == 3:
        return cmd_extpay(session, token, sys.argv[2])
    print("usage: live_session_api.py register <base>|list|invoice <label>|extpay <id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
