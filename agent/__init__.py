"""C016 MilestonePay — the AI half: evidence -> draft invoice.

Pipeline (MERGED_PLAN_2026-10-30.md, product steps 1 and 3):

  evidence text
    -> extractor (provider interface; deterministic local provider,
       or an LLM adapter configured purely from environment variables)
    -> MilestoneDraft (every field carries value + confidence +
       source quote; missing values stay None and are flagged,
       never invented)
    -> human approval gate (approval.approve_draft; recorded on the
       draft and in an append-only JSONL audit log, mirroring the
       webhook_audit.jsonl discipline)
    -> handoff (handoff.create_invoice_for_draft refuses any draft
       that is not APPROVED, and any draft with blocking gaps;
       an approved, complete draft becomes a PayPal Invoicing API
       v2 DRAFT invoice via the same payload shape and client code
       path as live_session_api.py)

Nothing in this package sends an invoice, records a payment, or
moves money. Draft creation is the only PayPal-side action, and it
is gated on a recorded human approval.
"""

from .draft import FieldExtraction, MilestoneDraft  # noqa: F401
from .extractors import (  # noqa: F401
    DeterministicExtractor,
    LLMExtractor,
    ProviderNotConfigured,
)
from .approval import approve_draft, reject_draft  # noqa: F401
from .handoff import (  # noqa: F401
    ApprovalRequiredError,
    FakeInvoiceClient,
    PayPalInvoiceClient,
    build_invoice_payload,
    create_invoice_for_draft,
)
