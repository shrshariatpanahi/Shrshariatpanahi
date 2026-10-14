"""Pricing engine: compares a customer's current account against every plan and promotion.

All comparisons use the same basis: the cost of keeping service for `horizon_months` *plus*
finishing every device payment on the account, so that both "stay" and "switch" end with
the customer owning their current phones outright.
"""

from __future__ import annotations

from datetime import date

from .catalog import Catalog
from .models import (
    AccountSnapshot,
    CurrentCost,
    DeviceDeal,
    LineOnAccount,
    LineUpgradeStatus,
    Plan,
    PlanOption,
    Promotion,
    RecommendationReport,
)

DATA_HEADROOM = 1.2  # recommend plans with 20% more data than the customer's average
MIN_MONTHLY_SAVINGS = 5.0  # below this we tell the customer their plan is already competitive
ALMOST_PAID_OFF_INSTALLMENTS = 3
MAX_DEVICE_DEALS = 8


def _money(value: float) -> str:
    return f"${value:,.2f}".replace(".00", "")


def current_cost(snapshot: AccountSnapshot, horizon_months: int) -> CurrentCost:
    devices = [line.device for line in snapshot.lines if line.device and line.device.financed]
    monthly_devices = sum(
        d.monthly_installment - d.monthly_promo_credit for d in devices if d.installments_remaining > 0
    )
    return CurrentCost(
        monthly_plan=round(snapshot.monthly_plan_cost, 2),
        monthly_devices=round(monthly_devices, 2),
        monthly_total=round(snapshot.monthly_plan_cost + monthly_devices, 2),
        device_payoff_balance=round(sum(d.remaining_balance for d in devices), 2),
        remaining_device_credits=round(sum(d.remaining_credits for d in devices), 2),
        horizon_total=round(
            snapshot.monthly_plan_cost * horizon_months + sum(d.remaining_net_cost for d in devices), 2
        ),
    )


def plan_fit(plan: Plan, lines: list[LineOnAccount]) -> tuple[bool, list[str]]:
    """Return whether the plan covers every line's usage, plus caveats worth showing."""
    caveats: list[str] = []
    for line in lines:
        need = line.avg_data_gb * DATA_HEADROOM
        if plan.data_gb is not None and plan.data_gb < need:
            return False, [f"{line.label} uses about {line.avg_data_gb:g} GB; plan includes {plan.data_gb:g} GB"]
        if line.hotspot_gb > 0 and plan.hotspot_gb is not None and plan.hotspot_gb < line.hotspot_gb * DATA_HEADROOM:
            return False, [f"{line.label} uses about {line.hotspot_gb:g} GB of hotspot; plan includes {plan.hotspot_gb:g} GB"]
        if plan.premium_data_gb is not None and line.avg_data_gb > plan.premium_data_gb:
            caveats.append(
                f"{line.label} uses more than the {plan.premium_data_gb:g} GB of priority data; speeds may slow in busy areas"
            )
    return True, caveats


def _promo_value(promo: Promotion, snapshot: AccountSnapshot, horizon_months: int) -> float:
    lines = len(snapshot.lines)
    if promo.type == "switcher_payoff":
        return sum(
            min(promo.value_per_line, line.device.remaining_balance)
            for line in snapshot.lines
            if line.device and line.device.financed
        )
    if promo.type == "switcher_credit":
        return promo.value_per_line * lines
    if promo.type == "plan_discount":
        months = min(promo.duration_months or horizon_months, horizon_months)
        return promo.monthly_discount_per_line * lines * months
    return 0.0


def _choose_promotions(
    promos: list[Promotion], snapshot: AccountSnapshot, horizon_months: int
) -> list[tuple[Promotion, float]]:
    """All stackable promotions plus the single most valuable non-stackable one."""
    valued = [(p, round(_promo_value(p, snapshot, horizon_months), 2)) for p in promos]
    valued = [(p, v) for p, v in valued if v > 0]
    chosen = [(p, v) for p, v in valued if p.stackable]
    exclusive = [(p, v) for p, v in valued if not p.stackable]
    if exclusive:
        chosen.append(max(exclusive, key=lambda pv: pv[1]))
    return chosen


