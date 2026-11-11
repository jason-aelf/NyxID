import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from coverage_summary import line_pct


SCRIPTS = Path(__file__).resolve().parent


def cargo_report(value):
    return {"data": [{"totals": {"lines": {"percent": value}}}]}


def vitest_report(value):
    return {"total": {"lines": {"pct": value}}}


REPORTS = (cargo_report, vitest_report)
INVALID_PERCENTAGES = (
    True, False, None, "Unknown", "not a number", "", [], {},
    float("nan"), float("inf"), float("-inf"),
    "NaN", "Infinity", "-Infinity", "1e9999", 10 ** 400,
    -0.001, 100.001, -1, 101, "-1", "101",
)
MALFORMED_REPORTS = (
    None, True, 1, "report", [], {},
    {"data": None}, {"data": {}}, {"data": []},
    {"data": [None]}, {"data": [[]]}, {"data": ["entry"]},
    {"data": [{}]}, {"data": [{"totals": None}]},
    {"data": [{"totals": []}]}, {"data": [{"totals": "totals"}]},
    {"data": [{"totals": {}}]},
    {"data": [{"totals": {"lines": None}}]},
    {"data": [{"totals": {"lines": []}}]},
    {"data": [{"totals": {"lines": "percent"}}]},
    {"data": [{"totals": {"lines": {}}}]},
    {"total": None}, {"total": []}, {"total": "total"},
    {"total": {}}, {"total": {"lines": None}},
    {"total": {"lines": []}}, {"total": {"lines": "pct"}},
    {"total": {"lines": {}}},
)


def reject_constant(value):
    raise ValueError(f"non-JSON numeric constant: {value}")


class CoveragePercentageTests(unittest.TestCase):
    def test_finite_boundaries_and_numeric_strings_are_preserved(self):
        for report in REPORTS:
            for value in (0, 100, -0.0, 0.001, 99.999, 73.456,
                          "0", "100", " 75.25 ", "1e1"):
                with self.subTest(format=report.__name__, value=value):
                    actual = line_pct(report(value))
                    self.assertIsInstance(actual, float)
                    self.assertTrue(math.isfinite(actual))
                    self.assertEqual(actual, float(value))

    def test_invalid_percentages_normalize_to_zero_in_both_formats(self):
        for report in REPORTS:
            for value in INVALID_PERCENTAGES:
                with self.subTest(format=report.__name__, value=value):
                    self.assertEqual(line_pct(report(value)), 0.0)

    def test_malformed_nesting_has_controlled_errors(self):
        for doc in MALFORMED_REPORTS:
            with self.subTest(doc=doc):
                with self.assertRaises((ValueError, SystemExit)):
                    line_pct(doc)

    def test_selected_cargo_field_never_falls_back_to_vitest(self):
        for value in INVALID_PERCENTAGES:
            with self.subTest(value=value):
                self.assertEqual(line_pct({**cargo_report(value), **vitest_report(100)}), 0.0)
        self.assertEqual(line_pct({**cargo_report(25), **vitest_report(100)}), 25.0)

    def test_malformed_selected_cargo_report_is_not_hidden_by_vitest(self):
        for doc in MALFORMED_REPORTS:
            if isinstance(doc, dict) and "data" in doc:
                with self.subTest(doc=doc):
                    with self.assertRaises((ValueError, SystemExit)):
                        line_pct({**doc, **vitest_report(100)})


class CoverageCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.report = self.root / "report.json"
        self.summary = self.root / "summary.json"

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / name), *map(str, args)],
            capture_output=True, text=True, timeout=30,
        )

    def read_summary(self):
        return json.loads(self.summary.read_text(encoding="utf-8"),
                          parse_constant=reject_constant)

    def test_summary_cli_preserves_metadata_boundaries_and_rounding(self):
        for report, component, label, threshold in (
            (cargo_report, "backend", "Backend (nyxid)", 73),
            (cargo_report, "cli", "CLI (nyxid-cli)", 64),
            (vitest_report, "frontend", "Frontend (vitest)", 15),
        ):
            for value, expected in ((0, 0.0), ("100", 100.0), ("73.456", 73.46)):
                with self.subTest(component=component, value=value):
                    self.report.write_text(json.dumps(report(value)), encoding="utf-8")
                    result = self.run_script("coverage_summary.py", component, label,
                                             threshold, self.report, self.summary)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.read_summary(), {
                        "component": component, "label": label, "threshold": threshold,
                        "head_pct": expected, "base_pct": None,
                    })

    def test_summary_cli_emits_strict_json_for_invalid_percentages(self):
        for report in REPORTS:
            for value in INVALID_PERCENTAGES:
                with self.subTest(format=report.__name__, value=value):
                    # Include Python's permissive NaN/Infinity JSON inputs too.
                    self.report.write_text(json.dumps(report(value)), encoding="utf-8")
                    result = self.run_script("coverage_summary.py", "frontend", "Frontend",
                                             15, self.report, self.summary)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    summary = self.read_summary()
                    self.assertEqual(summary["head_pct"], 0.0)
                    self.assertLess(summary["head_pct"], summary["threshold"])
                    self.assertIsNone(summary["base_pct"])

    def test_summary_cli_rejects_malformed_reports_without_type_errors(self):
        for doc in MALFORMED_REPORTS:
            with self.subTest(doc=doc):
                self.report.write_text(json.dumps(doc), encoding="utf-8")
                result = self.run_script("coverage_summary.py", "backend", "Backend",
                                         73, self.report, self.summary)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ValueError", result.stderr)
                self.assertNotIn("AttributeError", result.stderr)
                self.assertNotIn("TypeError", result.stderr)
                self.assertFalse(self.summary.exists())

    def merge_base(self):
        # A stale base value must also be cleared when the report is unavailable.
        head = {"component": "backend", "label": "Backend", "threshold": 73,
                "head_pct": 80.0, "base_pct": 99.0}
        self.summary.write_text(json.dumps(head), encoding="utf-8")
        result = self.run_script("coverage_merge_base.py", self.summary, self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self.read_summary()
        self.assertEqual({key: value for key, value in summary.items() if key != "base_pct"},
                         {key: value for key, value in head.items() if key != "base_pct"})
        return summary["base_pct"]

    def test_missing_and_structurally_invalid_base_reports_remain_unavailable(self):
        self.assertIsNone(self.merge_base())
        for doc in MALFORMED_REPORTS:
            with self.subTest(doc=doc):
                self.report.write_text(json.dumps(doc), encoding="utf-8")
                self.assertIsNone(self.merge_base())
        self.report.write_text("invalid json", encoding="utf-8")
        self.assertIsNone(self.merge_base())

    def test_base_merge_rounds_valid_values_and_normalizes_invalid_fields(self):
        for report in REPORTS:
            for value, expected in ((0, 0.0), ("100", 100.0), (73.456, 73.46)):
                with self.subTest(format=report.__name__, value=value):
                    self.report.write_text(json.dumps(report(value)), encoding="utf-8")
                    self.assertEqual(self.merge_base(), expected)
            for value in INVALID_PERCENTAGES:
                with self.subTest(format=report.__name__, value=value):
                    self.report.write_text(json.dumps(report(value)), encoding="utf-8")
                    self.assertEqual(self.merge_base(), 0.0)


if __name__ == "__main__":
    unittest.main()
