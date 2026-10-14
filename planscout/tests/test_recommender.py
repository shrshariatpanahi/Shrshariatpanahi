from datetime import date

import pytest

from planscout.catalog import Catalog
from planscout.recommender import current_cost, plan_fit, recommend

from .conftest import TODAY


def test_catalog_loads_and_cross_references_are_valid(catalog):
    assert set(catalog.carriers) == {"summit", "harbor", "nova", "pulse"}
    assert catalog.is_sample


def test_plan_pricing_uses_line_tier_and_autopay(catalog):
    plan = catalog.plan("summit-unlimited-elite")
    assert plan.monthly_cost(1, autopay=False) == 95
    assert plan.monthly_cost(2) == (85 - 10) * 2
    assert plan.monthly_cost(6) == (65 - 10) * 6  # beyond the last tier, the last tier applies


def test_plan_fit_requires_data_and_hotspot_headroom(catalog, make_snapshot):
    heavy = make_snapshot(lines=[{"label": "A", "avg_data_gb": 14, "hotspot_gb": 0}]).lines
    assert not plan_fit(catalog.plan("summit-15gb"), heavy)[0]  # 14 GB * 1.2 > 15 GB
    assert plan_fit(catalog.plan("nova-20gb"), heavy)[0]
    tether = make_snapshot(lines=[{"label": "A", "avg_data_gb": 5, "hotspot_gb": 9}]).lines
    assert not plan_fit(catalog.plan("nova-unlimited"), tether)[0]  # 10 GB hotspot < 10.8
    assert plan_fit(catalog.plan("nova-unlimited-plus"), tether)[0]


def test_light_user_on_premium_plan_is_told_to_downgrade(catalog, make_snapshot):
    snapshot = make_snapshot(lines=[{"label": "Me", "avg_data_gb": 3}])
    report = recommend(snapshot, catalog, today=TODAY)
    assert report.best_option is not None
    assert report.best_option.monthly_plan_cost < report.current.monthly_plan
    assert report.best_option.savings > 0
    # The plan the customer already has is never recommended back to them.
    assert all(o.plan_id != "summit-unlimited-elite" for o in report.plan_options)
    # Options are sorted cheapest first.
    totals = [o.horizon_total for o in report.plan_options]
    assert totals == sorted(totals)


def test_switching_counts_device_payoff_and_forfeited_credits(catalog, make_snapshot):
    device = {
        "model": "Orion S24",
        "financed": True,
        "monthly_installment": 30,
        "installments_remaining": 10,
        "monthly_promo_credit": 10,
    }
    snapshot = make_snapshot(lines=[{"label": "Me", "avg_data_gb": 3, "device": device}])
    current = current_cost(snapshot, 24)
    assert current.device_payoff_balance == 300
    assert current.remaining_device_credits == 100
    assert current.horizon_total == 85 * 24 + 200

    report = recommend(snapshot, catalog, today=TODAY)
    harbor = next(o for o in report.plan_options if o.plan_id == "harbor-essentials")
    # One-time: $35 activation + $300 payoff. The payoff promo reimburses up to the balance owed.
    assert harbor.one_time_costs == 335
    assert harbor.promo_savings == 300
    assert harbor.horizon_total == 60 * 24 + 335 - 300
    assert any("give up $100" in c for c in harbor.caveats)

    same_carrier = next(o for o in report.plan_options if o.plan_id == "summit-15gb")
    assert same_carrier.one_time_costs == 0
    assert same_carrier.horizon_total == 50 * 24 + 200  # keeps paying the phone net of credits


def test_only_best_non_stackable_promo_applies_but_stackable_ones_add_up(catalog, make_snapshot):
    snapshot = make_snapshot(lines=[{"label": "A", "avg_data_gb": 3}, {"label": "B", "avg_data_gb": 3}])
    report = recommend(snapshot, catalog, today=TODAY)
    nova = next(o for o in report.plan_options if o.plan_id == "nova-unlimited")
    # $150/line switch credit (exclusive) + $10/line/mo for 12 months (stackable).
    assert nova.promo_savings == 150 * 2 + 10 * 2 * 12
    assert len(nova.promotions) == 2


def test_expired_promotions_are_ignored(catalog, make_snapshot):
    snapshot = make_snapshot(lines=[{"label": "A", "avg_data_gb": 3}])
    report = recommend(snapshot, catalog, today=TODAY)
    harbor = [o for o in report.plan_options if o.carrier_id == "harbor"]
    assert all("Summer" not in title for o in harbor for title in o.promotions)
    # The same promotion counts while it is still running.
    summer = recommend(snapshot, catalog, today=date(2026, 8, 1))
    assert any("Summer" in t for o in summer.plan_options if o.carrier_id == "harbor" for t in o.promotions)


