"""Subparser builders and command handlers for `wt absence`, `wt travel`, and `wt report`."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any, Final

from worktrack.absences import (
    ABSENCE_KIND_VACATION,
    ABSENCE_KINDS,
    ALLOWED_DAY_FRACTIONS,
    HolidayCalendar,
)
from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_ABSENCE,
    SUBCOMMAND_REPORT,
    SUBCOMMAND_TRAVEL,
    CliContext,
)
from worktrack.cli.render import write_json, write_line
from worktrack.reporting import (
    ALLOWED_TRANSPORT_MODES,
    DayMealFlags,
    ReportingEngine,
    ReportingPackRegistry,
    calculate_trip_allowance,
    export_report_csv,
    export_report_markdown,
    load_custom_holidays_from_toml,
    reporting_summary_csvw_schema,
    reporting_summary_json_schema,
)

# region Constants
ACTION_ADD: Final = "add"
ACTION_LIST: Final = "list"
ACTION_QUOTA: Final = "quota"
ACTION_HOLIDAYS: Final = "holidays"
ACTION_SUMMARY: Final = "summary"
ACTION_PACKS: Final = "packs"
ACTION_GENERATE: Final = "generate"
ACTION_SCHEMA: Final = "schema"

FORMAT_JSON: Final = "json"
FORMAT_CSV: Final = "csv"
FORMAT_MARKDOWN: Final = "markdown"
SCHEMA_KIND_JSON_SCHEMA: Final = "json-schema"
SCHEMA_KIND_CSVW: Final = "csvw"

DEFAULT_COUNTRY_CODE: Final = "DE"
DEFAULT_TRANSPORT_MODE: Final = "train"
COMMA_DELIMITER: Final = ","
DATE_YEAR_SLICE_LEN: Final = 4
# endregion


# region Helpers
def _parse_meal_dates(
    breakfast_raw: str | None,
    lunch_raw: str | None,
    dinner_raw: str | None,
) -> dict[str, DayMealFlags]:
    """Parse comma-separated date lists into a dictionary of `DayMealFlags`."""
    b_set = _split_date_tokens(breakfast_raw)
    l_set = _split_date_tokens(lunch_raw)
    d_set = _split_date_tokens(dinner_raw)
    all_dates = sorted(b_set | l_set | d_set)
    return {
        d: DayMealFlags(breakfast=d in b_set, lunch=d in l_set, dinner=d in d_set)
        for d in all_dates
    }


def _split_date_tokens(raw: str | None) -> set[str]:
    """Extract trimmed non-empty date tokens from a comma-separated string."""
    tokens = raw.split(COMMA_DELIMITER) if raw else []
    return {token.strip() for token in tokens if token.strip()}


def _build_holiday_calendar(ctx: CliContext, explicit_subdiv: str | None) -> HolidayCalendar:
    """Instantiate a `HolidayCalendar` for the specified subdivision or context default."""
    subdiv = (
        explicit_subdiv
        if explicit_subdiv is not None
        else (ctx.settings.holiday_subdivision or DEFAULT_COUNTRY_CODE)
    )
    custom_holidays = load_custom_holidays_from_toml(ctx.settings.config_file)
    return HolidayCalendar(subdivision=subdiv, custom_holidays=custom_holidays)


def _add_date_bounds(parser: argparse.ArgumentParser) -> None:
    """Add optional start and end date bound arguments."""
    parser.add_argument("--start", default=None, help="Optional start date filter YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Optional end date filter YYYY-MM-DD.")


def _add_subdiv_arg(parser: argparse.ArgumentParser) -> None:
    """Add optional regional subdivision code argument."""
    parser.add_argument("--subdivision", default=None, help="Regional holiday subdivision code.")
# endregion


# region Subcommands
def register_reporting_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `absence`, `travel`, and `report` CLI subcommands."""
    _register_absence_parser(subparsers, json_parent)
    _register_travel_parser(subparsers, json_parent)
    _register_report_parser(subparsers, json_parent)


