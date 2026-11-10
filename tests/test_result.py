"""TrialResult: exit codes, JSON shape, and trials that can't run."""

import json

from xania_sim.record.findings import Finding
from xania_sim.record.result import SCHEMA, Outcome, TrialResult, add_findings
from xania_sim.trials.commands import run


def _finding(sig="bug:x"):
    return Finding("xania-bug", sig, "xania logged a BUG: x", "medium", "x", "python -m xania_sim smoke", {})


def _ref(status):
    return {"signature": "s", "kind": "k", "title": "t", "severity": "high", "status": status, "first_time": False,
            "repro": ""}


def test_exit_codes():
    assert TrialResult("smoke", "t").exit_code() == 0
    assert TrialResult("smoke", "t", ran=False, error="no build").exit_code() == 2
    assert TrialResult("hunt", "t", findings=[_ref("new")]).exit_code() == 1
    # known and ignored findings are listed but don't fail a run
    assert TrialResult("hunt", "t", findings=[_ref("known"), _ref("ignored")]).exit_code() == 0


def test_json_shape():
    r = TrialResult("stage", "t", measurements={"ac": 187.5}, outcomes=[Outcome("gear", False, "wear ring", ["no"])])
    d = json.loads(r.to_json())
    assert d["schema"] == SCHEMA and d["exit_code"] == 0
    assert d["outcomes"][0] == {"activity": "gear", "ok": False, "summary": "wear ring", "evidence": ["no"], "data": {}}
    assert "failed: gear: wear ring" in r.text()


def test_findings_without_recording_are_new():
    r = add_findings(TrialResult("smoke", "t"), [_finding()], record=False)
    assert r.findings[0]["status"] == "new" and r.findings[0]["first_time"] and r.exit_code() == 1
    assert r.run_id == ""


def test_run_catches_errors_as_couldnt_run():
    def body(result):
        raise RuntimeError("the test MUD failed to start")

    r = run("smoke", "t", body, record=False)
    assert not r.ran and "failed to start" in r.error and r.exit_code() == 2
    assert "COULDN'T RUN" in r.text()


def test_run_collects_findings_and_measurements():
    def body(result):
        result.measurements["quits"] = 42
        return [_finding()]

    r = run("churn", "t", body, record=False)
    assert r.ran and r.measurements == {"quits": 42} and len(r.findings) == 1 and r.exit_code() == 1
