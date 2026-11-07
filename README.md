# MilestonePay

An invoice-on-evidence agent for freelancers and small agencies who
bill per milestone. MilestonePay reads free-text milestone evidence,
extracts a structured invoice draft, refuses to invoice without a
recorded human approval, creates the draft through PayPal's
Invoicing API v2, and reconciles payments into a ledger that trusts
only PayPal's canonical invoice record.

Built for the PayPal AI Hackathon (Devpost). **Sandbox only** — the
code asserts the PayPal sandbox base URL before any live call; no
production endpoint and no real money is involved anywhere.

## The design rules

1. **The machine drafts.** Every extracted field carries a value, a
   confidence score, the source quotation from the evidence, and a
   derivation note. A field the evidence does not support stays
   empty and is flagged — never filled in by guess.
2. **The human decides.** A draft becomes an invoice only after a
   recorded approval (approver + timestamp, written to an
   append-only audit log). The invoicing hand-off itself enforces
   the gate: unapproved drafts, rejected drafts, and approved drafts
   with blocking gaps (e.g. a missing amount) are refused.
3. **The ledger trusts only the source.** Webhook events are
   signature-verified via PayPal's verify-webhook-signature API,
   and the ledger is updated from the canonical invoice record
   fetched from PayPal — never from the webhook payload. Duplicate
   deliveries are ignored by event id.

## Architecture

```
evidence.txt
    |
    v
agent/                  extraction providers (one interface)
  draft.py              MilestoneDraft: fields + confidence + quotes + flags
  extractors.py         DeterministicExtractor (offline stand-in, labelled)
                        LLMExtractor (OpenAI-compatible adapter, fail-closed)
  approval.py           human approval gate + append-only audit log
  handoff.py            approved draft -> PayPal Invoicing API v2 DRAFT
  demo.py               CLI demo over the fixtures
    |
    v
PayPal Invoicing API v2 (sandbox)          live_session_api.py (session CLI)
    |
    v
PayPal webhooks (public HTTPS URL)
    |
    v
webhook_listener.py     verify signature -> fetch canonical invoice
                        -> apply to ledger -> audit line (JSONL)
paypal_webhook_verifier.py   env loading, sandbox assertion, OAuth,
                             verification + canonical fetch helpers
run_sandbox_listener.py      runs the listener against the sandbox
revive_webhook.py            re-points the webhook after a tunnel restart
```

## Run the demo (offline, no PayPal contact)

```
python3 -m agent.demo                      # all three fixtures
python3 -m agent.demo evidence_clean.txt   # one fixture
```

Per fixture the demo prints the evidence, the extracted draft with
per-field confidence, the flags, a hand-off attempt *before*
approval (refused — the fake invoice client stays at 0 calls), the
recorded approval, and the post-approval hand-off against a fake
client. `python3 -m agent.demo --llm` selects the LLM adapter
instead; without the `MILESTONEPAY_AI_*` environment variables set
it fails closed and says which variables are missing.

## Run the tests (offline)

```
python3 test_agent_pipeline.py     # extraction, flags, gate, hand-off, adapter
python3 test_webhook_pipeline.py   # verify, dedupe, canonical ledger, hardening
```

Both suites run locally with no network and print `ALL TESTS
PASSED` on success.

## Live sandbox session (optional, needs your own sandbox app)

The listener and session tooling read PayPal sandbox credentials
from a local `.env` file (never committed; see `.gitignore`):

```
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
```

`run_sandbox_listener.py` registers/verifies the webhook over a
public HTTPS tunnel URL, verifies each delivery's signature, and
writes one audit line per event to `webhook_audit.jsonl`.
`live_session_api.py` is the operator CLI used in the proof
sessions (register webhook, create/send invoices, record external
payments, read canonical state).

## Honesty notes — read before quoting this project

These are part of the project, not fine print:

- **One** real delivered webhook payment event has been applied to
  the ledger so far: USD 40.00 against a USD 100.00 invoice,
  recorded through the API as an external payment, reconciling the
  invoice to PARTIALLY_PAID with USD 60.00 remaining. No
  full-payment reconciliation through buyer checkout is claimed;
  that line is still outstanding.
- The extraction provider used in the demo and tests is a
  **deterministic, offline stand-in**, labelled as such in its name
  and output. It is pattern-based and is **not an AI model**. The
  LLM adapter behind the same interface is real code —
  environment-configured, fail-closed, with output validated
  against the evidence — but it has **not yet run against a live
  model**; in tests it was exercised only against an injected fake
  transport.
- The agent's hand-off **creates a draft invoice** after approval;
  sending is a separate human step. The agent's own PayPal client
  reuses the code path proven in the live sandbox sessions but has
  **not yet made a live call itself** — a live smoke test is
  pending.

## Licence

MIT — see `LICENSE`.
