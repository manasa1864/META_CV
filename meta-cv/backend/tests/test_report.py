"""The evaluation report: organised case by case, with no overall pass/fail verdict (free sections only - no model, no network)."""
from app import report


def test_the_free_sections_list_every_case_and_the_report_has_no_overall_verdict():
    report.DATA.clear()
    sections = [("safety", report.safety_cases()), ("abuse", report.abuse_section())]
    text = report.render(sections, 0.0, True)
    assert "Overall" not in text and "PASS" not in text and "FAIL" not in text
    for case_id in ("inj-01", "hi-07", "pii-05", "ben-10"):      # one row per case, by id
        assert f"| {case_id} |" in text
    assert text.count("| attacker |") == 8 and text.count("| honest |") == 5          # every campaign fact, with and without trust
    assert "A page on another site posts a question" in text and "Rate limit 3 per minute" in text
    assert "What was measured for each mechanism" in text and "Limits of this evaluation" in text
    assert all(row[3] == report.OK for row in report.DATA["abuse"])


def test_helpers():
    assert report.cut("a  b\nc", 10) == "a b c" and report.cut("x" * 30, 10).endswith("…") and len(report.cut("x" * 30, 10)) == 10
    assert report._humanize("test_a_b[case x]") == "A b [case x]"
    assert report.table(["a", "b"], [["x|y", 1]])[2] == r"| x\|y | 1 |"
