"""One assay predicate for classification, confirmation, and main checks.

Numbers compare as numbers. Plain lab text is kept when it already parses.
Declared headers are the only fields that count as the business payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import app.config as cfg

RULES_ID = "business-v1"
_PLAIN = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


@dataclass(frozen=True)
class Rules:
    key: str
    hole: str
    interval_from: str
    interval_to: str
    sample_type: str
    qaqc_types: tuple[str, ...]
    analytes: tuple[str, ...]
    required: tuple[str, ...]
    min_analyte: Decimal | None
    duplicate_types: tuple[str, ...] = ("DUP",)

    @property
    def numeric(self) -> frozenset[str]:
        return frozenset((self.interval_from, self.interval_to, *self.analytes))


def current_rules() -> Rules:
    minimum = cfg.MIN_ANALYTE
    return Rules(
        key=cfg.KEY_HEADER,
        hole=cfg.HOLE_HEADER,
        interval_from=cfg.INTERVAL_FROM,
        interval_to=cfg.INTERVAL_TO,
        sample_type=cfg.SAMPLE_TYPE_HEADER,
        qaqc_types=tuple(cfg.QAQC_TYPES),
        analytes=tuple(cfg.ANALYTE_HEADERS),
        required=tuple(cfg.REQUIRED_HEADERS),
        min_analyte=None if minimum is None else Decimal(str(minimum)),
        duplicate_types=tuple(getattr(cfg, "DUPLICATE_TYPES", ())),
    )


def as_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_decimal(value: str) -> Decimal | None:
    text = as_text(value)
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def format_decimal(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def plain_or_canonical(value: str) -> tuple[str, bool]:
    """Keep plain lab text. Rewrite scientific and odd forms. Return (stored, rewritten)."""
    number = parse_decimal(value)
    stripped = as_text(value)
    if number is None:
        return stripped, False
    if _PLAIN.fullmatch(stripped):
        return stripped, False
    canonical = format_decimal(number)
    return canonical, canonical != stripped


def business_values(
    payload: dict,
    rules: Rules,
    binding: dict[str, str] | None = None,
) -> tuple[dict[str, str], str | None]:
    """Configured headers only, in config order. Extras such as Note are omitted."""
    notes: list[str] = []
    values: dict[str, str] = {}
    for name in rules.required:
        source = binding[name] if binding else name
        text = as_text(payload.get(source))
        if name in rules.numeric and text:
            stored, rewritten = plain_or_canonical(text)
            values[name] = stored
            if rewritten:
                notes.append(f"{name} {text} read as {stored}")
        else:
            values[name] = text
    return values, ("; ".join(notes) or None)


def same_cell(name: str, left: str, right: str, rules: Rules) -> bool:
    if name not in rules.numeric:
        return left == right
    if not left and not right:
        return True
    left_n = parse_decimal(left)
    right_n = parse_decimal(right)
    if left_n is None or right_n is None:
        return left == right
    return left_n == right_n


def same_business(left: dict, right: dict, rules: Rules) -> bool:
    a, _ = business_values(left, rules)
    b, _ = business_values(right, rules)
    return all(same_cell(name, a[name], b[name], rules) for name in rules.required)


def field_changes(left: dict, right: dict, rules: Rules) -> list[tuple[str, str, str]]:
    """(header, left value, right value) for each configured header that differs."""
    before, _ = business_values(left, rules)
    after, _ = business_values(right, rules)
    return [
        (name, before[name], after[name])
        for name in rules.required
        if not same_cell(name, before[name], after[name], rules)
    ]


def field_diffs(left: dict, right: dict, rules: Rules) -> str:
    return "; ".join(
        f"{name}: {old or '(none)'} → {new or '(none)'}"
        for name, old, new in field_changes(left, right, rules)
    )


def is_qaqc(payload: dict, rules: Rules) -> bool:
    sample = as_text(payload.get(rules.sample_type)).upper()
    return sample in rules.qaqc_types


def failure_items(payload: dict, rules: Rules) -> list[tuple[str, str, tuple[str, ...]]]:
    """Each item is (check name, row reason, headers at fault). Empty means it passes."""
    found: list[tuple[str, str, tuple[str, ...]]] = []
    values, _ = business_values(payload, rules)
    if not values[rules.key]:
        found.append((f"{rules.key} is present", f"{rules.key} is blank", (rules.key,)))
    qaqc = is_qaqc(values, rules)
    if not qaqc and not values[rules.hole]:
        found.append(
            (
                f"{rules.hole} is present on interval samples",
                f"{rules.hole} is blank",
                (rules.hole,),
            )
        )
    for name in rules.analytes:
        reason = analyte_failure(name, values[name], rules.min_analyte)
        if reason:
            found.append((analyte_check_name(name, rules.min_analyte), reason, (name,)))
    interval = interval_failure(values, rules, qaqc)
    if interval:
        fields = (rules.interval_from, rules.interval_to)
        if interval.startswith(f"{rules.interval_from} is not"):
            fields = (rules.interval_from,)
        elif interval.startswith(f"{rules.interval_to} is not"):
            fields = (rules.interval_to,)
        found.append(
            (f"{rules.interval_from} is less than {rules.interval_to}", interval, fields)
        )
    return found


def failure_list(payload: dict, rules: Rules) -> list[tuple[str, str]]:
    """Each item is (check name, row reason). Empty means the payload passes."""
    return [(name, reason) for name, reason, _fields in failure_items(payload, rules)]


def failure_fields(payload: dict, rules: Rules) -> list[str]:
    fields: list[str] = []
    for _name, _reason, names in failure_items(payload, rules):
        fields.extend(name for name in names if name not in fields)
    return fields


def overlap_check_name(rules: Rules) -> str:
    return f"Intervals do not overlap within a {rules.hole}"


def interval_overlaps(rows: list[tuple[str, dict]], rules: Rules) -> dict[str, list[str]]:
    """Row key → keys on the same hole whose interval overlaps it.

    Standards, blanks, and field duplicates are left out. Touching ends do not overlap.
    """
    skipped = {name.upper() for name in (*rules.qaqc_types, *rules.duplicate_types)}
    by_hole: dict[str, list[tuple[Decimal, Decimal, str]]] = {}
    for key, payload in rows:
        values, _ = business_values(payload, rules)
        if as_text(values[rules.sample_type]).upper() in skipped:
            continue
        start = parse_decimal(values[rules.interval_from])
        end = parse_decimal(values[rules.interval_to])
        hole = values[rules.hole]
        if not hole or start is None or end is None or start >= end:
            continue
        by_hole.setdefault(hole, []).append((start, end, key))
    found: dict[str, list[str]] = {}
    for items in by_hole.values():
        items.sort()
        for index, (start, end, key) in enumerate(items):
            for other_start, _other_end, other in items[index + 1:]:
                if other_start >= end:
                    break
                found.setdefault(key, []).append(other)
                found.setdefault(other, []).append(key)
    return found


def invalid_reason(payload: dict, rules: Rules) -> str | None:
    found = failure_list(payload, rules)
    if not found:
        return None
    return found[0][1]


def key_match_failure(row_key: str, payload: dict, rules: Rules) -> str | None:
    values, _ = business_values(payload, rules)
    if values[rules.key] != as_text(row_key):
        return f"{rules.key} does not match the row key"
    return None


def analyte_failure(name: str, value: str, minimum: Decimal | None) -> str | None:
    if parse_decimal(value) is None:
        return f"{name} is not a number"
    if minimum is not None and parse_decimal(value) < minimum:
        return f"{name} must be {minimum} or greater"
    return None


def analyte_check_name(name: str, minimum: Decimal | None) -> str:
    if minimum is None:
        return f"{name} is a number"
    return f"{name} is a number and at least {minimum}"


def analyte_rule_text(name: str, minimum: Decimal | None) -> str:
    if minimum is None:
        return f"{name} parses as a number. Blank and non-numeric values fail."
    return f"{name} parses as a number and must be {minimum} or greater."


def interval_failure(payload: dict, rules: Rules, qaqc: bool) -> str | None:
    start = payload[rules.interval_from]
    end = payload[rules.interval_to]
    if qaqc and not start and not end:
        return None
    start_n = parse_decimal(start)
    end_n = parse_decimal(end)
    if start_n is None:
        return f"{rules.interval_from} is not a number"
    if end_n is None:
        return f"{rules.interval_to} is not a number"
    if start_n >= end_n:
        return f"{rules.interval_from} must be less than {rules.interval_to}"
    return None


def check_catalog(rules: Rules) -> list[tuple[str, str, tuple[str, ...]]]:
    """Check name, the sentence it enforces, and the headers it reads."""
    interval = (rules.hole, rules.sample_type, rules.interval_from, rules.interval_to)
    catalog = [
        (
            f"{rules.key} is present",
            f"{rules.key} is required on every row.",
            (rules.key,),
        ),
        (
            f"{rules.key} matches the row key",
            f"The stored {rules.key} equals the main row key.",
            (rules.key,),
        ),
        (
            f"{rules.hole} is present on interval samples",
            f"{rules.hole} is required unless the sample type is "
            f"{', '.join(rules.qaqc_types)}.",
            (rules.hole, rules.sample_type),
        ),
        (
            f"{rules.interval_from} is less than {rules.interval_to}",
            f"{rules.interval_from} and {rules.interval_to} parse as numbers and "
            f"the from is less than the to. "
            f"{', '.join(rules.qaqc_types)} may omit both.",
            interval,
        ),
    ]
    for name in rules.analytes:
        catalog.append(
            (
                analyte_check_name(name, rules.min_analyte),
                analyte_rule_text(name, rules.min_analyte),
                (rules.sample_type, name),
            )
        )
    catalog.append(
        (
            overlap_check_name(rules),
            f"Two samples on the same {rules.hole} do not cover the same metres. "
            f"Ends that only touch (one sample ends at 6.0 m, the next starts at 6.0 m) are fine. "
            f"Checked across every file in main. "
            f"{', '.join((*rules.qaqc_types, *rules.duplicate_types))} are left out.",
            interval,
        )
    )
    return catalog


def duplicate_notes(occurrences: dict[str, list[int]], key_header: str) -> dict[int, str]:
    """Map source row number → reason. Every copy of a repeated key is named."""
    notes: dict[int, str] = {}
    for rows in occurrences.values():
        if len(rows) < 2:
            continue
        for row in rows:
            others = [str(other) for other in rows if other != row]
            noun = "row" if len(others) == 1 else "rows"
            notes[row] = f"duplicate {key_header}; also on {noun} {', '.join(others)}"
    return notes


def classify(
    payload: dict,
    main_payload: dict | None,
    duplicate_reason: str | None,
    rules: Rules,
) -> tuple[str, str | None, str | None]:
    key = as_text(payload.get(rules.key)) or None
    content = invalid_reason(payload, rules)
    reason = _join(content, duplicate_reason)
    if content or duplicate_reason:
        return "invalid", reason, key
    if main_payload is None:
        return "good", None, key
    if same_business(main_payload, payload, rules):
        return "already_same", None, key
    return "changed", "values differ from main", key


def absent_keys(group_main: set[str], file_keys: set[str]) -> list[str]:
    return sorted(key for key in group_main - file_keys if key)


def key_digest(keys: list[str]) -> str:
    import hashlib

    body = "\n".join(sorted(keys))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def payload_digest(payload: dict) -> str:
    import hashlib
    import json

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _join(content: str | None, duplicate: str | None) -> str | None:
    parts = [part for part in (content, duplicate) if part]
    if not parts:
        return None
    return "; ".join(parts)
