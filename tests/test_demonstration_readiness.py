"""Prevent prerequisite evidence from silently accepting skips or missing stages."""

import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "demonstration_readiness", Path("scripts/demonstration_readiness.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_missing_skipped_failed_and_crashed_evidence_never_passes(tmp_path):
    xml = tmp_path / "evidence.xml"
    assert module.classify(xml, 0, ["stage"])["status"] == "failed"
    xml.write_text('<testsuite><testcase name="stage"><skipped/></testcase></testsuite>')
    assert module.classify(xml, 0, ["stage"])["status"] == "blocked"
    xml.write_text('<testsuite><testcase name="stage"><failure/></testcase></testsuite>')
    assert module.classify(xml, 0, ["stage"])["status"] == "failed"
    xml.write_text('<testsuite><testcase name="stage"/></testsuite>')
    assert module.classify(xml, 0, ["missing"])["status"] == "failed"
    assert module.classify(xml, 1, ["stage"])["status"] == "failed"
    assert module.classify(xml, 0, ["stage"])["status"] == "passed"
    xml.write_text(
        '<testsuite><testcase name="stage[a]"/><testcase name="stage[b]"><skipped/></testcase></testsuite>'
    )
    assert module.classify(xml, 0, ["stage"])["status"] == "blocked"
