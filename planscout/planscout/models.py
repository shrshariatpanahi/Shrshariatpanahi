"""Domain models shared by the catalog, connectors, recommender and API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator

DataSource = Literal["oauth", "sandbox_oauth", "bill_upload", "manual"]
PROVINCES: dict[str, str] = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}


def _province_code(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    code = str(value).strip().upper()
    if code not in PROVINCES:
        raise ValueError(f"Unknown province: {value}")
    return code


Province = Annotated[str | None, BeforeValidator(_province_code)]  # two-letter code, e.g. "QC"
PlanTier = Literal["premium", "mid", "basic"]
PromotionType = Literal["switcher_payoff", "switcher_credit", "plan_discount", "device_discount"]


# --------------------------------------------------------------------------- catalog


class Carrier(BaseModel):
    id: str
    name: str
    type: Literal["postpaid", "prepaid"]
    parent: str = ""  # brands with the same parent share a network and often exclude each other from switcher offers
    provinces: list[str] = []  # where the brand sells service; empty means everywhere
    activation_fee_per_line: float = 0
    offers_device_financing: bool = True
    brand_color: str = "#555555"
    website: str = ""

    def serves(self, province: str | None) -> bool:
        return not province or not self.provinces or province in self.provinces


class Plan(BaseModel):
    id: str
    carrier_id: str
    name: str
    tier: PlanTier
    data_gb: float | None = None  # None means unlimited
    premium_data_gb: float | None = None  # unlimited plans may slow down after this
    hotspot_gb: float | None = 0  # None means unlimited, 0 means no hotspot
    price_per_line: dict[int, float]  # lines on account -> price per line, before AutoPay
    autopay_discount_per_line: float = 0
    features: list[str] = []
    coverage: str = ""  # e.g. "Canada-US"
    after_data: str = ""  # what happens after data_gb, e.g. "slowed to 512 kbps, no overage"
    provinces: list[str] = []  # only if the plan is regional; empty means everywhere the carrier sells
    offer_note: str = ""  # limited-time pricing, shown to the customer
    offer_ends: date | None = None
    source_urls: list[str] = []
    verified_on: date | None = None

    def serves(self, province: str | None) -> bool:
        return not province or not self.provinces or province in self.provinces

    def per_line_price(self, lines: int, autopay: bool = True) -> float:
        tier = max(k for k in self.price_per_line if k <= max(lines, 1))
        price = self.price_per_line[tier]
        if autopay:
            price -= self.autopay_discount_per_line
        return price

    def monthly_cost(self, lines: int, autopay: bool = True) -> float:
        return round(self.per_line_price(lines, autopay) * lines, 2)

    @property
    def is_unlimited(self) -> bool:
        return self.data_gb is None


class DeviceModel(BaseModel):
    model: str
    brand: str
    retail_price: float


class Promotion(BaseModel):
    id: str
    carrier_id: str
    type: PromotionType
    title: str
    description: str = ""
    new_customers: bool = True
    existing_customers: bool = False
    eligible_plan_ids: list[str] = []  # empty means any plan from this carrier
    valid_until: date | None = None
    stackable: bool = False
    # switcher_payoff: cap per line; switcher_credit: flat amount per line
    value_per_line: float = 0
    # plan_discount
    monthly_discount_per_line: float = 0
    duration_months: int = 0  # 0 means for as long as you keep the plan
    # device_discount
    device_model: str | None = None
    device_discount: float = 0
    credit_months: int = 0  # 0 means instant discount, otherwise spread as bill credits
    requires_trade_in: bool = False
    trade_in_models: list[str] = []
    device_retail_price: float | None = None  # this carrier's full price, if it differs from the catalog's
    # Carriers whose customers can't use this offer when moving their number (usually sister brands).
    excluded_port_from: list[str] = []
    source_urls: list[str] = []

    def is_active(self, today: date) -> bool:
        return self.valid_until is None or self.valid_until >= today

    def applies_to_plan(self, plan_id: str | None) -> bool:
        return not self.eligible_plan_ids or plan_id in self.eligible_plan_ids

    def available_to(self, *, existing_customer: bool, current_carrier_id: str | None = None) -> bool:
        if existing_customer:
            return self.existing_customers
        return self.new_customers and current_carrier_id not in self.excluded_port_from


# --------------------------------------------------------------------------- customer account


class DeviceOnAccount(BaseModel):
    model: str = Field("", max_length=80)
    financed: bool = False
    monthly_installment: float = Field(0, ge=0, le=1000)
    installments_remaining: int = Field(0, ge=0, le=60)
    monthly_promo_credit: float = Field(0, ge=0, le=1000)
    unlocked: bool | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DeviceOnAccount:
        if not self.financed:
            self.monthly_installment = 0
            self.installments_remaining = 0
            self.monthly_promo_credit = 0
        if self.monthly_promo_credit > self.monthly_installment:
            raise ValueError("monthly_promo_credit cannot exceed monthly_installment")
        return self

    @property
    def remaining_balance(self) -> float:
        """What you would owe today to pay the device off (credits stop if you leave)."""
        return round(self.monthly_installment * self.installments_remaining, 2)

    @property
    def remaining_net_cost(self) -> float:
        """What you will still pay for the device if you stay and keep receiving credits."""
        return round((self.monthly_installment - self.monthly_promo_credit) * self.installments_remaining, 2)

    @property
    def remaining_credits(self) -> float:
        return round(self.monthly_promo_credit * self.installments_remaining, 2)


class LineOnAccount(BaseModel):
    label: str = Field("Line", max_length=40)
    avg_data_gb: float = Field(0, ge=0, le=2000)  # total data per month, including hotspot use (as bills report it)
    hotspot_gb: float = Field(0, ge=0, le=2000)  # the part of avg_data_gb used for hotspot/tethering
    device: DeviceOnAccount | None = None


class AccountSnapshot(BaseModel):
    """Normalized view of a customer's carrier account, whatever the data source."""

    carrier_id: str
    plan_name: str = Field("", max_length=80)
    plan_id: str | None = None
    monthly_plan_cost: float = Field(ge=0, le=10000)  # after discounts, excluding devices and taxes
    autopay: bool = True
    province: Province = None  # limits recommendations to carriers that serve it
    lines: list[LineOnAccount] = Field(min_length=1, max_length=20)
    source: DataSource = "manual"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- recommendations


