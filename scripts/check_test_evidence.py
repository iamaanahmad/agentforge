"""Fail closed on absent, failed, skipped, or incomplete required JUnit evidence."""

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def evaluate(path, required=()):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    passed = set()
    for case in cases:
        name = case.get("name", "").split("[")[0]
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
            passed.add(name)
    missing = sorted(set(required) - passed)
    return {
        **counts,
        "missing": missing,
        "accepted": bool(cases) and not (counts["failed"] or counts["skipped"] or missing),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--require", action="append", default=[])
    args = parser.parse_args()
    try:
        report = evaluate(args.report, args.require)
    except (OSError, ET.ParseError) as exc:
        report = {"accepted": False, "error": type(exc).__name__}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