def _register_absence_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt absence` subparsers for leave, quotas, and statutory holidays."""
    abs_p = subparsers.add_parser(
        SUBCOMMAND_ABSENCE,
        parents=[json_parent],
        help="Manage statutory absences, vacation quotas, and holiday calendars.",
    )
    abs_p.set_defaults(handler=handle_absence, canonical_subcommand=SUBCOMMAND_ABSENCE)
    actions = abs_p.add_subparsers(dest="absence_action", required=True)

    add_p = actions.add_parser(ACTION_ADD, parents=[json_parent], help="Record an absence.")
    add_p.add_argument("--start", required=True, help="Absence start date YYYY-MM-DD.")
    add_p.add_argument("--end", default=None, help="Optional absence end date YYYY-MM-DD.")
    add_p.add_argument(
        "--kind", choices=sorted(ABSENCE_KINDS), default=ABSENCE_KIND_VACATION, help="Absence kind."
    )
    add_p.add_argument(
        "--fraction",
        type=float,
        choices=sorted(ALLOWED_DAY_FRACTIONS),
        default=1.0,
        help="Fraction.",
    )
    add_p.add_argument("--notes", default="", help="Optional descriptive remarks.")
    _add_subdiv_arg(add_p)

    list_p = actions.add_parser(ACTION_LIST, parents=[json_parent], help="List recorded absences.")
    _add_date_bounds(list_p)
    list_p.add_argument("--kind", default=None, choices=sorted(ABSENCE_KINDS), help="Kind filter.")
    _add_subdiv_arg(list_p)

    quota_p = actions.add_parser(ACTION_QUOTA, parents=[json_parent], help="Get or set quotas.")
    quota_p.add_argument("--year", type=int, required=True, help="Calendar year.")
    quota_p.add_argument("--entitlement", type=float, default=None, help="Entitlement days.")
    quota_p.add_argument("--carryover", type=float, default=0.0, help="Carryover days.")

    hol_p = actions.add_parser(ACTION_HOLIDAYS, parents=[json_parent], help="List holidays.")
    hol_p.add_argument("--year", type=int, required=True, help="Calendar year.")
    _add_subdiv_arg(hol_p)

    sum_p = actions.add_parser(ACTION_SUMMARY, parents=[json_parent], help="Summarize balance.")
    sum_p.add_argument("--year", type=int, required=True, help="Calendar year.")
    sum_p.add_argument("--reference-date", default=None, help="Cutoff reference date YYYY-MM-DD.")
    _add_subdiv_arg(sum_p)


def _register_travel_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt travel` subparsers for trips and per-diem allowances."""
    trav_p = subparsers.add_parser(
        SUBCOMMAND_TRAVEL,
        parents=[json_parent],
        help="Track business travel trips, per-diems, and mileage reimbursements.",
    )
    trav_p.set_defaults(handler=handle_travel, canonical_subcommand=SUBCOMMAND_TRAVEL)
    actions = trav_p.add_subparsers(dest="travel_action", required=True)

    add_p = actions.add_parser(ACTION_ADD, parents=[json_parent], help="Record a business trip.")
    add_p.add_argument("--title", required=True, help="Trip title or purpose.")
    add_p.add_argument("--destination", required=True, help="Destination city or site.")
    add_p.add_argument("--start", required=True, help="Trip start ISO-8601 timestamp.")
    add_p.add_argument("--end", required=True, help="Trip end ISO-8601 timestamp.")
    add_p.add_argument("--country", default=DEFAULT_COUNTRY_CODE, help="ISO country code.")
    add_p.add_argument(
        "--mode",
        choices=sorted(ALLOWED_TRANSPORT_MODES),
        default=DEFAULT_TRANSPORT_MODE,
        help="Mode.",
    )
    add_p.add_argument("--km", type=float, default=0.0, help="Travel distance in kilometers.")
    add_p.add_argument("--breakfast", default=None, help="Comma-separated breakfast dates.")
    add_p.add_argument("--lunch", default=None, help="Comma-separated lunch dates.")
    add_p.add_argument("--dinner", default=None, help="Comma-separated dinner dates.")
    add_p.add_argument("--entity-id", default=None, help="Attribution Entity UUID.")
    add_p.add_argument("--notes", default="", help="Optional trip remarks.")
    add_p.add_argument("--pack", default=None, help="Reporting pack slug.")

    list_p = actions.add_parser(ACTION_LIST, parents=[json_parent], help="List business trips.")
    _add_date_bounds(list_p)
    list_p.add_argument("--pack", default=None, help="Reporting pack slug.")


def _register_report_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt report` subparsers for statutory packs and summaries."""
    rep_p = subparsers.add_parser(
        SUBCOMMAND_REPORT,
        parents=[json_parent],
        help="Generate statutory tax summaries and inspect reporting schemas.",
    )
    rep_p.set_defaults(handler=handle_report, canonical_subcommand=SUBCOMMAND_REPORT)
    actions = rep_p.add_subparsers(dest="report_action", required=True)

    actions.add_parser(ACTION_PACKS, parents=[json_parent], help="List available reporting packs.")

    gen_p = actions.add_parser(ACTION_GENERATE, parents=[json_parent], help="Generate tax summary.")
    gen_p.add_argument("--start", required=True, help="Report window start date YYYY-MM-DD.")
    gen_p.add_argument("--end", required=True, help="Report window end date YYYY-MM-DD.")
    gen_p.add_argument("--pack", default=None, help="Reporting pack slug.")
    _add_subdiv_arg(gen_p)
    gen_p.add_argument("--one-way-km", type=float, default=None, help="Commute distance in km.")
    gen_p.add_argument(
        "--format",
        choices=[FORMAT_JSON, FORMAT_CSV, FORMAT_MARKDOWN],
        default=FORMAT_MARKDOWN,
        help="Rendered report output format.",
    )

    schema_p = actions.add_parser(ACTION_SCHEMA, parents=[json_parent], help="Emit report schema.")
    schema_p.add_argument(
        "--kind",
        choices=[SCHEMA_KIND_JSON_SCHEMA, SCHEMA_KIND_CSVW],
        default=SCHEMA_KIND_JSON_SCHEMA,
        help="Schema dialect.",
    )