class CurrentCost(BaseModel):
    monthly_plan: float
    monthly_devices: float
    monthly_total: float
    device_payoff_balance: float
    remaining_device_credits: float
    horizon_total: float


class PlanOption(BaseModel):
    carrier_id: str
    carrier_name: str
    plan_id: str
    plan_name: str
    same_carrier: bool
    monthly_plan_cost: float
    one_time_costs: float
    promo_savings: float
    promotions: list[str]
    horizon_total: float
    savings: float
    avg_monthly_savings: float
    reasons: list[str]
    caveats: list[str]


class DeviceDeal(BaseModel):
    promo_id: str
    carrier_id: str
    carrier_name: str
    title: str
    device_model: str
    retail_price: float
    promo_value: float
    effective_device_price: float
    credit_months: int
    same_carrier: bool
    required_plan_id: str | None
    required_plan_name: str
    extra_plan_cost: float
    net_cost: float
    trade_in_line: str | None = None
    trade_in_model: str | None = None
    notes: list[str]


class LineUpgradeStatus(BaseModel):
    label: str
    device_model: str
    status: Literal["paid_off", "almost_paid_off", "financed"]
    remaining_balance: float
    message: str


class RecommendationReport(BaseModel):
    carrier_id: str
    carrier_name: str
    province: str | None
    sales_tax_rate: float | None  # percent, for showing tax-inclusive estimates
    horizon_months: int
    catalog_as_of: date
    catalog_is_sample: bool
    current: CurrentCost
    verdict: str
    best_option: PlanOption | None
    plan_options: list[PlanOption]
    quick_wins: list[str]
    device_deals: list[DeviceDeal]
    upgrade_status: list[LineUpgradeStatus]
    assumptions: list[str]
