from pathlib import Path

import pytest

from planscout.connectors.bill_parser import BillParseError, extract_text, parse_bill_text
from planscout.models import AccountSnapshot

SAMPLE = (Path(__file__).parent / "fixtures" / "sample_bill.txt").read_text()


def test_parses_sample_bill(catalog):
    result = parse_bill_text(SAMPLE, "harbor", catalog)
    draft = result.draft
    assert draft["plan_name"] == "Harbor Max"
    assert draft["plan_id"] == "harbor-max"
    assert draft["autopay"] is True
    assert draft["monthly_plan_cost"] == 180.0  # $195 plan charges - $15 AutoPay

    lines = draft["lines"]
    assert [line["label"] for line in lines] == ["•••0142", "•••0187", "•••0110"]
    assert [line["avg_data_gb"] for line in lines] == [18.4, 6.2, 1.1]
    assert lines[0]["hotspot_gb"] == 2.1

    first = lines[0]["device"]
    assert first["model"] == "Apex Phone 15"
    assert first["monthly_installment"] == 37.5
    assert first["installments_remaining"] == 17
    assert first["monthly_promo_credit"] == 12.5
    assert lines[1]["device"]["model"] == "Orion S24"
    assert lines[1]["device"]["installments_remaining"] == 4
    assert lines[1]["device"]["monthly_promo_credit"] == 0
    assert lines[2]["device"] is None

    # The customer-care number in the header is not mistaken for a line.
    assert len(lines) == 3
    # The draft is a valid account once confirmed.
    AccountSnapshot.model_validate(draft)


def test_full_phone_numbers_are_not_kept(catalog):
    draft = parse_bill_text(SAMPLE, "harbor", catalog).draft
    assert "555-0142" not in str(draft) and "4165550142" not in str(draft)


def test_missing_fields_produce_warnings_and_placeholders(catalog):
    result = parse_bill_text("Total amount due $80.00\nYour plan: Mystery Plan (2 lines)", "nova", catalog)
    assert result.draft["plan_id"] is None
    assert result.draft["monthly_plan_cost"] == 80.0
    assert len(result.draft["lines"]) == 2
    assert any("estimated from the total" in w for w in result.warnings)
    assert any("only read 0" in w for w in result.warnings)


def test_extract_text_rejects_non_text_and_large_files():
    assert extract_text("bill.txt", b"hello") == "hello"
    with pytest.raises(BillParseError):
        extract_text("bill.bin", b"\xff\xfe\xfa")
    with pytest.raises(BillParseError):
        extract_text("bill.txt", b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(BillParseError):
        extract_text("bill.pdf", b"%PDF-1.7 not really a pdf")


def test_extract_text_reads_pdf():
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    assert extract_text("bill.pdf", buffer.getvalue()) == ""