def _price_option(
    plan: Plan,
    snapshot: AccountSnapshot,
    catalog: Catalog,
    current: CurrentCost,
    horizon_months: int,
    today: date,
    fit_caveats: list[str],
) -> PlanOption:
    carrier = catalog.carriers[plan.carrier_id]
    current_carrier = catalog.carriers[snapshot.carrier_id]
    same_carrier = plan.carrier_id == snapshot.carrier_id
    lines = len(snapshot.lines)
    monthly = plan.monthly_cost(lines, autopay=True)

    reasons = [f"{_money(monthly)}/mo for {lines} line{'s' if lines > 1 else ''} vs your {_money(current.monthly_plan)}/mo"]
    if plan.is_unlimited:
        reasons.append("Unlimited data")
    else:
        heaviest = max(line.avg_data_gb for line in snapshot.lines)
        reasons.append(f"{plan.data_gb:g} GB per line covers your heaviest line (~{heaviest:g} GB/mo)")
    caveats = list(fit_caveats)

    current_plan = catalog.plan(snapshot.plan_id)
    if current_plan:
        missing = [f for f in current_plan.features if f not in plan.features]
        if missing:
            caveats.append("Doesn't include: " + ", ".join(missing))

    promos = [
        p
        for p in catalog.active_promotions(today)
        if p.carrier_id == plan.carrier_id
        and p.type != "device_discount"
        and p.available_to(existing_customer=same_carrier)
        and p.applies_to_plan(plan.id)
    ]
    chosen = _choose_promotions(promos, snapshot, horizon_months)
    promo_savings = round(sum(v for _, v in chosen), 2)

    if same_carrier:
        one_time = 0.0
        device_cost = current.horizon_total - current.monthly_plan * horizon_months
        if current.remaining_device_credits > 0:
            caveats.append("Confirm your device promotion credits continue on this plan; some require a specific plan")
    else:
        activation = carrier.activation_fee_per_line * lines
        one_time = activation + current.device_payoff_balance
        device_cost = 0.0
        if activation:
            caveats.append(f"Includes {_money(activation)} in activation fees")
        if current.device_payoff_balance > 0:
            caveats.append(
                f"Leaving {current_carrier.name} makes your remaining device balance of "
                f"{_money(current.device_payoff_balance)} due"
            )
        if current.remaining_device_credits > 0:
            caveats.append(f"You'd give up {_money(current.remaining_device_credits)} in remaining device credits")
        if any(line.device and line.device.unlocked is False for line in snapshot.lines):
            caveats.append(f"Some phones are locked; ask {current_carrier.name} to unlock them before you switch")
        caveats.append(f"Check that your phones work on {carrier.name}'s network and coverage where you live")
        if carrier.type == "prepaid":
            caveats.append("Prepaid: you pay upfront each month and phones can't be financed")

    horizon_total = round(monthly * horizon_months + one_time + device_cost - promo_savings, 2)
    savings = round(current.horizon_total - horizon_total, 2)
    return PlanOption(
        carrier_id=carrier.id,
        carrier_name=carrier.name,
        plan_id=plan.id,
        plan_name=plan.name,
        same_carrier=same_carrier,
        monthly_plan_cost=monthly,
        one_time_costs=round(one_time, 2),
        promo_savings=promo_savings,
        promotions=[p.title for p, _ in chosen],
        horizon_total=horizon_total,
        savings=savings,
        avg_monthly_savings=round(savings / horizon_months, 2),
        reasons=reasons,
        caveats=caveats,
    )


