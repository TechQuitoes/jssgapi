"""RTO-aware helpers shared by the geo API and Group-ID resolution.

The checked-in ``app.core.rto_codes`` mapping is the source of truth for the
city code that goes into a Group ID. Most cities resolve through
``get_rto_code``; a few LGD rows need an explicit alias because their names do
not contain the RTO's city/zone name (notably Delhi's municipal corporations).
"""

from __future__ import annotations

from app.core.rto_codes import RTO_BY_STATE, get_rto_code

#: State/UT codes covered by the checked-in RTO mapping.
RTO_STATE_CODES: frozenset[str] = frozenset(RTO_BY_STATE)

#: LGD city code -> RTO code for rows whose names cannot be matched directly.
#: Delhi's LGD layer uses municipal corporations rather than the RTO zones.
DELHI_RTO_OVERRIDES: dict[int, str] = {
    276400: "07",  # East Delhi Municipal Corporation -> DL07
    276401: "06",  # North Delhi Municipal Corporation -> DL06
    276402: "03",  # South Delhi Municipal Corporation -> DL03
    276447: "02",  # New Delhi Municipal Council -> DL02
}


STATE_NAME_TO_CODE: dict[str, str] = {
    "jammu and kashmir": "JK",
    "himachal pradesh": "HP",
    "punjab": "PB",
    "chandigarh": "CH",
    "uttarakhand": "UK",
    "haryana": "HR",
    "delhi": "DL",
    "rajasthan": "RJ",
    "uttar pradesh": "UP",
    "bihar": "BR",
    "sikkim": "SK",
    "arunachal pradesh": "AR",
    "nagaland": "NL",
    "manipur": "MN",
    "mizoram": "MZ",
    "tripura": "TR",
    "meghalaya": "ML",
    "assam": "AS",
    "west bengal": "WB",
    "jharkhand": "JH",
    "odisha": "OD",
    "chhattisgarh": "CG",
    "madhya pradesh": "MP",
    "gujarat": "GJ",
    "maharashtra": "MH",
    "andhra pradesh": "AP",
    "karnataka": "KA",
    "goa": "GA",
    "lakshadweep": "LD",
    "kerala": "KL",
    "tamil nadu": "TN",
    "puducherry": "PY",
    "andaman and nicobar islands": "AN",
    "telangana": "TG",
    "ladakh": "LA",
    "the dadra and nagar haveli and daman and diu": "DD",
}

STATE_CODE_ALIASES: dict[str, str] = {
    "TS": "TG",
}

STATE_DISTRICT_RTO_MAP: dict[tuple[str, str], tuple[str, str]] = {
    # Madhya Pradesh (MP)
    ("madhya pradesh", "indore"): ("MP", "09"),
    ("madhya pradesh", "bhopal"): ("MP", "04"),
    ("madhya pradesh", "ujjain"): ("MP", "13"),
    ("madhya pradesh", "gwalior"): ("MP", "07"),
    ("madhya pradesh", "jabalpur"): ("MP", "20"),
    ("madhya pradesh", "ratlam"): ("MP", "43"),
    ("madhya pradesh", "khandwa"): ("MP", "12"),
    ("madhya pradesh", "khargone"): ("MP", "10"),
    ("madhya pradesh", "dhar"): ("MP", "11"),
    ("madhya pradesh", "dewas"): ("MP", "41"),
    ("mp", "indore"): ("MP", "09"),
    ("mp", "bhopal"): ("MP", "04"),
    ("mp", "ujjain"): ("MP", "13"),
    ("mp", "gwalior"): ("MP", "07"),
    ("mp", "jabalpur"): ("MP", "20"),
    ("mp", "ratlam"): ("MP", "43"),
    ("mp", "khandwa"): ("MP", "12"),
    ("mp", "khargone"): ("MP", "10"),
    ("mp", "dhar"): ("MP", "11"),
    ("mp", "dewas"): ("MP", "41"),
    # Rajasthan (RJ)
    ("rajasthan", "jaipur"): ("RJ", "14"),
    ("rajasthan", "jodhpur"): ("RJ", "19"),
    ("rajasthan", "udaipur"): ("RJ", "27"),
    ("rj", "jaipur"): ("RJ", "14"),
    ("rj", "jodhpur"): ("RJ", "19"),
    ("rj", "udaipur"): ("RJ", "27"),
    # Gujarat (GJ)
    ("gujarat", "ahmedabad"): ("GJ", "01"),
    ("gujarat", "surat"): ("GJ", "05"),
    ("gj", "ahmedabad"): ("GJ", "01"),
    ("gj", "surat"): ("GJ", "05"),
    # Maharashtra (MH)
    ("maharashtra", "mumbai"): ("MH", "01"),
    ("maharashtra", "pune"): ("MH", "12"),
    ("mh", "mumbai"): ("MH", "01"),
    ("mh", "pune"): ("MH", "12"),
}


def get_state_district_code(state: str, city: str) -> tuple[str, str]:
    """Map (state, city) into (state_code, rto_code).

    Fallback: Extract 2-letter uppercase state initials + default '01'.
    """
    state_norm = state.strip().lower()
    city_norm = city.strip().lower()
    key = (state_norm, city_norm)
    if key in STATE_DISTRICT_RTO_MAP:
        return STATE_DISTRICT_RTO_MAP[key]

    state_code = STATE_NAME_TO_CODE.get(state_norm)
    if not state_code:
        if len(state_norm) == 2 and state_norm.isalpha():
            state_code = state_norm.upper()
        else:
            words = state_norm.split()
            if len(words) >= 2:
                state_code = (words[0][0] + words[1][0]).upper()
            elif len(state_norm) >= 2:
                state_code = state_norm[:2].upper()
            else:
                state_code = "MP"

    state_code = STATE_CODE_ALIASES.get(state_code, state_code)
    rto = rto_code_for_city(state_code, city)
    if rto:
        return state_code, rto.zfill(2)

    return state_code, "01"


def rto_code_for_city(
    state_code: str,
    city_name: str,
    lgd_code: int | None = None,
) -> str | None:
    """Return the two-digit RTO code for a city, or ``None`` when unmapped.

    ``lgd_code`` is optional; when supplied it is used for the explicit alias
    overrides before falling back to the normal name-based lookup.
    """
    state_code = state_code.strip().upper()
    state_code = STATE_CODE_ALIASES.get(state_code, state_code)
    if state_code == "DL" and lgd_code is not None:
        override = DELHI_RTO_OVERRIDES.get(lgd_code)
        if override is not None:
            return override
    return get_rto_code(state_code, city_name)
