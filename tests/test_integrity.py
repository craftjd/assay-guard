"""Rules and CSV parsing. No database."""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from app.services.diff import count_kinds, kinds_phrase, run_changes, version_changes
from app.services.raw_import import parse_csv
from app.services.rules import (
    Rules,
    absent_keys,
    business_values,
    classify,
    current_rules,
    duplicate_notes,
    failure_fields,
    failure_list,
    interval_overlaps,
    field_changes,
    field_diffs,
    invalid_reason,
    key_digest,
    same_business,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

RULES = Rules(
    key="SampleID",
    hole="HoleID",
    interval_from="SampFrom",
    interval_to="SampTo",
    sample_type="SampleType",
    qaqc_types=("STD", "BLK"),
    analytes=("Au_ppm",),
    required=(
        "SampleID",
        "HoleID",
        "SampFrom",
        "SampTo",
        "SampleType",
        "Au_ppm",
    ),
    min_analyte=None,
)
STRICT = Rules(
    key=RULES.key,
    hole=RULES.hole,
    interval_from=RULES.interval_from,
    interval_to=RULES.interval_to,
    sample_type=RULES.sample_type,
    qaqc_types=RULES.qaqc_types,
    analytes=RULES.analytes,
    required=RULES.required,
    min_analyte=Decimal("0"),
)


def _row(**values: str) -> dict[str, str]:
    base = {name: "" for name in RULES.required}
    base.update(values)
    return base


class RulesTest(unittest.TestCase):
    def test_plain_decimals_match_and_keep_lab_text(self) -> None:
        left = _row(SampleID="A", HoleID="H", SampFrom="0.0", SampTo="1.5", SampleType="CORE", Au_ppm="5.10")
        right = _row(SampleID="A", HoleID="H", SampFrom="0", SampTo="1.50", SampleType="CORE", Au_ppm="5.1")
        self.assertTrue(same_business(left, right, RULES))
        self.assertEqual(field_diffs(left, right, RULES), "")
        stored, note = business_values(left, RULES)
        self.assertEqual(stored["Au_ppm"], "5.10")
        self.assertIsNone(note)

    def test_field_changes_list_only_configured_headers_that_differ(self) -> None:
        main = _row(SampleID="A", HoleID="H", SampFrom="0", SampTo="1", SampleType="CORE", Au_ppm="5.12")
        incoming = dict(main, Au_ppm="6.40", SampTo="1.0", Note="rechecked")
        self.assertEqual(field_changes(main, incoming, RULES), [("Au_ppm", "5.12", "6.40")])
        self.assertEqual(field_diffs(main, incoming, RULES), "Au_ppm: 5.12 → 6.40")

    def test_overlap_skips_qaqc_duplicates_and_touching_ends(self) -> None:
        rows = [
            ("A", _row(SampleID="A", HoleID="H1", SampFrom="4.5", SampTo="6.0", SampleType="CORE")),
            ("B", _row(SampleID="B", HoleID="H1", SampFrom="6.0", SampTo="7.5", SampleType="CORE")),
            ("C", _row(SampleID="C", HoleID="H1", SampFrom="5.0", SampTo="6.5", SampleType="CORE")),
            ("D", _row(SampleID="D", HoleID="H1", SampFrom="6.0", SampTo="7.5", SampleType="DUP")),
            ("E", _row(SampleID="E", HoleID="H2", SampFrom="5.0", SampTo="6.5", SampleType="CORE")),
            ("S", _row(SampleID="S", SampleType="STD")),
        ]
        found = interval_overlaps(rows, RULES)
        self.assertEqual(sorted(found), ["A", "B", "C"])
        self.assertEqual(sorted(found["C"]), ["A", "B"])
        self.assertEqual(found["A"], ["C"])

    def test_failure_fields_name_the_headers_at_fault(self) -> None:
        reversed_interval = _row(SampleID="A", HoleID="H", SampFrom="4.5", SampTo="3.0", SampleType="CORE", Au_ppm="1")
        self.assertEqual(failure_fields(reversed_interval, RULES), ["SampFrom", "SampTo"])
        bad_gold = _row(SampleID="A", HoleID="", SampFrom="0", SampTo="1", SampleType="CORE", Au_ppm="n/a")
        self.assertEqual(failure_fields(bad_gold, RULES), ["HoleID", "Au_ppm"])
        good = _row(SampleID="A", HoleID="H", SampFrom="0", SampTo="1", SampleType="CORE", Au_ppm="1")
        self.assertEqual(failure_fields(good, RULES), [])

    def test_extra_column_is_not_part_of_the_business_payload(self) -> None:
        payload = _row(
            SampleID="A",
            HoleID="H",
            SampFrom="0",
            SampTo="1",
            SampleType="CORE",
            Au_ppm="1.0",
        )
        payload["Note"] = "check"
        stored, _note = business_values(payload, RULES)
        self.assertNotIn("Note", stored)
        self.assertTrue(same_business(payload, _row(**{k: stored[k] for k in RULES.required}), RULES))

    def test_scientific_notation_is_rewritten_and_noted(self) -> None:
        payload = _row(
            SampleID="A",
            HoleID="H",
            SampFrom="0",
            SampTo="1",
            SampleType="CORE",
            Au_ppm="1e-3",
        )
        stored, note = business_values(payload, RULES)
        self.assertEqual(stored["Au_ppm"], "0.001")
        self.assertIn("1e-3 read as 0.001", note or "")

    def test_negative_fails_only_when_the_minimum_is_set(self) -> None:
        payload = _row(
            SampleID="A",
            HoleID="H",
            SampFrom="0",
            SampTo="1",
            SampleType="CORE",
            Au_ppm="-0.01",
        )
        self.assertIsNone(invalid_reason(payload, RULES))
        self.assertIn("must be 0 or greater", invalid_reason(payload, STRICT) or "")
        names = [name for name, _reason in failure_list(payload, STRICT)]
        self.assertIn("Au_ppm is a number and at least 0", names)

    def test_standard_may_omit_hole_and_interval(self) -> None:
        payload = _row(SampleID="STD-1", SampleType="STD", Au_ppm="1.2")
        self.assertIsNone(invalid_reason(payload, RULES))

    def test_both_copies_of_a_duplicate_are_named(self) -> None:
        notes = duplicate_notes({"ACQ00903": [5, 6]}, "SampleID")
        self.assertEqual(notes[5], "duplicate SampleID; also on row 6")
        self.assertEqual(notes[6], "duplicate SampleID; also on row 5")
        payload = _row(
            SampleID="ACQ00903",
            HoleID="H",
            SampFrom="1.0",
            SampTo="2.0",
            SampleType="CORE",
            Au_ppm="0.55",
        )
        outcome, reason, key = classify(payload, None, notes[5], RULES)
        self.assertEqual(outcome, "invalid")
        self.assertEqual(key, "ACQ00903")
        self.assertIn("row 6", reason or "")

    def test_content_error_keeps_the_duplicate_note(self) -> None:
        payload = _row(SampleID="A", HoleID="H", SampFrom="4", SampTo="3", SampleType="CORE", Au_ppm="1")
        outcome, reason, _key = classify(payload, None, "duplicate SampleID; also on row 4", RULES)
        self.assertEqual(outcome, "invalid")
        self.assertIn("SampFrom must be less than SampTo", reason or "")
        self.assertIn("also on row 4", reason or "")

    def test_absent_keys_are_the_group_members_missing_from_the_file(self) -> None:
        self.assertEqual(absent_keys({"A", "B", "C"}, {"A", "C"}), ["B"])

    def test_digest_depends_on_the_key_set_not_the_order(self) -> None:
        self.assertEqual(key_digest(["b", "a"]), key_digest(["a", "b"]))
        self.assertNotEqual(key_digest(["a"]), key_digest(["a", "b"]))


class CsvTest(unittest.TestCase):
    def test_short_row_names_the_file_row(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_csv(b"A,B\n1,2\n\n3\n")
        self.assertIn("row 4", str(caught.exception))
        self.assertIn("expected 2", str(caught.exception))

    def test_blank_lines_are_counted_when_the_file_parses(self) -> None:
        parsed = parse_csv(b"A,B\n1,2\n\n3,4\n")
        self.assertEqual(parsed.blank_rows, 1)
        self.assertEqual([row for row, _payload in parsed.rows], [2, 4])

    def test_reordered_fixture_binds_by_name_and_drops_note(self) -> None:
        rules = current_rules()
        parsed = parse_csv((FIXTURES / "gold_proj_reordered.csv").read_bytes())
        by_lower = {header.lower(): header for header in parsed.headers}
        binding = {name: by_lower[name.lower()] for name in rules.required}
        values, _note = business_values(parsed.rows[0][1], rules, binding)
        self.assertEqual(values["SampleID"], "ACQ00101")
        self.assertEqual(values["Au_ppm"], "-0.01")
        self.assertNotIn("Note", values)
        original = parse_csv((FIXTURES / "gold_proj_despatch.csv").read_bytes())
        original_binding = {
            name: name for name in rules.required
        }
        original_values, _note = business_values(original.rows[0][1], rules, original_binding)
        self.assertTrue(same_business(values, original_values, rules))

    def test_bad_rows_name_both_duplicate_sample_rows(self) -> None:
        parsed = parse_csv((FIXTURES / "gold_proj_bad_rows.csv").read_bytes())
        rules = current_rules()
        binding = {name: name for name in rules.required}
        occurrences: dict[str, list[int]] = {}
        for source, payload in parsed.rows:
            values, _note = business_values(payload, rules, binding)
            key = values["SampleID"]
            if key:
                occurrences.setdefault(key, []).append(source)
        notes = duplicate_notes(occurrences, rules.key)
        self.assertEqual(occurrences["ACQ00903"], [5, 6])
        self.assertIn("row 6", notes[5])
        self.assertIn("row 5", notes[6])


def _stored(key: str, outcome: str, reason: str = "", **values: str) -> dict:
    payload = _row(SampleID=key, HoleID="H", SampFrom="0", SampTo="1", SampleType="CORE", Au_ppm="1.0")
    payload.update(values)
    return {"row_key": key, "outcome": outcome, "reason": reason, "payload": payload}


class RunChangesTest(unittest.TestCase):
    def test_identical_results_are_not_a_change(self) -> None:
        rows = {1: _stored("A", "already_same"), 2: _stored("B", "invalid", "Au_ppm is blank", Au_ppm="")}
        changes, settled = run_changes(rows, dict(rows), RULES, {}, {7})
        self.assertEqual(changes, [])
        self.assertEqual(settled, 0)

    def test_rows_settled_by_this_imports_own_save_are_not_a_change(self) -> None:
        before = {1: _stored("A", "good")}
        after = {1: _stored("A", "already_same")}
        changes, settled = run_changes(before, after, RULES, {"A": {"import_id": 7}}, {7})
        self.assertEqual(changes, [])
        self.assertEqual(settled, 1)

    def test_same_result_from_another_imports_save_is_a_change(self) -> None:
        before = {1: _stored("A", "good")}
        after = {1: _stored("A", "already_same")}
        changes, _settled = run_changes(before, after, RULES, {"A": {"import_id": 9}}, {7})
        self.assertEqual([change["kind"] for change in changes], ["main"])

    def test_rule_change_is_reported_as_now_fails(self) -> None:
        before = {1: _stored("A", "already_same")}
        after = {1: _stored("A", "invalid", "Au_ppm is below 0")}
        changes, _settled = run_changes(before, after, RULES, {}, {7})
        self.assertEqual(changes[0]["kind"], "now_fails")
        self.assertEqual(kinds_phrase(count_kinds(changes)), "1 now fails the config")

    def test_version_changes_lists_new_values_added_and_dropped_keys(self) -> None:
        old = [_stored("A", "good"), _stored("B", "good")]
        new = [_stored("A", "good", Au_ppm="2.0"), _stored("C", "good")]
        found = {row["row_key"]: row for row in version_changes(old, new, RULES)}
        self.assertEqual(found["A"]["kind"], "values")
        self.assertEqual(found["A"]["changes"][0]["field"], "Au_ppm")
        self.assertEqual(found["B"]["kind"], "removed")
        self.assertEqual(found["C"]["kind"], "added")


if __name__ == "__main__":
    unittest.main()
