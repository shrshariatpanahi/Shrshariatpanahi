"""Integrity checks for the real Canadian catalog in planscout/data/.

These don't pin prices (they change monthly); they make sure every edit keeps the catalog complete,
sourced, current and usable by the engine.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from planscout.catalog import Catalog
from planscout.config import Settings
from planscout.connectors.sandbox import SandboxCarrier
from planscout.main import create_app
from planscout.models import PROVINCES
from planscout.recommender import recommend

BRANDS = {
    "rogers", "fido", "chatr",
    "bell", "virgin-plus", "lucky-mobile",
    "telus", "koodo", "public-mobile",
    "freedom-mobile", "videotron", "fizz",
    "eastlink", "sasktel",
}
SISTER_BRANDS = [
    {"rogers", "fido", "chatr"},
    {"bell", "virgin-plus", "lucky-mobile"},
    {"telus", "koodo", "public-mobile"},
    {"freedom-mobile", "videotron", "fizz"},
]


# TEMPORARY: planscout/data still holds the sample catalog until the researched Canadian data is installed.
pytestmark = pytest.mark.skipif(Catalog.load().is_sample, reason="Canadian catalog not installed yet")


@pytest.fixture(scope="module")
def ca() -> Catalog:
    return Catalog.load()


def test_metadata(ca):
    assert ca.country == "CA"
    assert not ca.is_sample
    assert ca.free_unlocking
    assert set(ca.sales_tax) == set(PROVINCES)
    assert all(5 <= rate < 20 for rate in ca.sales_tax.values())


def test_all_brands_present_with_plans(ca):
    assert BRANDS <= set(ca.carriers)
    for carrier_id in BRANDS:
        assert ca.plans_for(carrier_id), f"{carrier_id} has no plans"


def test_sister_brands_share_a_parent(ca):
    for group in SISTER_BRANDS:
        assert len({ca.carriers[c].parent for c in group}) == 1, group
    parents = [ca.carriers[next(iter(g))].parent for g in SISTER_BRANDS]
    assert len(set(parents)) == len(parents)


def test_regional_carriers_are_limited_to_their_provinces(ca):
    assert ca.carriers["sasktel"].provinces == ["SK"]
    assert ca.carriers["eastlink"].provinces and "SK" not in ca.carriers["eastlink"].provinces
    assert "QC" in ca.carriers["videotron"].provinces
    for carrier_id in ("rogers", "bell", "telus"):
        assert ca.carriers[carrier_id].serves("ON") and ca.carriers[carrier_id].serves("QC")


def test_every_plan_is_sourced_and_sane(ca):
    for plan in ca.plans:
        assert plan.source_urls and all(u.startswith("http") for u in plan.source_urls), plan.id
        assert plan.verified_on is not None and plan.verified_on <= date.today(), plan.id
        for lines, price in plan.price_per_line.items():
            assert 1 <= lines <= 10 and 5 <= price <= 250, (plan.id, lines, price)
        assert plan.autopay_discount_per_line < plan.price_per_line[1], plan.id
        assert plan.data_gb is None or 0 < plan.data_gb <= 2000, plan.id
        assert plan.offer_ends is None or plan.offer_ends >= ca.as_of, plan.id


def test_every_promotion_is_sourced_and_current(ca):
    for promo in ca.promotions:
        assert promo.source_urls and all(u.startswith("http") for u in promo.source_urls), promo.id
        assert promo.valid_until is None or promo.valid_until >= ca.as_of, promo.id
        assert promo.new_customers or promo.existing_customers, promo.id
        if promo.type == "device_discount":
            retail = promo.device_retail_price or ca.devices[promo.device_model].retail_price
            assert 0 < promo.device_discount <= retail, promo.id
            assert 0 <= promo.credit_months <= 36, promo.id  # CRTC Wireless Code caps terms at 24 months; allow slack
        if promo.type == "switcher_credit":
            assert 0 < promo.value_per_line <= 1500, promo.id
        if promo.type == "plan_discount":
            assert 0 < promo.monthly_discount_per_line <= 100, promo.id


def test_demo_customers_work_with_every_carrier(ca):
    sandbox = SandboxCarrier.load(ca)
    for carrier_id, carrier in ca.carriers.items():
        for persona_id in sandbox.personas:
            snapshot = sandbox.snapshot_for(carrier_id, persona_id)
            assert carrier.serves(snapshot.province), (carrier_id, persona_id)
            report = recommend(snapshot, ca, today=ca.as_of)
            assert report.sales_tax_rate is not None
            for option in report.plan_options:
                assert ca.carriers[option.carrier_id].serves(snapshot.province)


def test_app_starts_with_the_canadian_catalog(tmp_path):
    with TestClient(create_app(Settings(db_path=str(tmp_path / "ca.db")))) as client:
        catalog = client.get("/api/catalog").json()
        assert catalog["country"] == "CA" and catalog["is_sample"] is False
        assert len(catalog["provinces"]) == 13
        assert BRANDS <= {c["id"] for c in catalog["carriers"]}
