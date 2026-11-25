"""acQuire assay import for a gold-project despatch.

Header names bind the lab file. Column order does not.
Standards and blanks (STD, BLK) may omit the hole and the interval.
"""

CONFIG_VERSION = "test-config-1"
MIN_ANALYTE = None
CHANGE = "Below-detection negatives stay numeric."
SYSTEM = "acQuire"
DATASET = "Assays"

KEY_HEADER = "SampleID"
HOLE_HEADER = "HoleID"
INTERVAL_FROM = "SampFrom"
INTERVAL_TO = "SampTo"
SAMPLE_TYPE_HEADER = "SampleType"
QAQC_TYPES = ("STD", "BLK")
INTERVAL_TYPES = ("CORE", "DUP", "RC")
# Field duplicates repeat their parent interval, so they are left out of the overlap check.
DUPLICATE_TYPES = ("DUP",)

ELEMENTS = (
    {"symbol": "Au", "name": "Gold", "header": "Au_ppm", "unit": "ppm"},
    {"symbol": "Cu", "name": "Copper", "header": "Cu_pct", "unit": "pct"},
    {"symbol": "Ag", "name": "Silver", "header": "Ag_ppm", "unit": "ppm"},
    {"symbol": "As", "name": "Arsenic", "header": "As_ppm", "unit": "ppm"},
)

IDENTITY_HEADERS = (
    "ProjectCode",
    SAMPLE_TYPE_HEADER,
    "DespatchNo",
    "LabJobNo",
)

REQUIRED_HEADERS = (
    KEY_HEADER,
    HOLE_HEADER,
    *IDENTITY_HEADERS[:1],
    INTERVAL_FROM,
    INTERVAL_TO,
    *IDENTITY_HEADERS[1:],
    *(element["header"] for element in ELEMENTS),
)

ANALYTE_HEADERS = tuple(element["header"] for element in ELEMENTS)


def summary() -> dict:
    return {
        "version": CONFIG_VERSION,
        "system": SYSTEM,
        "dataset": DATASET,
        "key": KEY_HEADER,
        "hole": HOLE_HEADER,
        "interval_from": INTERVAL_FROM,
        "interval_to": INTERVAL_TO,
        "sample_type": SAMPLE_TYPE_HEADER,
        "interval_types": ", ".join(INTERVAL_TYPES),
        "qaqc_types": ", ".join(QAQC_TYPES),
        "duplicate_types": ", ".join(DUPLICATE_TYPES),
        "elements": list(ELEMENTS),
        "required_headers": list(REQUIRED_HEADERS),
        "header_match": "Header name",
        "analyte_rule": "Any number",
        "change": CHANGE,
    }
