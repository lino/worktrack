# Tax Packs, Travel (Reisekosten) & Vacation (Urlaub)

This guide explains how to configure country-specific tax rules (**Reporting Packs**), track statutory vacation (*Urlaub*) and public holidays (*Feiertage*) to avoid double-counting days, calculate business trip meal allowances (*Verpflegungsmehraufwand*) and mileage (*Reisekosten*), and generate ready-to-file annual tax summaries.

______________________________________________________________________

## 1. Overview & Why It's Configurable

### Why use Reporting Packs?

Tax deduction rules vary significantly between jurisdictions. For instance, German tax law (EStG) allows statutory allowances such as:

- **Homeoffice-Pauschale**: 6.00 EUR per calendar day spent working from home (up to 210 days / 1,260 EUR per tax year).
- **Entfernungspauschale**: Commute allowance of 0.30 EUR/km for the first 20 km and 0.38 EUR/km from kilometer 21 onward (one-way distance to your regular office).
- **Verpflegungsmehraufwand**: Per-diem meal allowances for business travel (14.00 EUR for arrival/departure days and single-day trips > 8 hours; 28.00 EUR for 24-hour full days), with statutory deductions when meals are provided by employers or conference hosts (-20% for breakfast, -40% for lunch, -40% for dinner).

However, if you live and work in another country or prefer country-neutral tracking, you don't want hardcoded German statutory rules. Worktrack organizes statutory calculation logic into modular **Reporting Packs**:

- **`generic` (Default)**: Tracks raw days and hours per work location (`home_office`, `office`, `mobile_work`) and trip distances without assuming any specific tax code or statutory monetary rates.
- **`de` (Germany)**: Applies German Income Tax Act (EStG) rules for home-office caps, tiered commuter mileage, and statutory domestic and international per-diem tables.
- **Custom Packs**: Add custom country rules or corporate allowances in your local configuration file.

______________________________________________________________________

## 2. Configuring Your Reporting Pack & German State (*Bundesland*)

### Setting your state and commute distance

Configure your default pack, holiday jurisdiction, and one-way commute distance in `$XDG_CONFIG_HOME/worktrack/config.toml`:

```toml
[reporting]
reporting_pack = "de"
holiday_subdivision = "DE-BY"
office_one_way_km = 25.0

# Optional custom company holidays (e.g. company closures)
custom_holidays = [
  { date = "2026-12-24", name = "Company Christmas Eve (Half Day)" },
  { date = "2026-12-31", name = "Company New Year's Eve (Half Day)" },
]
```

Or set them via environment variables:

```bash
export WORKTRACK_REPORTING_PACK="de"
export WORKTRACK_HOLIDAY_SUBDIVISION="DE-BY"
export WORKTRACK_OFFICE_ONE_WAY_KM="25.0"
```

### German State Codes (`holiday_subdivision`)

Public holidays differ by federal state (*Bundesland*). Worktrack supports statutory holiday rules for all 16 German states:

| Code    | Federal State (*Bundesland*) | Example Unique Holidays                         |
| ------- | ---------------------------- | ----------------------------------------------- |
| `DE-BW` | Baden-Württemberg            | Epiphany, Corpus Christi, All Saints' Day       |
| `DE-BY` | Bayern (Bavaria)             | Epiphany, Corpus Christi, All Saints' Day       |
| `DE-BE` | Berlin                       | International Women's Day (March 8)             |
| `DE-BB` | Brandenburg                  | Reformation Day                                 |
| `DE-HB` | Bremen                       | Reformation Day                                 |
| `DE-HH` | Hamburg                      | Reformation Day                                 |
| `DE-HE` | Hessen                       | Corpus Christi                                  |
| `DE-MV` | Mecklenburg-Vorpommern       | Reformation Day                                 |
| `DE-NI` | Niedersachsen                | Reformation Day                                 |
| `DE-NW` | Nordrhein-Westfalen          | Corpus Christi, All Saints' Day                 |
| `DE-RP` | Rheinland-Pfalz              | Corpus Christi, All Saints' Day                 |
| `DE-SL` | Saarland                     | Corpus Christi, Assumption Day, All Saints' Day |
| `DE-SN` | Sachsen                      | Reformation Day, Repentance and Prayer Day      |
| `DE-ST` | Sachsen-Anhalt               | Epiphany, Reformation Day                       |
| `DE-SH` | Schleswig-Holstein           | Reformation Day                                 |
| `DE-TH` | Thüringen                    | World Children's Day, Reformation Day           |

### Adding a Custom Country Pack (e.g. Austria)

You can define additional national or company reporting packs in `$XDG_CONFIG_HOME/worktrack/config.toml`:

```toml
[reporting.packs.at]
title = "Austria (Pendlerpauschale & Diäten)"
currency = "EUR"
country_code = "AT"
tax_rules_enabled = true
home_office_daily_rate = 3.0
home_office_max_days = 100
commute_first_tier_km = 20.0
commute_first_tier_rate = 0.0
commute_second_tier_rate = 0.0
per_diem_partial_day_rate = 13.2
per_diem_full_day_rate = 26.4
car_mileage_rate_per_km = 0.42
```

______________________________________________________________________

## 3. Tracking Vacation (*Urlaub*), Sick Leave & Public Holidays (*Feiertage*) (`wt absence`)

### Why track vacation and holidays?

