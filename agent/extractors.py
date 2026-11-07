"""Evidence extractors: free-text milestone evidence -> MilestoneDraft.

Two providers behind one interface (EvidenceExtractor.extract):

  DeterministicExtractor — rules/patterns, fully offline, no model.
  This is a STAND-IN provider: it exists so the pipeline, the
  approval gate, and the tests run with zero external dependency.
  It is labelled a stand-in here, in the demo output, and in the
  build doc; it is never described as an AI model.

  LLMExtractor — a real adapter for an OpenAI-compatible
  chat-completions endpoint, configured ONLY from environment
  variables (MILESTONEPAY_AI_PROVIDER / _MODEL / _API_KEY /
  _BASE_URL). It fails closed with ProviderNotConfigured when any
  of provider/model/key is unset. Its output passes through the
  same deterministic validation as any provider: a field whose
  source quote does not appear in the evidence is capped at low
  confidence and flagged, and a value the model omits stays None.
  NOTE: in the 2026-11-05 build run this adapter was exercised
  only against an injected fake transport in tests — never against
  a live model. No claim of live-model extraction is supported.

Both providers finish by running draft.compute_flags, so the
flagging rules are identical whichever provider produced the draft.
"""

import json
import os
import re

from .draft import FieldExtraction, MilestoneDraft, compute_flags, FIELD_NAMES

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures")

FIXTURE_NAMES = (
    "evidence_clean.txt",
    "evidence_missing_amount.txt",
    "evidence_ambiguous.txt",
)


def load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class ProviderNotConfigured(RuntimeError):
    """Raised when the LLM provider lacks required configuration."""


class ExtractionError(RuntimeError):
    """Raised when a provider's output cannot be used at all."""


class EvidenceExtractor:
    """Provider interface: extract(evidence_text) -> MilestoneDraft."""

    name = "base"

    def extract(self, evidence_text, evidence_source="inline"):
        raise NotImplementedError


def _empty_fields():
    return {name: FieldExtraction(note="not found in evidence")
            for name in FIELD_NAMES}


def _line_of(text, start):
    """The full line containing offset `start` (the source quote)."""
    lo = text.rfind("\n", 0, start) + 1
    hi = text.find("\n", start)
    if hi == -1:
        hi = len(text)
    return text[lo:hi].strip()


# ---------------------------------------------------------------------
# Deterministic provider (stand-in; offline; what the tests exercise)
# ---------------------------------------------------------------------

_CURRENCY_BY_TOKEN = {
    "$": "USD", "USD": "USD",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "₹": "INR", "Rs": "INR", "Rs.": "INR", "INR": "INR",
    "dollars": "USD", "rupees": "INR",
}

_MONEY_RE = re.compile(
    r"(USD|EUR|GBP|INR|Rs\.?|\$|€|£|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(USD|EUR|GBP|INR|dollars|rupees)",
    re.IGNORECASE)
_AMOUNT_LABELS = ("amount", "total", "due", "fee", "price", "charge")
_NET_TERMS = {10, 15, 20, 30, 45, 60, 90}
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*\S)\s*$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


