"""
Tax and benefit parameters, by year.

Every figure here was checked against published sources in October 2026.
Figures marked verify=True are provisional or came from secondary sources only.
Update this file each January; nothing else in the app hard-codes these numbers.
"""

RULES = {
    2026: {
        # ---------------- Netherlands ----------------
        "box3": {
            "exemption_per_person": 59_357,      # heffingsvrij vermogen
            "rate_bank": 0.0128,                 # provisional, final early 2027
            "rate_other": 0.0600,                # final
            "rate_debt": 0.0270,                 # provisional
            "debt_threshold_per_person": 3_800,
            "tax_rate": 0.36,
            "peildatum": "1 January",
            "source": "SRA / Belastingdienst, Jan 2026",
        },
        "kinderbijslag_quarter": {
            # per child per quarter by age band at start of quarter
            "H1": {"0-5": 295.07, "6-11": 358.30, "12-17": 421.53},
            "H2": {"0-5": 298.40, "6-11": 362.35, "12-17": 426.29},
            "verify": True,
            "source": "SVB amounts as reported Jan/Jul 2026; check svb.nl",
        },
        "kindgebonden_budget": {
            "per_child": 2_580,
            "extra_12_15": 703,
            "extra_16_17": 703,                  # not confirmed; treat as estimate
            "alo_kop_single_parent": 3_416,      # single-parent supplement
            "income_threshold_partner": 39_141,
            "income_threshold_single": 29_736,
            "taper": 0.076,
            "max_assets_single": 146_011,
            "max_assets_partner": 184_633,
            "verify": True,
            "source": "Rijksoverheid parameters 2026 via Knab/Consumentenbond; do a proefberekening at toeslagen.nl",
        },
        "expat_ruling": {
            "salary_norm": 48_013,               # taxable salary after allowance
            "salary_norm_u30_master": 36_497,
            "rate": 0.30,
            "rate_2027": 0.27,                   # for rulings first applied 2024+
            "salary_norm_2027_new": 50_436,      # 2024 price level, indexed; rulings from 2025+
            "max_allowance": 78_600,             # 30% of WNT cap 262,000
            "source": "Grant Thornton / Forvis Mazars / iamexpat 2026",
        },
        "nl_wage_tax": {
            # box 1, below AOW age. Rates include national insurance contributions.
            "brackets": [(38_883, 0.3575), (78_426, 0.3756), (None, 0.4950)],
            "ahk": {"max": 3_115, "phase_start": 29_736, "phase_rate": 0.06398},
            "arbeidskorting": [  # (band end, base, rate applied above band start, band start)
                (11_965, 0.0, 0.08324, 0),
                (25_845, 996.0, 0.31009, 11_965),
                (45_592, 5_300.0, 0.01950, 25_845),
                (132_920, 5_685.0, -0.06510, 45_592),
            ],
            "holiday_allowance": 0.08,
            "source": "Belastingdienst, Voorlopige aanslag 2026: tarieven en heffingskortingen",
        },
        # ---------------- United States ----------------
        "feie_max_usd": 132_900,
        "us_std_deduction": {"MFJ": 32_200, "single": 16_100, "MFS": 16_100, "HOH": 24_150},
        "ctc": {
            "per_child": 2_200,
            "refundable_cap": 1_700,
            "earned_income_floor": 2_500,
            "phase_in": 0.15,
            "phaseout_start": {"MFJ": 400_000, "single": 200_000, "MFS": 200_000, "HOH": 200_000},
            "source": "OBBBA (P.L. 119-21) sec. 70104; IRS Rev. Proc. 2025-32",
        },
        "fbar_threshold_usd": 10_000,
        "form8938_abroad": {
            # (year-end value, max value any time during year)
            "single": (200_000, 300_000),
            "MFS": (200_000, 300_000),
            "HOH": (200_000, 300_000),
            "MFJ": (400_000, 600_000),
        },
    }
}


def get(year: int) -> dict:
    """Return rules for the year, falling back to the latest known year."""
    if year in RULES:
        return RULES[year]
    latest = max(RULES)
    return RULES[latest]


def year_label(year: int) -> str:
    return str(year) if year in RULES else f"{max(RULES)} (latest known, {year} not loaded)"
