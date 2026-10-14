"""Canada-specific behaviour: provinces, sister brands, sales tax, unlocking, French bills."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from planscout.connectors.bill_parser import parse_bill_text, province_from_postal_code
from planscout.models import AccountSnapshot
from planscout.recommender import recommend

from .conftest import TODAY

FIXTURES = Path(__file__).parent / "fixtures"
LIGHT = [{"label": "A", "avg_data_gb": 3}]


def carriers_in(report):
    return {o.carrier_id for o in report.plan_options} | {d.carrier_id for d in report.device_deals}


def test_only_carriers_serving_the_province_are_recommended(catalog, make_snapshot):
    everywhere = recommend(make_snapshot(lines=LIGHT), catalog, today=TODAY)
    assert {"nova", "pulse"} <= carriers_in(everywhere)

    quebec = recommend(make_snapshot(lines=LIGHT, province="QC"), catalog, today=TODAY)
    assert "nova" not in carriers_in(quebec)  # Nova doesn't sell in Quebec
    assert "pulse" in carriers_in(quebec)

    sask = recommend(make_snapshot(lines=LIGHT, province="SK"), catalog, today=TODAY)
    assert carriers_in(sask) <= {"summit", "harbor"}


def test_sister_brand_switch_excluded_from_switcher_offer(catalog, make_snapshot):
    from_summit = recommend(make_snapshot(lines=LIGHT), catalog, today=TODAY)
    nova = next(o for o in from_summit.plan_options if o.plan_id == "nova-unlimited")
    assert any("$150 per line" in p for p in nova.promotions)

    from_pulse = recommend(make_snapshot("pulse", "pulse-15gb", lines=LIGHT), catalog, today=TODAY)
    nova = next(o for o in from_pulse.plan_options if o.plan_id == "nova-unlimited")
    assert not any("$150 per line" in p for p in nova.promotions)
    assert any("both owned by Nova Group" in c for c in nova.caveats)


def test_sales_tax_rate_is_reported_for_the_province(catalog, make_snapshot):
    report = recommend(make_snapshot(lines=LIGHT, province="ON"), catalog, today=TODAY)
    assert report.province == "ON" and report.sales_tax_rate == 13
    assert any("13% in ON" in a for a in report.assumptions)
    no_province = recommend(make_snapshot(lines=LIGHT), catalog, today=TODAY)
    assert no_province.sales_tax_rate is None
    assert any("Add your province" in a for a in no_province.assumptions)


def test_locked_phone_caveat_mentions_free_unlocking(catalog, make_snapshot):
    lines = [{"label": "A", "avg_data_gb": 3, "device": {"model": "Vela 8", "unlocked": False}}]
    report = recommend(make_snapshot(lines=lines), catalog, today=TODAY)
    harbor = next(o for o in report.plan_options if o.carrier_id == "harbor")
    assert any("must unlock them for free" in c for c in harbor.caveats)


def test_limited_time_offers_are_flagged(catalog, make_snapshot):
    plan = catalog.plan("nova-20gb")
    original = plan.offer_note
    try:
        plan.offer_note = "Bonus data for new activations"
        report = recommend(make_snapshot(lines=LIGHT), catalog, today=TODAY)
        option = next(o for o in report.plan_options if o.plan_id == "nova-20gb")
        assert any(c.startswith("Limited-time pricing: Bonus data") for c in option.caveats)
    finally:
        plan.offer_note = original


def test_unknown_province_rejected():
    with pytest.raises(ValidationError):
        AccountSnapshot(carrier_id="summit", monthly_plan_cost=50, province="ZZ", lines=LIGHT)
    assert AccountSnapshot(carrier_id="summit", monthly_plan_cost=50, province="qc", lines=LIGHT).province == "QC"


@pytest.mark.parametrize(
    "text, province",
    [
        ("Toronto ON M5V 2T6", "ON"),
        ("Montréal (Québec) H3B 1K9", "QC"),
        ("Halifax NS B3H 4R2", "NS"),
        ("Iqaluit NU X0A 0H0", "NU"),
        ("Yellowknife NT X1A 2L9", "NT"),
        ("Vancouver BC V6B1A1", "BC"),
        ("no address here", None),
    ],
)
def test_province_from_postal_code(text, province):
    assert province_from_postal_code(text) == province


def test_parses_french_bill(catalog):
    text = (FIXTURES / "sample_bill_fr.txt").read_text(encoding="utf-8")
    result = parse_bill_text(text, "pulse", catalog)
    draft = result.draft
    assert draft["province"] == "QC"
    assert draft["plan_name"] == "Forfait Max"
    assert draft["autopay"] is True
    assert draft["monthly_plan_cost"] == 100.0  # 110,00 $ - 10,00 $ pre-authorized payment discount
    assert [line["label"] for line in draft["lines"]] == ["•••0123", "•••0177"]
    first = draft["lines"][0]
    assert first["avg_data_gb"] == 24.6 and first["hotspot_gb"] == 3.2
    assert first["device"]["model"] == "iPhone 16"
    assert first["device"]["installments_remaining"] == 15
    assert first["device"]["monthly_installment"] == 42.5
    assert first["device"]["monthly_promo_credit"] == 10.0
    assert draft["lines"][1]["device"] is None
    assert "H3B" not in str(draft)  # only the province is kept, not the postal code
    AccountSnapshot.model_validate(draft)


def test_english_bill_province(catalog):
    text = (FIXTURES / "sample_bill.txt").read_text()
    assert parse_bill_text(text, "harbor", catalog).draft["province"] == "ON"
    no_address = text.replace("Pat Lee, 100 King St W, Toronto ON M5X 1A9\n", "")
    result = parse_bill_text(no_address, "harbor", catalog)
    assert result.draft["province"] is None
    assert any("province" in w for w in result.warnings)


def test_api_exposes_provinces_and_uses_them(signed_in):
    catalog = signed_in.get("/api/catalog").json()
    assert catalog["country"] == "CA" and catalog["provinces"]["QC"] == "Quebec"
    assert catalog["sales_tax"]["ON"] == 13
    assert next(c for c in catalog["carriers"] if c["id"] == "nova")["provinces"] == ["ON", "BC", "AB", "MB"]

    account = {"plan_name": "Unlimited Elite", "monthly_plan_cost": 85, "province": "QC", "lines": LIGHT}
    connection_id = signed_in.post("/api/connect/summit/account", json=account).json()["id"]
    report = signed_in.get(f"/api/connections/{connection_id}/recommendations").json()
    assert report["province"] == "QC" and report["sales_tax_rate"] == 14.975
    assert all(o["carrier_id"] != "nova" for o in report["plan_options"])

    bad = signed_in.post("/api/connect/summit/account", json={**account, "province": "XX"})
    assert bad.status_code == 422


def test_sandbox_demo_customer_gets_a_province(signed_in):
    from .test_api import sandbox_sign_in

    response, _ = sandbox_sign_in(signed_in, carrier="pulse", persona="couple")  # couple lives in BC
    connection_id = int(response.headers["location"].rsplit("=", 1)[1])
    account = signed_in.get(f"/api/connections/{connection_id}").json()["account"]
    assert account["province"] == "QC"  # Pulse doesn't serve BC, so the demo customer is placed where it does


def test_trade_in_deal_without_model_list_is_shown_with_terms(catalog, make_snapshot):
    promo = next(p for p in catalog.promotions if p.id == "summit-apex16-tradein")
    saved = (promo.trade_in_models, promo.description)
    try:
        promo.trade_in_models, promo.description = [], "Trade in an Apex Phone 12 or newer in good condition"
        lines = [{"label": "A", "avg_data_gb": 3, "device": {"model": "Vela 8"}}]
        deal = next(d for d in recommend(make_snapshot(lines=lines), catalog, today=TODAY).device_deals
                    if d.promo_id == "summit-apex16-tradein")
        assert deal.trade_in_line is None
        assert "Trade in an Apex Phone 12 or newer in good condition" in deal.notes
        assert any("eligible phone" in n for n in deal.notes)
    finally:
        promo.trade_in_models, promo.description = saved