class DeterministicExtractor(EvidenceExtractor):
    """Pattern-based stand-in provider. Deterministic and offline."""

    name = "deterministic (stand-in, no model)"

    def extract(self, evidence_text, evidence_source="inline"):
        text = evidence_text or ""
        fields = _empty_fields()
        self._client(text, fields)
        self._email(text, fields)
        self._title(text, fields)
        self._deliverables(text, fields)
        self._money(text, fields)
        self._due_terms(text, fields)
        draft = MilestoneDraft(evidence_text=text,
                               provider_name=self.name,
                               fields=fields,
                               evidence_source=evidence_source)
        draft.flags = compute_flags(draft)
        return draft

    # -- individual fields ------------------------------------------

    def _client(self, text, fields):
        m = re.search(r"(?im)^\s*client\s*:\s*(.+?)\s*$", text)
        if m:
            fields["client"] = FieldExtraction(
                value=m.group(1), confidence=0.95,
                source_quote=_line_of(text, m.start()),
                note="labelled 'Client:' line")
            return
        m = re.search(
            r"\bfor\s+((?:[A-Z][A-Za-z&]+\s+){0,4}"
            r"(?:Group|Studios|Studio|Media|Productions|Labs|Works|"
            r"Retail|Co\.|Ltd|Inc))\b", text)
        if m:
            fields["client"] = FieldExtraction(
                value=m.group(1).strip(), confidence=0.6,
                source_quote=_line_of(text, m.start()),
                note="inferred from 'for <Name>' phrasing; low "
                     "confidence, human should confirm")

    def _email(self, text, fields):
        m = re.search(r"(?im)^\s*(?:client\s+)?e-?mail\s*:\s*(\S+@\S+)",
                      text)
        if m:
            fields["client_email"] = FieldExtraction(
                value=m.group(1), confidence=0.95,
                source_quote=_line_of(text, m.start()),
                note="labelled email line")
            return
        m = _EMAIL_RE.search(text)
        if m:
            fields["client_email"] = FieldExtraction(
                value=m.group(0), confidence=0.7,
                source_quote=_line_of(text, m.start()),
                note="first email address in the evidence; assumed "
                     "to be the client's")

    def _title(self, text, fields):
        m = re.search(r"(?im)^\s*milestone\s*\d*\s*[:\-–—]\s*(.+?)\s*$",
                      text)
        if m:
            fields["milestone_title"] = FieldExtraction(
                value=m.group(1), confidence=0.9,
                source_quote=_line_of(text, m.start()),
                note="'Milestone' heading line")

    def _deliverables(self, text, fields):
        items, first_pos = [], None
        for match in re.finditer(r"(?m)^.*$", text):
            line = match.group(0)
            b = _BULLET_RE.match(line)
            if b:
                if first_pos is None:
                    first_pos = match.start()
                items.append(b.group(1).strip())
        if items:
            fields["deliverables"] = FieldExtraction(
                value=items, confidence=0.8,
                source_quote=_line_of(text, first_pos),
                note="%d bullet line(s) claimed as deliverables"
                     % len(items))

    def _money(self, text, fields):
        best = None  # (labelled?, FieldExtraction for amount)
        for m in _MONEY_RE.finditer(text):
            token = m.group(1) or m.group(4)
            number = m.group(2) or m.group(3)
            line = _line_of(text, m.start()).lower()
            labelled = any(k in line for k in _AMOUNT_LABELS)
            currency = None
            if token:
                for variant in (token, token.upper(), token.capitalize(),
                                token.lower()):
                    if variant in _CURRENCY_BY_TOKEN:
                        currency = _CURRENCY_BY_TOKEN[variant]
                        break
            try:
                amount = float(number.replace(",", ""))
            except ValueError:
                continue
            conf = 0.95 if labelled else 0.8
            cand = FieldExtraction(
                value=amount, confidence=conf,
                source_quote=_line_of(text, m.start()),
                note=("monetary figure on an amount/total line"
                      if labelled else
                      "first monetary figure in the evidence"))
            if best is None or (labelled and not best[0]):
                best = (labelled, cand, currency, token)
            if labelled:
                break
        if best is not None:
            _, cand, currency, token = best
            fields["amount"] = cand
            if currency:
                fields["currency"] = FieldExtraction(
                    value=currency, confidence=cand.confidence,
                    source_quote=cand.source_quote,
                    note="currency token %r read as %s"
                         % (token, currency))

    def _due_terms(self, text, fields):
        m = re.search(r"(?i)\bnet\s*-?\s*(10|15|20|30|45|60|90)\b", text)
        if m:
            n = int(m.group(1))
            fields["due_terms"] = FieldExtraction(
                value={"term_type": "NET_%d" % n, "raw": m.group(0)},
                confidence=0.9, source_quote=_line_of(text, m.start()),
                note="'Net N' term")
            return
        m = re.search(r"(?i)(?:payable|due)\s+within\s+(\d+)\s+days", text)
        if m:
            n = int(m.group(1))
            value = {"raw": m.group(0)}
            if n in _NET_TERMS:
                value["term_type"] = "NET_%d" % n
            fields["due_terms"] = FieldExtraction(
                value=value, confidence=0.85,
                source_quote=_line_of(text, m.start()),
                note="'within N days' term")
            return
        m = re.search(r"(?i)\bdue\s+on\s+(\d{4}-\d{2}-\d{2})\b", text)
        if m:
            fields["due_terms"] = FieldExtraction(
                value={"term_type": "DUE_ON_DATE_SPECIFIED",
                       "due_date": m.group(1), "raw": m.group(0)},
                confidence=0.85, source_quote=_line_of(text, m.start()),
                note="explicit due date (ISO)")
            return
        if re.search(r"(?i)\bdue on receipt\b", text):
            fields["due_terms"] = FieldExtraction(
                value={"term_type": "DUE_ON_RECEIPT",
                       "raw": "due on receipt"},
                confidence=0.85, source_quote="due on receipt",
                note="stated in evidence")


# ---------------------------------------------------------------------
# LLM provider (real adapter; env-configured; not run live in the
# 2026-11-05 build — see the module docstring)
# ---------------------------------------------------------------------

