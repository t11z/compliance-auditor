"""The gates, as tests. Each one encodes a way the tool could quietly lie."""
import copy, json, subprocess, sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / ".maintenance" / "scripts"))
from render import exit_code, render, validate  # noqa: E402

BASE = json.loads((ROOT / "examples" / "example-run.json").read_text())


def mutate(**_):
    return copy.deepcopy(BASE)


# --- schema rails: the report structurally cannot make these claims -----------

@pytest.mark.parametrize("name,mut", [
    ("class C cannot pass",        lambda r: r["controls"][2].update(verdict="pass")),
    ("fail needs a finding",       lambda r: r["controls"][0].update(finding_refs=[])),
    ("fail needs remediation",     lambda r: r["controls"][0].pop("remediation")),
    ("class B needs confidence",   lambda r: r["controls"][5].pop("confidence")),
    ("inherited needs attestation", lambda r: r["controls"][5].pop("inherited_evidence")),
    ("no 'compliant' field",       lambda r: r.update(compliant=True)),
])
def test_schema_rejects(name, mut):
    r = mutate(); mut(r)
    with pytest.raises(jsonschema.ValidationError):
        validate(r)


def test_baseline_is_valid():
    validate(BASE)


# --- CI semantics: a broken collector is never green -------------------------

def test_fail_exits_1():
    assert exit_code(mutate(), {"fail"}) == 1


def test_partial_below_threshold_exits_0():
    r = mutate()
    for c in r["controls"]:
        if c["verdict"] == "fail":
            c["verdict"] = "pass"
    assert exit_code(r, {"fail"}) == 0


def test_broken_collector_never_green():
    r = mutate()
    r["tooling"]["collectors"][3]["status"] = "failed"
    assert exit_code(r, {"fail"}) == 2
    assert exit_code(r, {"fail", "partial"}) == 2


def test_insufficient_evidence_never_green():
    r = mutate()
    r["controls"][1]["verdict"] = "insufficient_evidence"
    assert exit_code(r, {"fail"}) == 2


# --- rendering is deterministic ----------------------------------------------

def test_render_is_byte_stable():
    assert render(BASE) == render(copy.deepcopy(BASE))


def test_report_states_out_of_scope():
    md = render(BASE)
    assert "Nicht technisch geprueft" in md
    assert "ORP.3.A1" in md


# --- watcher ------------------------------------------------------------------

def _watch(fixtures, today, state_reset=True):
    if state_reset:
        (ROOT / ".maintenance" / "state" / "sources.json").unlink(missing_ok=True)
    out = subprocess.run(
        [sys.executable, ".maintenance/scripts/watch.py", "--fixtures",
         f".maintenance/tests/fixtures/{fixtures}",
         "--write-state", "--today", today],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return json.loads(out.stdout), out.returncode


def test_watch_baseline_then_idempotent():
    rec, _ = _watch("v1", "2026-07-11")
    assert any(c["kind"] == "baseline_established" for c in rec["changes"])
    rec2, _ = _watch("v1", "2026-07-11", state_reset=False)
    assert not any(c["source_id"] == "bsi-grundschutz-kompendium" for c in rec2["changes"])


def test_watch_detects_building_block_change():
    _watch("v1", "2026-07-11")
    rec, _ = _watch("v2", "2026-07-11", state_reset=False)
    mod = [c for c in rec["changes"] if c["kind"] == "segment_modified"]
    assert any(c["segment_id"] == "CON.1" for c in mod)
    # the diff must be verbatim, not paraphrased
    assert "TLS 1.3" in next(c for c in mod if c["segment_id"] == "CON.1")["verbatim_diff"]


def test_watch_deadline_boundary():
    rec, _ = _watch("v1", "2026-06-01")
    assert not [c for c in rec["changes"] if c["source_id"] == "regulatory-deadlines"]
    rec, _ = _watch("v1", "2026-07-11", state_reset=False)
    assert any(c["kind"] == "deadline_approaching" for c in rec["changes"])
    rec, _ = _watch("v1", "2026-09-20", state_reset=False)
    assert any(c["kind"] == "deadline_passed" for c in rec["changes"])