def _device_deals(snapshot: AccountSnapshot, catalog: Catalog, today: date) -> list[DeviceDeal]:
    lines = len(snapshot.lines)
    current_per_line = snapshot.monthly_plan_cost / lines
    deals: list[DeviceDeal] = []

    for promo in catalog.active_promotions(today):
        if promo.type != "device_discount" or not promo.device_model:
            continue
        same_carrier = promo.carrier_id == snapshot.carrier_id
        if not promo.available_to(existing_customer=same_carrier):
            continue
        device = catalog.devices[promo.device_model]
        carrier = catalog.carriers[promo.carrier_id]
        notes: list[str] = []

        trade_in = None
        if promo.requires_trade_in:
            candidates = [
                line for line in snapshot.lines if line.device and line.device.model in promo.trade_in_models
            ]
            if not candidates:
                continue
            trade_in = min(candidates, key=lambda line: line.device.remaining_balance)
            notes.append(f"Trade in the {trade_in.device.model} on {trade_in.label}")
            if trade_in.device.remaining_balance > 0:
                notes.append(
                    f"That phone still has {_money(trade_in.device.remaining_balance)} owing, which must be paid off first"
                )

        extra = 0.0
        keeps_current_plan = same_carrier and (
            promo.applies_to_plan(snapshot.plan_id) if snapshot.plan_id else not promo.eligible_plan_ids
        )
        if keeps_current_plan:
            required_id, required_name = snapshot.plan_id, snapshot.plan_name or "your current plan"
            notes.append("Works with your current plan")
        else:
            eligible = [p for p in catalog.plans_for(promo.carrier_id) if promo.applies_to_plan(p.id)]
            fitting = [p for p in eligible if plan_fit(p, snapshot.lines)[0]]
            pool = fitting or eligible
            if not pool:
                continue
            required = min(pool, key=lambda p: p.monthly_cost(lines))
            required_id, required_name = required.id, required.name
            if not fitting:
                notes.append(f"{required.name} may not cover your current data usage")
            delta = required.monthly_cost(lines) / lines - current_per_line
            if delta > 0 and promo.credit_months:
                extra = round(delta * promo.credit_months, 2)
                notes.append(
                    f"Requires {required.name}, about {_money(delta)}/line/mo more than you pay now "
                    f"({_money(extra)} over {promo.credit_months} months)"
                )
            else:
                notes.append(f"Requires the {required.name} plan")

        if promo.credit_months:
            notes.append(
                f"Discount is paid as bill credits over {promo.credit_months} months; leaving early forfeits the rest"
            )
        else:
            notes.append("Instant discount at purchase")
        if not same_carrier:
            notes.append(f"Requires moving your number to {carrier.name}")

        promo_value = min(promo.device_discount, device.retail_price)
        effective = round(device.retail_price - promo_value, 2)
        deals.append(
            DeviceDeal(
                promo_id=promo.id,
                carrier_id=carrier.id,
                carrier_name=carrier.name,
                title=promo.title,
                device_model=device.model,
                retail_price=device.retail_price,
                promo_value=promo_value,
                effective_device_price=effective,
                credit_months=promo.credit_months,
                same_carrier=same_carrier,
                required_plan_id=required_id,
                required_plan_name=required_name,
                extra_plan_cost=extra,
                net_cost=round(effective + extra, 2),
                trade_in_line=trade_in.label if trade_in else None,
                trade_in_model=trade_in.device.model if trade_in else None,
                notes=notes,
            )
        )

    deals.sort(key=lambda d: (d.net_cost, not d.same_carrier))
    return deals[:MAX_DEVICE_DEALS]


