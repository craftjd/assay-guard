"""Same assay headers as test-config-1, but negatives are rejected."""

from app.config import test_config_1 as base

CONFIG_VERSION = "test-config-2"
MIN_ANALYTE = 0
CHANGE = "Analytes must be zero or greater. Below-detection negatives fail."

SYSTEM = base.SYSTEM
DATASET = base.DATASET
KEY_HEADER = base.KEY_HEADER
HOLE_HEADER = base.HOLE_HEADER
INTERVAL_FROM = base.INTERVAL_FROM
INTERVAL_TO = base.INTERVAL_TO
SAMPLE_TYPE_HEADER = base.SAMPLE_TYPE_HEADER
QAQC_TYPES = base.QAQC_TYPES
INTERVAL_TYPES = base.INTERVAL_TYPES
DUPLICATE_TYPES = base.DUPLICATE_TYPES
ELEMENTS = base.ELEMENTS
IDENTITY_HEADERS = base.IDENTITY_HEADERS
REQUIRED_HEADERS = base.REQUIRED_HEADERS
ANALYTE_HEADERS = base.ANALYTE_HEADERS


def summary() -> dict:
    data = base.summary()
    data["version"] = CONFIG_VERSION
    data["analyte_rule"] = "Zero or greater"
    data["change"] = CHANGE
    return data
