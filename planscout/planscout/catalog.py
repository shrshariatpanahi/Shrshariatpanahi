"""Loads and validates the carrier / plan / promotion / device catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import TypeAdapter

from .models import Carrier, DeviceModel, Plan, Promotion

DATA_DIR = Path(__file__).parent / "data"


class CatalogError(ValueError):
    pass


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


@dataclass
class Catalog:
    carriers: dict[str, Carrier]
    plans: list[Plan]
    promotions: list[Promotion]
    devices: dict[str, DeviceModel]
    as_of: date
    is_sample: bool = False
    currency_symbol: str = "$"
    _plans_by_id: dict[str, Plan] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._plans_by_id = {p.id: p for p in self.plans}
        self._validate()

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> Catalog:
        def read(name: str):
            return json.loads((data_dir / name).read_text(encoding="utf-8"))

        meta = read("catalog_meta.json")
        carriers = TypeAdapter(list[Carrier]).validate_python(read("carriers.json"))
        return cls(
            carriers={c.id: c for c in carriers},
            plans=TypeAdapter(list[Plan]).validate_python(read("plans.json")),
            promotions=TypeAdapter(list[Promotion]).validate_python(read("promotions.json")),
            devices={d.model: d for d in TypeAdapter(list[DeviceModel]).validate_python(read("devices.json"))},
            as_of=date.fromisoformat(meta["as_of"]),
            is_sample=bool(meta.get("is_sample", False)),
            currency_symbol=meta.get("currency_symbol", "$"),
        )

    def _validate(self) -> None:
        errors: list[str] = []
        if len(self._plans_by_id) != len(self.plans):
            errors.append("duplicate plan ids")
        for plan in self.plans:
            if plan.carrier_id not in self.carriers:
                errors.append(f"plan {plan.id}: unknown carrier {plan.carrier_id}")
            if 1 not in plan.price_per_line:
                errors.append(f"plan {plan.id}: price_per_line must include a 1-line price")
        for promo in self.promotions:
            if promo.carrier_id not in self.carriers:
                errors.append(f"promotion {promo.id}: unknown carrier {promo.carrier_id}")
            for plan_id in promo.eligible_plan_ids:
                plan = self._plans_by_id.get(plan_id)
                if plan is None or plan.carrier_id != promo.carrier_id:
                    errors.append(f"promotion {promo.id}: plan {plan_id} is not a {promo.carrier_id} plan")
            if promo.type == "device_discount" and promo.device_model not in self.devices:
                errors.append(f"promotion {promo.id}: unknown device {promo.device_model}")
        if errors:
            raise CatalogError("; ".join(errors))

    def plan(self, plan_id: str | None) -> Plan | None:
        return self._plans_by_id.get(plan_id) if plan_id else None

    def plans_for(self, carrier_id: str) -> list[Plan]:
        return [p for p in self.plans if p.carrier_id == carrier_id]

    def active_promotions(self, today: date) -> list[Promotion]:
        return [p for p in self.promotions if p.is_active(today)]

    def match_plan(self, carrier_id: str, plan_name: str) -> Plan | None:
        """Best-effort match of a free-text plan name (from a bill or form) to a catalog plan."""
        wanted = _normalize(plan_name)
        if not wanted:
            return None
        candidates = self.plans_for(carrier_id)
        for plan in candidates:
            if _normalize(plan.name) == wanted:
                return plan
        # Prefer the longest catalog name contained in the text, e.g. "Harbor Max (3 lines)".
        contained = [p for p in candidates if _normalize(p.name) in wanted]
        return max(contained, key=lambda p: len(p.name), default=None)
