"""Pluggable ReportingPack models, registry, and statutory tax pack definitions."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Self

# region Constants
PACK_SLUG_GENERIC: Final = "generic"
PACK_SLUG_DE: Final = "de"

DEFAULT_CURRENCY: Final = "EUR"
DECIMAL_PRECISION: Final = 2
ZERO_DISTANCE: Final = 0.0
ZERO_AMOUNT: Final = 0.0

DE_HOMEOFFICE_DAILY_RATE: Final = 6.00
DE_HOMEOFFICE_MAX_DAYS: Final = 210
DE_COMMUTE_FIRST_TIER_KM: Final = 20.0
DE_COMMUTE_FIRST_TIER_RATE: Final = 0.30
DE_COMMUTE_SECOND_TIER_RATE: Final = 0.38
DE_PER_DIEM_PARTIAL_RATE: Final = 14.00
DE_PER_DIEM_FULL_RATE: Final = 28.00
DE_PER_DIEM_MIN_SINGLE_DAY_HOURS: Final = 8.0
DE_BREAKFAST_DEDUCTION_RATIO: Final = 0.20
DE_LUNCH_DEDUCTION_RATIO: Final = 0.40
DE_DINNER_DEDUCTION_RATIO: Final = 0.40
DE_CAR_MILEAGE_RATE_PER_KM: Final = 0.30
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class ReportingPack:
    """Configurable tax and workplace reporting rule configuration for a jurisdiction.

    Attributes:
        slug: Normalized identifier for the reporting pack.
        title: Descriptive human-readable label.
        currency: ISO 4217 currency code.
        country_code: Primary ISO 3166-1 alpha-2 country code.
        tax_rules_enabled: Whether statutory deductions and allowances are active.
        home_office_daily_rate: Daily home-office tax allowance rate.
        home_office_max_days: Maximum claimable home-office days per calendar year.
        commute_first_tier_km: Threshold in km for the first commute allowance tier.
        commute_first_tier_rate: Rate per km up to first tier distance.
        commute_second_tier_rate: Rate per km beyond first tier distance.
        per_diem_partial_day_rate: Statutory per-diem rate for arrival/departure or >8h days.
        per_diem_full_day_rate: Statutory per-diem rate for full 24-hour business travel days.
        per_diem_min_hours_single_day: Minimum travel duration for single-day per-diem.
        breakfast_deduction_ratio: Fraction of full per-diem deducted for breakfast.
        lunch_deduction_ratio: Fraction of full per-diem deducted for lunch.
        dinner_deduction_ratio: Fraction of full per-diem deducted for dinner.
        car_mileage_rate_per_km: Reimbursement rate per km for private car business travel.
        country_per_diem_overrides: Specific per-diem rates by foreign country code.
    """

    slug: str
    title: str
    currency: str = DEFAULT_CURRENCY
    country_code: str = ""
    tax_rules_enabled: bool = False
    home_office_daily_rate: float = ZERO_AMOUNT
    home_office_max_days: int = 0
    commute_first_tier_km: float = DE_COMMUTE_FIRST_TIER_KM
    commute_first_tier_rate: float = ZERO_AMOUNT
    commute_second_tier_rate: float = ZERO_AMOUNT
    per_diem_partial_day_rate: float = ZERO_AMOUNT
    per_diem_full_day_rate: float = ZERO_AMOUNT
    per_diem_min_hours_single_day: float = DE_PER_DIEM_MIN_SINGLE_DAY_HOURS
    breakfast_deduction_ratio: float = DE_BREAKFAST_DEDUCTION_RATIO
    lunch_deduction_ratio: float = DE_LUNCH_DEDUCTION_RATIO
    dinner_deduction_ratio: float = DE_DINNER_DEDUCTION_RATIO
    car_mileage_rate_per_km: float = ZERO_AMOUNT
    country_per_diem_overrides: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    def daily_commute_allowance(self, one_way_km: float) -> float:
        """Compute tiered one-way distance commute allowance for one office day.

        Args:
            one_way_km: Distance in km from residence to primary place of work.

        Returns:
            Allowance rounded to 2 decimal places, or 0.0 if disabled or km <= 0.
        """
        if not self.tax_rules_enabled or one_way_km <= ZERO_DISTANCE:
            return ZERO_AMOUNT
        first_km = min(one_way_km, self.commute_first_tier_km)
        second_km = max(ZERO_DISTANCE, one_way_km - self.commute_first_tier_km)
        allowance = (first_km * self.commute_first_tier_rate) + (
            second_km * self.commute_second_tier_rate
        )
        return round(allowance, DECIMAL_PRECISION)

    def per_diem_rates_for_country(self, country_code: str) -> tuple[float, float]:
        """Resolve statutory partial and full daily per-diem rates for a country.

        Args:
            country_code: ISO 3166-1 alpha-2 country code.

        Returns:
            Tuple of (partial_day_rate, full_day_rate).
        """
        code = country_code.upper().strip()
        if code in self.country_per_diem_overrides:
            return self.country_per_diem_overrides[code]
        return (self.per_diem_partial_day_rate, self.per_diem_full_day_rate)

    def to_dict(self) -> dict[str, object]:
        """Serialize reporting pack settings to a structured dictionary."""
        return {
            "slug": self.slug,
            "title": self.title,
            "currency": self.currency,
            "country_code": self.country_code,
            "tax_rules_enabled": self.tax_rules_enabled,
            "home_office_daily_rate": self.home_office_daily_rate,
            "home_office_max_days": self.home_office_max_days,
            "commute_first_tier_km": self.commute_first_tier_km,
            "commute_first_tier_rate": self.commute_first_tier_rate,
            "commute_second_tier_rate": self.commute_second_tier_rate,
            "per_diem_partial_day_rate": self.per_diem_partial_day_rate,
            "per_diem_full_day_rate": self.per_diem_full_day_rate,
            "per_diem_min_hours_single_day": self.per_diem_min_hours_single_day,
            "breakfast_deduction_ratio": self.breakfast_deduction_ratio,
            "lunch_deduction_ratio": self.lunch_deduction_ratio,
            "dinner_deduction_ratio": self.dinner_deduction_ratio,
            "car_mileage_rate_per_km": self.car_mileage_rate_per_km,
            "country_per_diem_overrides": {
                k: list(v) for k, v in self.country_per_diem_overrides.items()
            },
        }


# endregion


# region Predefined Packs
GENERIC_PACK: Final = ReportingPack(
    slug=PACK_SLUG_GENERIC,
    title="Generic Workplace & Travel Pack",
    currency=DEFAULT_CURRENCY,
    country_code="",
    tax_rules_enabled=False,
)

GERMANY_PACK: Final = ReportingPack(
    slug=PACK_SLUG_DE,
    title="Germany (EStG / Reisekostenrecht)",
    currency=DEFAULT_CURRENCY,
    country_code="DE",
    tax_rules_enabled=True,
    home_office_daily_rate=DE_HOMEOFFICE_DAILY_RATE,
    home_office_max_days=DE_HOMEOFFICE_MAX_DAYS,
    commute_first_tier_km=DE_COMMUTE_FIRST_TIER_KM,
    commute_first_tier_rate=DE_COMMUTE_FIRST_TIER_RATE,
    commute_second_tier_rate=DE_COMMUTE_SECOND_TIER_RATE,
    per_diem_partial_day_rate=DE_PER_DIEM_PARTIAL_RATE,
    per_diem_full_day_rate=DE_PER_DIEM_FULL_RATE,
    per_diem_min_hours_single_day=DE_PER_DIEM_MIN_SINGLE_DAY_HOURS,
    breakfast_deduction_ratio=DE_BREAKFAST_DEDUCTION_RATIO,
    lunch_deduction_ratio=DE_LUNCH_DEDUCTION_RATIO,
    dinner_deduction_ratio=DE_DINNER_DEDUCTION_RATIO,
    car_mileage_rate_per_km=DE_CAR_MILEAGE_RATE_PER_KM,
)

BUILTIN_PACKS: Final[Mapping[str, ReportingPack]] = {
    PACK_SLUG_GENERIC: GENERIC_PACK,
    PACK_SLUG_DE: GERMANY_PACK,
}
# endregion


# region Configuration Loader
def load_custom_holidays_from_toml(config_file: Path | None) -> dict[str, str]:
    """Load custom regional or company holidays defined in a TOML config file.

    Args:
        config_file: Path to TOML configuration file or None.

    Returns:
        Mapping of ISO-8601 date strings to holiday titles.
    """
    if config_file is None or not config_file.exists():
        return {}
    try:
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    candidates = (
        raw.get("reporting", {}).get("custom_holidays"),
        raw.get("worktrack", {}).get("reporting", {}).get("custom_holidays"),
        raw.get("custom_holidays"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            return {str(k).strip(): str(v).strip() for k, v in candidate.items()}
    return {}


def _as_float(val: object, default: float) -> float:
    """Convert an arbitrary TOML value to float with fallback default."""
    if isinstance(val, (int, float, str)):
        return float(val)
    return default


def _as_int(val: object, default: int) -> int:
    """Convert an arbitrary TOML value to int with fallback default."""
    if isinstance(val, (int, float, str)):
        return int(val)
    return default


def _parse_pack_from_table(slug: str, table: Mapping[str, object]) -> ReportingPack:
    """Build a `ReportingPack` instance from a TOML dictionary table."""
    return ReportingPack(
        slug=slug,
        title=str(table.get("title", slug)),
        currency=str(table.get("currency", DEFAULT_CURRENCY)),
        country_code=str(table.get("country_code", "")),
        tax_rules_enabled=bool(table.get("tax_rules_enabled", False)),
        home_office_daily_rate=_as_float(table.get("home_office_daily_rate"), ZERO_AMOUNT),
        home_office_max_days=_as_int(table.get("home_office_max_days"), 0),
        commute_first_tier_km=_as_float(
            table.get("commute_first_tier_km"), DE_COMMUTE_FIRST_TIER_KM
        ),
        commute_first_tier_rate=_as_float(
            table.get("commute_first_tier_rate"), ZERO_AMOUNT
        ),
        commute_second_tier_rate=_as_float(
            table.get("commute_second_tier_rate"), ZERO_AMOUNT
        ),
        per_diem_partial_day_rate=_as_float(
            table.get("per_diem_partial_day_rate"), ZERO_AMOUNT
        ),
        per_diem_full_day_rate=_as_float(
            table.get("per_diem_full_day_rate"), ZERO_AMOUNT
        ),
        per_diem_min_hours_single_day=_as_float(
            table.get("per_diem_min_hours_single_day"), DE_PER_DIEM_MIN_SINGLE_DAY_HOURS
        ),
        breakfast_deduction_ratio=_as_float(
            table.get("breakfast_deduction_ratio"), DE_BREAKFAST_DEDUCTION_RATIO
        ),
        lunch_deduction_ratio=_as_float(
            table.get("lunch_deduction_ratio"), DE_LUNCH_DEDUCTION_RATIO
        ),
        dinner_deduction_ratio=_as_float(
            table.get("dinner_deduction_ratio"), DE_DINNER_DEDUCTION_RATIO
        ),
        car_mileage_rate_per_km=_as_float(
            table.get("car_mileage_rate_per_km"), ZERO_AMOUNT
        ),
    )


# endregion


# region Registry
class ReportingPackRegistry:
    """Registry maintaining statutory and user-configured reporting packs."""

    def __init__(self, custom_packs: Mapping[str, ReportingPack] | None = None) -> None:
        """Initialize registry with built-in packs and optional custom overrides."""
        self._packs: dict[str, ReportingPack] = dict(BUILTIN_PACKS)
        if custom_packs:
            for slug, pack in custom_packs.items():
                self._packs[slug.lower().strip()] = pack

    @classmethod
    def from_toml_file(cls, config_file: Path | None) -> Self:
        """Instantiate registry populated with packs declared in a TOML configuration."""
        if config_file is None or not config_file.exists():
            return cls()
        try:
            doc = tomllib.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return cls()

        packs_tables = (
            doc.get("reporting", {}).get("packs"),
            doc.get("worktrack", {}).get("reporting", {}).get("packs"),
        )
        custom_packs: dict[str, ReportingPack] = {}
        for candidate in packs_tables:
            if isinstance(candidate, dict):
                for slug, table in candidate.items():
                    if isinstance(table, dict):
                        norm_slug = str(slug).lower().strip()
                        custom_packs[norm_slug] = _parse_pack_from_table(norm_slug, table)
        return cls(custom_packs=custom_packs)

    def get_pack(self, slug: str) -> ReportingPack:
        """Retrieve a reporting pack by slug, raising KeyError when unknown.

        Args:
            slug: Normalized identifier for the reporting pack.

        Returns:
            Matched ReportingPack instance.
        """
        key = slug.lower().strip()
        if key not in self._packs:
            raise KeyError(f"Reporting pack '{slug}' not found.")
        return self._packs[key]

    def list_packs(self) -> list[ReportingPack]:
        """Return all registered reporting packs sorted by slug."""
        return [self._packs[k] for k in sorted(self._packs.keys())]


# endregion