def _upgrade_status(snapshot: AccountSnapshot) -> list[LineUpgradeStatus]:
    statuses = []
    for line in snapshot.lines:
        device = line.device
        if not device or not device.model:
            continue
        if not device.financed or device.installments_remaining == 0:
            statuses.append(
                LineUpgradeStatus(
                    label=line.label,
                    device_model=device.model,
                    status="paid_off",
                    remaining_balance=0,
                    message="Paid off: you can upgrade or switch carriers with no device balance",
                )
            )
        elif device.installments_remaining <= ALMOST_PAID_OFF_INSTALLMENTS:
            statuses.append(
                LineUpgradeStatus(
                    label=line.label,
                    device_model=device.model,
                    status="almost_paid_off",
                    remaining_balance=device.remaining_balance,
                    message=f"{device.installments_remaining} payments left; a good time to start shopping",
                )
            )
        else:
            statuses.append(
                LineUpgradeStatus(
                    label=line.label,
                    device_model=device.model,
                    status="financed",
                    remaining_balance=device.remaining_balance,
                    message=f"{device.installments_remaining} payments left ({_money(device.remaining_balance)} balance)",
                )
            )
    return statuses


def _quick_wins(snapshot: AccountSnapshot, catalog: Catalog) -> list[str]:
    wins = []
    plan = catalog.plan(snapshot.plan_id)
    if plan and not snapshot.autopay and plan.autopay_discount_per_line:
        saving = plan.autopay_discount_per_line * len(snapshot.lines)
        wins.append(f"Turn on AutoPay with {catalog.carriers[snapshot.carrier_id].name} to save {_money(saving)}/mo")
    return wins


def recommend(
    snapshot: AccountSnapshot,
    catalog: Catalog,
    horizon_months: int = 24,
    today: date | None = None,
) -> RecommendationReport:
    if snapshot.carrier_id not in catalog.carriers:
        raise ValueError(f"Unknown carrier: {snapshot.carrier_id}")
    today = today or date.today()
    current = current_cost(snapshot, horizon_months)

    options: list[PlanOption] = []
    for plan in catalog.plans:
        if plan.id == snapshot.plan_id:
            continue
        fits, fit_caveats = plan_fit(plan, snapshot.lines)
        if fits:
            options.append(_price_option(plan, snapshot, catalog, current, horizon_months, today, fit_caveats))
    options.sort(key=lambda o: (o.horizon_total, not o.same_carrier))

    best = options[0] if options and options[0].avg_monthly_savings >= MIN_MONTHLY_SAVINGS else None
    if best is None:
        verdict = (
            f"Your current plan is competitive: no plan we track saves you more than "
            f"{_money(MIN_MONTHLY_SAVINGS)}/mo over {horizon_months} months."
        )
    elif best.same_carrier:
        verdict = (
            f"Switch to {best.plan_name} without leaving {best.carrier_name} and save about "
            f"{_money(best.avg_monthly_savings)}/mo ({_money(best.savings)} over {horizon_months} months)."
        )
    else:
        verdict = (
            f"Move to {best.carrier_name} {best.plan_name} and save about {_money(best.avg_monthly_savings)}/mo "
            f"({_money(best.savings)} over {horizon_months} months, after switching costs and promotions)."
        )

    assumptions = [
        f"Compares {horizon_months} months of service plus paying off the phones you have now",
        "Prices exclude taxes and regulatory fees",
        "Other plans are priced with AutoPay on",
        f"Plans must cover each line's average data use plus {round((DATA_HEADROOM - 1) * 100)}% headroom",
        "Promotions are subject to the carrier's terms; confirm them before you switch",
    ]
    if catalog.is_sample:
        assumptions.insert(0, "SAMPLE CATALOG: carriers, plans and prices are illustrative, not real offers")

    return RecommendationReport(
        carrier_id=snapshot.carrier_id,
        carrier_name=catalog.carriers[snapshot.carrier_id].name,
        horizon_months=horizon_months,
        catalog_as_of=catalog.as_of,
        catalog_is_sample=catalog.is_sample,
        current=current,
        verdict=verdict,
        best_option=best,
        plan_options=options,
        quick_wins=_quick_wins(snapshot, catalog),
        device_deals=_device_deals(snapshot, catalog, today),
        upgrade_status=_upgrade_status(snapshot),
        assumptions=assumptions,
    )