def test_existing_customer_loyalty_offer_only_on_same_carrier(catalog, make_snapshot):
    on_summit = recommend(make_snapshot(lines=[{"label": "A", "avg_data_gb": 3}]), catalog, today=TODAY)
    start = next(o for o in on_summit.plan_options if o.plan_id == "summit-unlimited-start")
    assert any("Loyalty" in p for p in start.promotions)

    on_nova = recommend(
        make_snapshot("nova", "nova-unlimited", lines=[{"label": "A", "avg_data_gb": 3}]), catalog, today=TODAY
    )
    start = next(o for o in on_nova.plan_options if o.plan_id == "summit-unlimited-start")
    assert not any("Loyalty" in p for p in start.promotions)


def test_already_cheapest_plan_gets_no_recommendation(catalog, make_snapshot):
    snapshot = make_snapshot("pulse", "pulse-5gb", lines=[{"label": "A", "avg_data_gb": 2}])
    report = recommend(snapshot, catalog, today=TODAY)
    assert report.best_option is None
    assert "competitive" in report.verdict


def test_autopay_quick_win(catalog, make_snapshot):
    snapshot = make_snapshot(autopay=False, lines=[{"label": "A", "avg_data_gb": 3}])
    report = recommend(snapshot, catalog, today=TODAY)
    assert report.quick_wins == ["Turn on AutoPay with Summit Wireless to save $10/mo"]


def test_trade_in_deal_requires_an_eligible_phone(catalog, make_snapshot):
    without = make_snapshot(lines=[{"label": "A", "avg_data_gb": 3, "device": {"model": "Vela 8"}}])
    deals = recommend(without, catalog, today=TODAY).device_deals
    assert not any(d.promo_id == "summit-apex16-tradein" for d in deals)

    with_phone = make_snapshot(lines=[{"label": "A", "avg_data_gb": 3, "device": {"model": "Apex Phone 13"}}])
    deals = recommend(with_phone, catalog, today=TODAY).device_deals
    deal = next(d for d in deals if d.promo_id == "summit-apex16-tradein")
    assert deal.trade_in_model == "Apex Phone 13"
    assert deal.effective_device_price == 0
    assert deal.extra_plan_cost == 0  # already on the required plan
    assert deal.same_carrier


def test_device_deal_adds_cost_of_pricier_required_plan(catalog, make_snapshot):
    snapshot = make_snapshot("summit", "summit-15gb", lines=[{"label": "A", "avg_data_gb": 3, "device": {"model": "Apex Phone 13"}}])
    deal = next(d for d in recommend(snapshot, catalog, today=TODAY).device_deals if d.promo_id == "summit-apex16-tradein")
    # Requires Unlimited Elite ($85) instead of Essentials 15GB ($50) for the 36 months of credits.
    assert deal.required_plan_id == "summit-unlimited-elite"
    assert deal.extra_plan_cost == (85 - 50) * 36
    assert deal.net_cost == deal.extra_plan_cost


def test_new_customer_only_device_deals_hidden_from_existing_customers(catalog, make_snapshot):
    snapshot = make_snapshot("harbor", "harbor-max", lines=[{"label": "A", "avg_data_gb": 3, "device": {"model": "Vela 8"}}])
    deals = recommend(snapshot, catalog, today=TODAY).device_deals
    assert not any(d.promo_id == "harbor-orion-s26-switch" for d in deals)
    assert any(d.promo_id == "harbor-apex16-upgrade" for d in deals)


def test_upgrade_status(catalog, make_snapshot):
    lines = [
        {"label": "A", "device": {"model": "Vela 8"}},
        {"label": "B", "device": {"model": "Orion S24", "financed": True, "monthly_installment": 30, "installments_remaining": 2}},
        {"label": "C", "device": {"model": "Apex Phone 15", "financed": True, "monthly_installment": 40, "installments_remaining": 12}},
    ]
    status = recommend(make_snapshot(lines=lines), catalog, today=TODAY).upgrade_status
    assert [s.status for s in status] == ["paid_off", "almost_paid_off", "financed"]
    assert status[2].remaining_balance == 480


def test_unknown_carrier_rejected(catalog, make_snapshot):
    snapshot = make_snapshot().model_copy(update={"carrier_id": "nope"})
    with pytest.raises(ValueError):
        recommend(snapshot, catalog, today=TODAY)


def test_catalog_validation_catches_bad_references(catalog):
    from planscout.catalog import CatalogError
    from planscout.models import Promotion

    bad = Promotion(id="x", carrier_id="nova", type="switcher_credit", title="x", eligible_plan_ids=["harbor-max"])
    with pytest.raises(CatalogError):
        Catalog(
            carriers=catalog.carriers,
            plans=catalog.plans,
            promotions=[bad],
            devices=catalog.devices,
            as_of=catalog.as_of,
        )