_LLM_INSTRUCTIONS = """You extract a billable milestone from evidence text.
Return ONLY a JSON object with these keys: client, client_email,
milestone_title, deliverables, amount, currency, due_terms.
Each value is an object {"value": ..., "confidence": 0.0-1.0,
"source_quote": "exact substring of the evidence"} — except
deliverables, whose value is a list of strings.
RULES: never invent a value. If the evidence does not state a
field, its value MUST be null and its source_quote null. Amount is
a number with no commas. Currency is an ISO code (USD, INR, EUR,
GBP). due_terms value is {"term_type": "NET_30"} style (PayPal
term types: DUE_ON_RECEIPT, DUE_ON_DATE_SPECIFIED, NET_10, NET_15,
NET_20, NET_30, NET_45, NET_60, NET_90) plus "due_date" when a
specific date is stated."""


class LLMExtractor(EvidenceExtractor):
    """Adapter for an OpenAI-compatible chat-completions endpoint.

    Configuration comes ONLY from the environment:
      MILESTONEPAY_AI_PROVIDER  provider label (e.g. "openai")
      MILESTONEPAY_AI_MODEL     model id
      MILESTONEPAY_AI_API_KEY   key (memory only; never logged)
      MILESTONEPAY_AI_BASE_URL  optional, default OpenAI base

    `transport(url, headers, payload) -> parsed JSON dict` is
    injectable so tests can exercise this adapter's request shape
    and output validation without any live call.
    """

    name = "llm"

    def __init__(self, env=None, transport=None):
        env = os.environ if env is None else env
        self.provider = env.get("MILESTONEPAY_AI_PROVIDER", "")
        self.model = env.get("MILESTONEPAY_AI_MODEL", "")
        self._api_key = env.get("MILESTONEPAY_AI_API_KEY", "")
        self.base_url = env.get("MILESTONEPAY_AI_BASE_URL",
                                "https://api.openai.com/v1")
        self._transport = transport
        if self.provider or self.model:
            self.name = "llm:%s/%s" % (self.provider or "?",
                                       self.model or "?")

    def _require_config(self):
        missing = [name for name, val in (
            ("MILESTONEPAY_AI_PROVIDER", self.provider),
            ("MILESTONEPAY_AI_MODEL", self.model),
            ("MILESTONEPAY_AI_API_KEY", self._api_key)) if not val]
        if missing:
            raise ProviderNotConfigured(
                "LLM extraction provider is not configured; missing "
                "environment variables: " + ", ".join(missing) +
                ". Set them to enable the LLM provider, or use the "
                "deterministic stand-in provider. (Variable names "
                "only — no key value is read from code or files.)")

    def _call(self, evidence_text):
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _LLM_INSTRUCTIONS},
                {"role": "user", "content": evidence_text},
            ],
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + self._api_key,
                   "Content-Type": "application/json"}
        if self._transport is not None:
            return self._transport(url, headers, payload)
        import requests  # local import: only the live path needs it
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=60)
        resp.raise_for_status()
        return resp.json()

    def extract(self, evidence_text, evidence_source="inline"):
        self._require_config()
        body = self._call(evidence_text or "")
        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ExtractionError(
                "LLM response was not the required JSON shape: %s"
                % type(exc).__name__)
        if not isinstance(parsed, dict):
            raise ExtractionError("LLM response JSON was not an object")
        fields = _empty_fields()
        for name in FIELD_NAMES:
            raw = parsed.get(name)
            if not isinstance(raw, dict):
                continue
            value = raw.get("value")
            if name == "amount" and isinstance(value, str):
                try:
                    value = float(value.replace(",", ""))
                except ValueError:
                    value = None
            quote = raw.get("source_quote")
            conf = raw.get("confidence", 0.0)
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.0
            note = "LLM-extracted"
            # Grounding check: a quote that does not appear in the
            # evidence cannot support the field.
            if value is not None and value != []:
                if not quote or quote not in evidence_text:
                    conf = min(conf, 0.2)
                    note += ("; source quote NOT found in the "
                             "evidence — unverified, confidence "
                             "capped")
            fields[name] = FieldExtraction(
                value=value, confidence=conf, source_quote=quote,
                note=note)
        draft = MilestoneDraft(evidence_text=evidence_text,
                               provider_name=self.name,
                               fields=fields,
                               evidence_source=evidence_source)
        draft.flags = compute_flags(draft)
        for name in FIELD_NAMES:
            fx = fields[name]
            if "unverified" in fx.note:
                draft.flags.append(
                    "LLM field %s carries an unverified source quote."
                    % name)
        return draft