1. **Know your remaining leave (*Resturlaub*)**: Keep track of annual entitlement, carryover days from the prior year, taken vacation, and planned days.
1. **Prevent double-counting on your tax return**: Tax audits check that home-office days and commuter days are never claimed on days when you were on vacation, sick leave, or observing a statutory public holiday. Worktrack automatically excludes registered absence dates and regional public holidays from billable workplace and commute calculations.

### How to use it

```bash
# Set your annual vacation entitlement (30 days) and carryover from last year (2 days)
wt absence quota --year 2026 --entitlement 30 --carryover 2

# Inspect statutory and regional public holidays for your state
wt absence holidays --year 2026 --subdivision DE-BY

# Record a vacation period
wt absence add \
  --start 2026-08-03 \
  --end 2026-08-14 \
  --kind vacation \
  --subdivision DE-BY \
  --notes "Summer Urlaub (Bavaria)"

# Record a half-day compensatory off or single sick day
wt absence add --start 2026-11-06 --kind compensatory_off --fraction 0.5 --notes "Dentist appointment"
wt absence add --start 2026-10-09 --kind sick_leave --notes "Flu recovery"

# Check your leave balance and remaining Resturlaub
wt absence summary --year 2026 --subdivision DE-BY
```

______________________________________________________________________

## 4. Recording Business Trips (*Reisekosten* & *Verpflegungsmehraufwand*) (`wt travel`)

### How statutory travel allowances work

Under German travel expense law (*Reisekostenrecht*):

- **Arrival & Departure Days**: Single-day trips lasting > 8 hours or the arrival and departure days of multi-day trips receive a partial per-diem rate (**14.00 EUR**).
- **Full Days (24 Hours)**: Intermediate days during a multi-day trip receive the full per-diem rate (**28.00 EUR**).
- **Meal Deductions**: If meals are included in hotel bookings or provided by hosts, statutory deductions apply against the full-day rate (28.00 EUR):
  - **Breakfast (-20%)**: -5.60 EUR
  - **Lunch (-40%)**: -11.20 EUR
  - **Dinner (-40%)**: -11.20 EUR
- **Car Mileage Allowance**: Private car travel is reimbursed at **0.30 EUR/km**. Train, public transit, and flight trips track 0 km mileage allowance by default.

### How to record business trips

```bash
# Record a 3-day conference trip by train where hotel breakfast was provided on intermediate days
wt travel add \
  --title "Architecture Summit Berlin" \
  --destination "Berlin" \
  --country "DE" \
  --start 2026-10-12T08:00:00Z \
  --end 2026-10-14T19:00:00Z \
  --mode train \
  --breakfast 2026-10-13,2026-10-14 \
  --notes "Hotel breakfast provided by conference package" \
  --pack de

# Record a customer visit by car with 180 km round-trip distance
wt travel add \
  --title "Client Onboarding Workshop" \
  --destination "Nuremberg" \
  --country "DE" \
  --start 2026-11-04T07:30:00Z \
  --end 2026-11-04T18:00:00Z \
  --mode car \
  --km 180.0 \
  --lunch 2026-11-04 \
  --notes "Customer provided lunch in cafeteria" \
  --pack de

# List recorded business trips and calculated net reimbursements
wt travel list --pack de
```

______________________________________________________________________

## 5. Generating Your Annual Tax & Travel Report (`wt report`)

### Inspecting packs and generating reports

At the end of the calendar year or tax quarter, generate a consolidated report combining workplace days (`home_office`, `office`, `mobile_work`), statutory commute deductions, and business trip allowances.

```bash
# List available reporting packs and their statutory parameters
wt report packs

# Generate your annual report in Markdown, CSV, or JSON
wt report generate \
  --start 2026-01-01 \
  --end 2026-12-31 \
  --pack de \
  --subdivision DE-BY \
  --one-way-km 25.0 \
  --format markdown

# Export structured JSON Schema for third-party tax accounting integration
wt report schema
```

### Example Markdown Output

The generated report summarizes all statutory tallies and net allowances:

```markdown
# Annual Tax & Workplace Summary (2026)
- **Jurisdiction Pack**: Germany (EStG / Reisekostenrecht) (`de`)
- **Regional Holiday Calendar**: Bayern (`DE-BY`)
- **One-Way Commute Distance**: 25.0 km

## Workplace Classification & Commute Tallies
| Category | Days | Statutory Rate | Deductible Allowance |
| --- | --- | --- | --- |
| Home Office (Homeoffice-Pauschale) | 140 | 6.00 EUR / day | 840.00 EUR |
| Regular Office (Entfernungspauschale) | 65 | 25 km @ 0.30/0.38 EUR/km | 513.50 EUR |
| Mobile Work | 12 | — | — |

## Annual Leave & Non-Working Days
- **Annual Entitlement**: 30 days (+ 2 carryover = 32 total)
- **Taken Vacation**: 22 days (Remaining: 10 days)
- **Sick Leave**: 3 days
- **Weekday Public Holidays**: 11 days

## Business Travel (Reisekosten)
| Trip Title | Destination | Net Per-Diem | Mileage Allowance | Total |
| --- | --- | --- | --- | --- |
| Architecture Summit Berlin | Berlin (DE) | 50.40 EUR | 0.00 EUR | 50.40 EUR |
| Client Onboarding Workshop | Nuremberg (DE) | 2.80 EUR | 54.00 EUR | 56.80 EUR |
| **Total Travel Reimbursement** | | **53.20 EUR** | **54.00 EUR** | **107.20 EUR** |
```