# endregion


# region Handlers
def handle_absence(ctx: CliContext, args: argparse.Namespace) -> int:
    """Route and dispatch `wt absence` subcommands."""
    action = str(args.absence_action)
    cal = _build_holiday_calendar(ctx, getattr(args, "subdivision", None))

    if action == ACTION_ADD:
        return _handle_absence_add(ctx, args, cal)
    if action == ACTION_LIST:
        return _handle_absence_list(ctx, args, cal)
    if action == ACTION_QUOTA:
        return _handle_absence_quota(ctx, args)
    if action == ACTION_HOLIDAYS:
        return _handle_absence_holidays(ctx, args, cal)
    return _handle_absence_summary(ctx, args, cal)


def _handle_absence_add(ctx: CliContext, args: argparse.Namespace, cal: HolidayCalendar) -> int:
    """Record an absence entry in the ledger."""
    with ctx.open_absence_ledger(calendar=cal) as ledger:
        entry = ledger.record_absence(
            start_date=args.start,
            end_date=args.end,
            kind=args.kind,
            day_fraction=args.fraction,
            notes=args.notes or "",
        )
        if ctx.as_json:
            write_json(ctx.stdout, entry.to_dict(cal))
        else:
            eff = entry.effective_days(cal)
            write_line(ctx.stdout, f"Absence {entry.id} recorded ({entry.kind}, {eff:.1f} days).")
    return EXIT_SUCCESS


def _handle_absence_list(ctx: CliContext, args: argparse.Namespace, cal: HolidayCalendar) -> int:
    """List recorded absences matching optional query bounds."""
    with ctx.open_absence_ledger(calendar=cal) as ledger:
        entries = ledger.list_absences(start_date=args.start, end_date=args.end, kind=args.kind)
        if ctx.as_json:
            write_json(ctx.stdout, [e.to_dict(cal) for e in entries])
        else:
            for e in entries:
                eff = e.effective_days(cal)
                write_line(
                    ctx.stdout,
                    f"{e.id} | {e.start_date}..{e.end_date} | {e.kind:<16} | {eff:4.1f} days",
                )
    return EXIT_SUCCESS


def _handle_absence_quota(ctx: CliContext, args: argparse.Namespace) -> int:
    """Retrieve or update vacation quota for a calendar year."""
    with ctx.open_absence_ledger() as ledger:
        if args.entitlement is not None:
            quota = ledger.set_quota(
                args.year, args.entitlement, carryover_days=args.carryover or 0.0
            )
        else:
            quota = ledger.get_quota(args.year)
        if ctx.as_json:
            write_json(ctx.stdout, quota.to_dict())
        else:
            write_line(
                ctx.stdout, f"Quota {quota.year}: {quota.total_available_days:.1f} days total."
            )
    return EXIT_SUCCESS


def _handle_absence_holidays(
    ctx: CliContext, args: argparse.Namespace, cal: HolidayCalendar
) -> int:
    """List public holidays for the specified year and subdivision."""
    holidays = cal.holidays_for_year(args.year)
    if ctx.as_json:
        write_json(ctx.stdout, [h.to_dict() for h in holidays])
    else:
        for h in holidays:
            write_line(ctx.stdout, f"{h.day.isoformat()} | {h.name} [{h.subdivision}]")
    return EXIT_SUCCESS


def _handle_absence_summary(ctx: CliContext, args: argparse.Namespace, cal: HolidayCalendar) -> int:
    """Emit annual vacation summary and leave metrics."""
    with ctx.open_absence_ledger(calendar=cal) as ledger:
        summary = ledger.summarize_year(args.year, reference_date=args.reference_date)
        if ctx.as_json:
            write_json(ctx.stdout, summary.to_dict())
        else:
            rem = summary.remaining_vacation_days
            write_line(ctx.stdout, f"Summary {summary.year}: {rem:.1f} days remaining.")
    return EXIT_SUCCESS


