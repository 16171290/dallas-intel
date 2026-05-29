"""Context-aware extraction fallback for watermark-garbled NOF pages.

Background: docs/OCR_WATERMARK_REMOVAL_INVESTIGATION.md (2026-05-29 addendum).
Tesseract + regex cannot recover grantor/address text where the
publicsearch.us "Unofficial Copy" watermark crosses bold text (e.g.
"NICOLE" -> "COLE", the Liable record's real grantors). A vision LLM reads
the document with language/layout context and recovers those fields.

This module is a *fallback*, not a replacement: it is only invoked when the
regex extractor leaves grantor/address null, it is gated behind
FORECLOSURE_LLM_OCR_ENABLED, and its output passes through the same
validators the regex path uses before anything is written to a record.

Standalone test harness: scripts/llm_ocr_probe.py (run it before wiring this
into the pipeline).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Model default per the Claude-API guidance: opus-4-8 unless explicitly
# overridden. LLM_OCR_MODEL lets an operator pick a cheaper model.
MODEL = os.getenv("LLM_OCR_MODEL", "claude-opus-4-8")
MAX_TOKENS = 4096
# Below this self-reported confidence we abstain (leave the field null)
# rather than risk writing a hallucinated value into a record.
CONFIDENCE_FLOOR = float(os.getenv("LLM_OCR_CONFIDENCE_FLOOR", "0.5"))
# Only the first pages carry grantor + property address; cap to bound cost.
MAX_PAGES = int(os.getenv("LLM_OCR_MAX_PAGES", "2"))
CACHE_DIR = Path(os.getenv("LLM_OCR_CACHE_DIR", "data/cache/llm_ocr"))

_SYSTEM = (
    "You transcribe Texas foreclosure 'Notice of Substitute Trustee Sale' "
    "documents. The page images carry a diagonal 'Unofficial Copy' watermark "
    "that overlaps the text. Read the underlying document, not the watermark. "
    "Extract ONLY text you can actually read on the page. If a field is "
    "illegible or absent, return null for it. NEVER invent, complete, or guess "
    "a name or address. The grantor is the borrower/trustor whose property is "
    "being foreclosed (labelled 'Grantor(s)', 'Trustor', 'Mortgagor', or named "
    "in a 'WHEREAS, on <date>, <NAME>, ...' clause) -- it is a person or "
    "company name, never a legal phrase like 'LIABLE' or 'BORROWER'. "
    "confidence is your own 0-1 estimate that the extracted values are correct."
)

_PROMPT = (
    "Extract the grantor (borrower) name, the property street address, and the "
    "foreclosure sale date from this notice. Return null for anything you "
    "cannot read with confidence."
)


@dataclass
class LLMFields:
    grantor: Optional[str] = None
    property_address: Optional[str] = None
    sale_date: Optional[str] = None
    confidence: float = 0.0
    from_cache: bool = False


def enabled() -> bool:
    """Gate for the LLM-OCR fallback. Defaults to False (inert)."""
    return os.getenv("FORECLOSURE_LLM_OCR_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _schema():
    """Pydantic model for structured output. Defined lazily so importing this
    module does not require pydantic/anthropic until the path is actually used."""
    from pydantic import BaseModel

    class ForeclosureExtraction(BaseModel):
        grantor: Optional[str]
        property_address: Optional[str]
        sale_date: Optional[str]
        confidence: float

    return ForeclosureExtraction


def _cache_path(record_id: str) -> Path:
    return CACHE_DIR / f"{record_id}.json"


def _cache_get(record_id: str) -> Optional[LLMFields]:
    p = _cache_path(record_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return LLMFields(
            grantor=d.get("grantor"),
            property_address=d.get("property_address"),
            sale_date=d.get("sale_date"),
            confidence=d.get("confidence", 0.0),
            from_cache=True,
        )
    except Exception as exc:  # corrupt cache file -> ignore, re-extract
        logger.warning("llm_ocr cache read failed for %s: %s", record_id, exc)
        return None


def _cache_put(record_id: str, fields: LLMFields) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(record_id).write_text(json.dumps({
            "grantor": fields.grantor,
            "property_address": fields.property_address,
            "sale_date": fields.sale_date,
            "confidence": fields.confidence,
        }, indent=2))
    except Exception as exc:
        logger.warning("llm_ocr cache write failed for %s: %s", record_id, exc)


def _client():
    """Construct an Anthropic client, or None if the SDK/key is unavailable.

    Mirrors the graceful-degradation pattern used for a missing Tesseract
    binary in foreclosure_ocr.enrich_foreclosure_records: log and skip rather
    than crash the pipeline.
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("llm_ocr: anthropic SDK not installed; skipping.")
        return None
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        logger.warning("llm_ocr: ANTHROPIC_API_KEY not set; skipping.")
        return None
    try:
        return anthropic.Anthropic()
    except Exception as exc:
        logger.warning("llm_ocr: client init failed: %s", exc)
        return None


def extract(record_id: str, page_pngs: list[bytes]) -> Optional[LLMFields]:
    """Run the vision-LLM extraction over a record's page PNGs.

    Returns None when disabled, when the SDK/key is unavailable, or on error
    (the caller treats None as "no fallback available" and proceeds). A cached
    result is returned without an API call.
    """
    if not enabled():
        return None

    cached = _cache_get(record_id)
    if cached is not None:
        return cached

    client = _client()
    if client is None:
        return None

    content: list[dict] = []
    for png in page_pngs[:MAX_PAGES]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": _PROMPT})

    try:
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
            output_format=_schema(),
        )
    except Exception as exc:
        logger.warning("llm_ocr: API call failed for %s: %s", record_id, exc)
        return None

    parsed = resp.parsed_output
    if parsed is None:
        logger.warning("llm_ocr: no parsed output for %s (stop=%s)",
                       record_id, getattr(resp, "stop_reason", "?"))
        return None

    fields = LLMFields(
        grantor=parsed.grantor,
        property_address=parsed.property_address,
        sale_date=parsed.sale_date,
        confidence=float(parsed.confidence or 0.0),
    )
    _cache_put(record_id, fields)
    return fields
