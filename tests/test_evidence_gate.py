"""Release must fail closed when real integration evidence disappears."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "evidence", Path(__file__).resolve().parents[1] / "scripts/check_test_evidence.py"
)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.mark.parametrize(
    "cases,required,accepted",
    [
        ('<testcase name="real"/>', ["real"], True),
        ('<testcase name="real"><skipped/></testcase>', ["real"], False),
        ('<testcase name="real"><failure/></testcase>', ["real"], False),
        ('<testcase name="real"><error/></testcase>', ["real"], False),
        ('<testcase name="unit"/>', ["real"], False),
        ("", [], False),
        ('<testcase name="real[a]"/><testcase name="real[b]"><skipped/></testcase>', ["real"], False),
    ],
)
def test_missing_failed_skipped_or_empty_evidence_blocks_release(tmp_path, cases, required, accepted):
    report = tmp_path / "evidence.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    assert evidence.evaluate(report, required)["accepted"] is accepted


@pytest.mark.parametrize("content", [None, "not xml"])
def test_missing_or_corrupt_report_exits_unsuccessfully(tmp_path, monkeypatch, content):
    report = tmp_path / "evidence.xml"
    if content is not None:
        report.write_text(content)
    monkeypatch.setattr("sys.argv", ["check_test_evidence.py", str(report)])
    assert evidence.main() == 1
