"""Collect prerequisite evidence for three demonstrations, never certify live missions.

This command uses controlled model responses. It does not deploy, send mail to
external recipients, or consume model credentials. See docs/demonstrations.md.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import signal
import shutil
import stat
import tempfile
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "development": [
        "tests/test_missions.py::test_real_planner_and_dependent_workers_complete_evidence",
        "tests/test_sandbox.py::test_real_broker_socket_and_registry_receipt",
        "tests/test_quality.py::test_defect_rejected_revised_and_independently_verified",
        "tests/test_delivery.py::test_exact_merge_approval_and_receipt",
        "tests/test_delivery.py::test_workflow_selection_stale_head_and_results",
    ],
    "browser": [
        "tests/test_browser.py::test_real_journey_upload_download_tabs_and_session_resume",
        "tests/test_browser.py::test_real_runtime_approval_to_receipt_through_private_socket",
        "tests/test_browser.py::test_real_timeout_and_unapproved_form_do_not_repeat",
        "tests/test_browser.py::test_real_file_upload_and_uncertain_submission",
    ],
    "recovery": [
        "tests/test_missions.py::test_mission_daemon_restart_keeps_completed_work",
        "tests/test_workers.py::test_worker_daemon_concurrent_children_survive_process_death",
        "tests/test_runtime_failures.py::test_process_crash_compares_external_effect_and_durable_receipt",
    ],
}


def classify(xml_path, returncode, required):
    """A missing case, crash, failure, or skip must never become passing evidence."""
    try:
        cases = list(ET.parse(xml_path).iter("testcase"))
    except (OSError, ET.ParseError):
        return {"status": "failed", "reason": "Missing or malformed JUnit evidence", "cases": []}
    rows = []
    for case in cases:
        state = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            state = "failed"
        elif case.find("skipped") is not None:
            state = "blocked"
        rows.append({"name": case.get("name", ""), "status": state, "seconds": case.get("time")})
    missing = [name for name in required if not any(r["name"].split("[")[0] == name for r in rows)]
    if returncode or missing or not rows or any(r["status"] == "failed" for r in rows):
        status = "failed"
    elif any(r["status"] == "blocked" for r in rows):
        status = "blocked"
    else:
        status = "passed"
    return {"status": status, "missing": missing, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private evidence directory")
    args = parser.parse_args()
    output = args.output.resolve()
    # Do not overwrite old evidence, or let pytest clear an existing fixture directory.
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Allow transport/runtime settings only. Unknown future credential names must
    # not silently enter a controlled test process.
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SYSTEMROOT",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        if key in os.environ
    }
    # Only pinned container images enter the controlled tests. No provider tokens.
    for key in ("A4G_TEST_BROWSER_IMAGE", "A4G_TEST_SANDBOX_IMAGE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env["A4G_QUALITY_TRACE_PATH"] = str(output / "quality.json")
    env["A4G_QUALITY_SANDBOX_TRACE_PATH"] = str(output / "sandbox-quality.json")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "component prerequisites; not three complete autonomous workflows",
        "model_evidence": "controlled scripted responses; no live calls",
        "deployment_evidence": "simulated adapter only; no actual deployment",
        "source_evidence": "controlled repository fixture; no live GitHub fetch",
        "live_model_cost_usd": None,
        "complete_workflows_verified": False,
        "groups": {},
    }
    for name, nodes in GROUPS.items():
        xml = output / (name + ".xml")
        command = [sys.executable, "-m", "pytest", "-q", *nodes, "--junitxml=" + str(xml)]
        # These tests own disposable tmp_path fixtures; pytest retains only marked test data.
        # AF_UNIX socket paths are limited to about 108 bytes. Evidence destinations
        # can be arbitrarily deep; keep executing fixtures on a short private path.
        fixtures = Path(tempfile.mkdtemp(prefix="a4gd-", dir="/tmp"))
        command.append("--basetemp=" + str(fixtures))
        with (output / (name + ".log")).open("w") as log:
            process = subprocess.Popen(
                command, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
            )
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                # Daemon probes spawn children. Retire the whole test process group.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                code = 124
        try:
            shutil.copytree(
                fixtures,
                output / (name + "-fixtures"),
                symlinks=True,
                ignore=lambda directory, names: [
                    entry for entry in names if stat.S_ISSOCK((Path(directory) / entry).lstat().st_mode)
                ],
            )
        finally:
            shutil.rmtree(fixtures)
        report["groups"][name] = classify(xml, code, [n.split("::")[-1] for n in nodes])
        report["groups"][name]["returncode"] = code
        print(name + ": " + report["groups"][name]["status"], flush=True)
    report["files"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
        if path.is_file()
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    # A clean prerequisite suite still cannot certify a composed mission or live deployment.
    return 1 if any(g["status"] == "failed" for g in report["groups"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