def handle_travel(ctx: CliContext, args: argparse.Namespace) -> int:
    """Route and dispatch `wt travel` subcommands."""
    action = str(args.travel_action)
    registry = ReportingPackRegistry.from_toml_file(ctx.settings.config_file)
    pack_slug = args.pack or ctx.settings.reporting_pack
    pack = registry.get_pack(pack_slug)

    if action == ACTION_ADD:
        return _handle_travel_add(ctx, args, pack)
    return _handle_travel_list(ctx, args, pack)


def _handle_travel_add(ctx: CliContext, args: argparse.Namespace, pack: Any) -> int:
    """Record a business travel trip and output allowance calculation."""
    meals = _parse_meal_dates(args.breakfast, args.lunch, args.dinner)
    with ctx.open_travel_store() as store:
        trip = store.record_trip(
            title=args.title,
            destination=args.destination,
            started_at=args.start,
            ended_at=args.end,
            country_code=args.country,
            transport_mode=args.mode,
            distance_km=args.km or 0.0,
            meals_by_date=meals,
            entity_id=args.entity_id,
            notes=args.notes or "",
        )
        calc = calculate_trip_allowance(trip, pack)
        if ctx.as_json:
            write_json(ctx.stdout, calc.to_dict())
        else:
            tot = f"{calc.total_reimbursement:.2f} {calc.currency}"
            write_line(ctx.stdout, f"Trip {trip.id} recorded ({trip.destination}, {tot}).")
    return EXIT_SUCCESS


def _handle_travel_list(ctx: CliContext, args: argparse.Namespace, pack: Any) -> int:
    """List business trips with calculated per-diem and mileage breakdown."""
    with ctx.open_travel_store() as store:
        trips = store.list_trips(start_date=args.start, end_date=args.end)
        calcs = [calculate_trip_allowance(t, pack) for t in trips]
        if ctx.as_json:
            write_json(ctx.stdout, [c.to_dict() for c in calcs])
        else:
            for c in calcs:
                tot = f"{c.total_reimbursement:6.2f} {c.currency}"
                write_line(ctx.stdout, f"{c.trip.id} | {c.trip.destination:<16} | {tot}")
    return EXIT_SUCCESS


def handle_report(ctx: CliContext, args: argparse.Namespace) -> int:
    """Route and dispatch `wt report` subcommands."""
    action = str(args.report_action)
    if action == ACTION_PACKS:
        registry = ReportingPackRegistry.from_toml_file(ctx.settings.config_file)
        packs = registry.list_packs()
        if ctx.as_json:
            write_json(ctx.stdout, [p.to_dict() for p in packs])
        else:
            for p in packs:
                write_line(ctx.stdout, f"{p.slug:<10} | {p.title:<36} | {p.currency}")
        return EXIT_SUCCESS

    if action == ACTION_SCHEMA:
        is_csvw = args.kind == SCHEMA_KIND_CSVW
        schema = reporting_summary_csvw_schema() if is_csvw else reporting_summary_json_schema()
        write_json(ctx.stdout, schema)
        return EXIT_SUCCESS

    return _handle_report_generate(ctx, args)


def _handle_report_generate(ctx: CliContext, args: argparse.Namespace) -> int:
    """Synthesize ledger workplace facts, absences, and trips into statutory tax report."""
    registry = ReportingPackRegistry.from_toml_file(ctx.settings.config_file)
    pack_slug = args.pack or ctx.settings.reporting_pack
    pack = registry.get_pack(pack_slug)
    cal = _build_holiday_calendar(ctx, args.subdivision)
    km = args.one_way_km if args.one_way_km is not None else ctx.settings.office_one_way_km

    with (
        ctx.open_absence_ledger(calendar=cal) as abs_ledger,
        ctx.open_travel_store() as travel_store,
        ctx.open_ledger() as ledger,
    ):
        excluded = abs_ledger.excluded_workplace_dates(
            args.start, args.end, include_public_holidays=True
        )
        year = int(args.start[:DATE_YEAR_SLICE_LEN])
        abs_summary: Mapping[str, object] = abs_ledger.summarize_year(year).to_dict()
        summary = ReportingEngine().build_summary(
            ledger=ledger,
            pack=pack,
            start_date=args.start,
            end_date=args.end,
            office_one_way_km=km,
            excluded_dates=excluded,
            travel_store=travel_store,
            absence_summary=abs_summary,
        )

    if ctx.as_json or args.format == FORMAT_JSON:
        write_json(ctx.stdout, summary.to_dict())
    elif args.format == FORMAT_CSV:
        write_line(ctx.stdout, export_report_csv(summary))
    else:
        write_line(ctx.stdout, export_report_markdown(summary))
    return EXIT_SUCCESS
# endregion
