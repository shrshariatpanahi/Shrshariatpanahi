"""Turns an uploaded carrier bill (PDF or text) into a *draft* account for the customer to review.

Works with any carrier today, with no partnership needed. Bill layouts vary, so parsing is
best-effort: the result is always shown back to the customer to confirm before it's analysed,
and the uploaded file itself is never stored.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from pypdf import PdfReader

from ..catalog import Catalog

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 40

_AMOUNT = r"\$?\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})"
PHONE_RE = re.compile(r"(?<!\d)\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
SUPPORT_NUMBER_RE = re.compile(r"customer (?:service|care)|call us|questions\?|1[\s.-]?8\d\d", re.I)
PLAN_NAME_RE = re.compile(r"\b(?:your\s+|rate\s+)?plan\s*:\s*(?P<name>[^\n]+)", re.I)
LINE_COUNT_RE = re.compile(r"\((\d{1,2})\s+lines?\)", re.I)
PLAN_CHARGES_RE = re.compile(r"(?:plan|monthly service|service plan)\s+charges?\b[^\n\d$]*" + _AMOUNT, re.I)
AUTOPAY_RE = re.compile(r"auto\s?pay\s+(?:discount|savings|credit)[^\n\d$]*-?\s*" + _AMOUNT, re.I)
TOTAL_RE = re.compile(r"(?:total\s+(?:amount\s+)?due|amount\s+due|total\s+balance)[^\n\d$]*" + _AMOUNT, re.I)
DATA_RE = re.compile(r"data(?:\s+used|\s+usage)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*GB", re.I)
HOTSPOT_RE = re.compile(r"hotspot(?:\s+used|\s+usage)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*GB", re.I)
INSTALLMENT_RE = re.compile(
    r"(?:device|phone|equipment)\s+(?:payment|installment|financing)\s*[-–:]?\s*(?P<model>[^\n$]*?)\s*"
    r"(?:installment|payment|pmt)?\.?\s*(?P<paid>\d{1,2})\s*(?:of|/)\s*(?P<total>\d{1,2})[^\n\d$]*"
    + _AMOUNT,
    re.I,
)
CREDIT_RE = re.compile(r"(?:promo(?:tional)?|device|trade-?in)\s+credit[^\n\d$]*-\s*" + _AMOUNT, re.I)


class BillParseError(ValueError):
    pass


@dataclass
class BillParseResult:
    draft: dict
    warnings: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)


def _amount(text: str) -> float:
    return float(text.replace(",", ""))


def extract_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise BillParseError("File is larger than 10 MB")
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise BillParseError("This PDF is password-protected; please upload an unlocked copy")
            pages = reader.pages[:MAX_PAGES]
            return "\n".join(page.extract_text() or "" for page in pages)
        except BillParseError:
            raise
        except Exception as exc:  # pypdf raises many exception types on malformed files
            raise BillParseError("Couldn't read this PDF") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BillParseError("Please upload a PDF or plain-text bill") from exc


def _split_line_blocks(text: str) -> list[tuple[str, str]]:
    """Group bill text into (last4, block) chunks, one per phone number that heads a section."""
    blocks: list[tuple[str, list[str]]] = []
    for row in text.splitlines():
        match = PHONE_RE.search(row)
        if match and not SUPPORT_NUMBER_RE.search(row):
            blocks.append((match.group(3), [row]))
        elif blocks:
            blocks[-1][1].append(row)
    return [(last4, "\n".join(rows)) for last4, rows in blocks]


def _clean_model(raw: str) -> str:
    model = re.sub(r"\s*[-–:]\s*$", "", raw.strip())
    return re.sub(r"\s+(?:installment|payment|pmt)\.?$", "", model, flags=re.I).strip()


def _parse_line(last4: str, block: str) -> dict | None:
    data = DATA_RE.search(block)
    hotspot = HOTSPOT_RE.search(block)
    installment = INSTALLMENT_RE.search(block)
    if not (data or installment):
        return None  # a phone number that isn't a service line (e.g. a contact number)
    line: dict = {
        "label": f"•••{last4}",
        "avg_data_gb": float(data.group(1)) if data else 0.0,
        "hotspot_gb": float(hotspot.group(1)) if hotspot else 0.0,
        "device": None,
    }
    if installment:
        paid, total = int(installment.group("paid")), int(installment.group("total"))
        credit = CREDIT_RE.search(block)
        monthly = _amount(installment.group(4))
        line["device"] = {
            "model": _clean_model(installment.group("model")),
            "financed": True,
            "monthly_installment": monthly,
            "installments_remaining": max(total - paid, 0),
            "monthly_promo_credit": min(_amount(credit.group(1)), monthly) if credit else 0.0,
            "unlocked": None,
        }
    return line


def parse_bill_text(text: str, carrier_id: str, catalog: Catalog) -> BillParseResult:
    result = BillParseResult(draft={"carrier_id": carrier_id, "source": "bill_upload", "autopay": False})
    draft = result.draft

    plan_match = PLAN_NAME_RE.search(text)
    if plan_match:
        raw_name = plan_match.group("name").strip()
        draft["plan_name"] = LINE_COUNT_RE.sub("", raw_name).strip()[:80]
        result.found.append("plan name")
        plan = catalog.match_plan(carrier_id, raw_name)
        draft["plan_id"] = plan.id if plan else None
    else:
        draft["plan_name"], draft["plan_id"] = "", None
        result.warnings.append("Couldn't find your plan name")

    autopay = AUTOPAY_RE.search(text)
    autopay_discount = _amount(autopay.group(1)) if autopay else 0.0
    draft["autopay"] = autopay is not None

    lines = [line for last4, block in _split_line_blocks(text) if (line := _parse_line(last4, block))]
    if lines:
        result.found.append(f"{len(lines)} line{'s' if len(lines) > 1 else ''}")
    count = LINE_COUNT_RE.search(text)
    expected = int(count.group(1)) if count else 0
    if expected > len(lines):
        result.warnings.append(f"Bill mentions {expected} lines but we only read {len(lines)}; please add the rest")
        lines += [
            {"label": f"Line {i + 1}", "avg_data_gb": 0.0, "hotspot_gb": 0.0, "device": None}
            for i in range(len(lines), expected)
        ]
    if not lines:
        result.warnings.append("Couldn't find individual lines; please add them")
        lines = [{"label": "Line 1", "avg_data_gb": 0.0, "hotspot_gb": 0.0, "device": None}]
    draft["lines"] = lines
    if any(line["avg_data_gb"] == 0 for line in lines):
        result.warnings.append("Some lines have no data usage; one bill is a snapshot, so adjust to your typical month")

    plan_charges = PLAN_CHARGES_RE.search(text)
    total = TOTAL_RE.search(text)
    if plan_charges:
        draft["monthly_plan_cost"] = round(_amount(plan_charges.group(1)) - autopay_discount, 2)
        result.found.append("plan charges")
    elif total:
        devices = sum(
            line["device"]["monthly_installment"] - line["device"]["monthly_promo_credit"]
            for line in lines
            if line["device"]
        )
        draft["monthly_plan_cost"] = round(max(_amount(total.group(1)) - devices, 0), 2)
        result.warnings.append("Plan cost was estimated from the total due and may include taxes and fees")
    else:
        draft["monthly_plan_cost"] = 0.0
        result.warnings.append("Couldn't find what you pay for your plan")

    return result
