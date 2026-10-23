"""Why the agent believes its diagnosis, computed in code (the approver's view; no model involved).

Three things the approver needs before saying yes, all derived from the evidence ledger:

  checklist   independent signals that agree with the named component, each with the evidence that shows it,
              summed into an evidence score (0..1) and a band (high / medium / low)
  ranking     every piece of evidence scored on how strongly the data itself points at the named component
              (trace origin, signal strength, service match, cited), so the most important evidence is visible
              without extra model calls
  warnings    where the model and the data disagree: a model confidence far above the evidence score, or
              decisive cited evidence that the data ranks low

`not_checked` lists the evidence kinds that were never collected, so a blind spot is stated rather than hidden.
"""
from __future__ import annotations

from typing import Any, Optional

from .agents.facts import Fact, extract
from .models import Diagnosis, Evidence, Incident

# (key, weight, what it means)
CHECKS: list[tuple[str, float, str]] = [
    ("traces", 0.30, "traces show the failure originates in the component"),
    ("alert", 0.20, "an alert fired on the component itself"),
    ("logs", 0.20, "the component's own logs show errors"),
    ("state", 0.20, "metrics or container state show it is abnormal"),
    ("single", 0.10, "only one root cause survived"),
]
HIGH, MEDIUM = 0.70, 0.40
STATE_KINDS = ("overview", "resources", "container", "datastore", "gap", "outbound")
EXPECTED_SOURCES = {"jaeger": "traces", "logs": "logs", "prometheus": "metrics", "docker": "container state"}


def band(score: float) -> str:
    return "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"


def _first(facts: list[Fact], component: str, kinds: tuple[str, ...]) -> Optional[Fact]:
    return next((x for x in facts if x.service == component and x.kind in kinds), None)


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def anomalous(x: Fact) -> bool:
    """A state fact that shows something wrong (a healthy container or quiet metrics corroborate nothing)."""
    v = x.values
    if x.kind == "container":
        return v.get("state") != "running" or bool(v.get("oom")) or bool(v.get("restarts")) or v.get("networks") == "none"
    if x.kind == "resources":
        return _num(v.get("throttled")) > 0 or _num(v.get("cpu")) >= 85 or _num(v.get("memory_percent")) >= 85
    if x.kind == "gap":
        return _num(v.get("gap_ms")) >= 200
    if x.kind == "overview":
        return "high error ratio" in str(v.get("notes", ""))
    return x.kind in ("datastore", "outbound")


def checklist(inc: Incident, diagnosis: Diagnosis, facts: list[Fact]) -> list[dict[str, Any]]:
    rc = diagnosis.root_causes[0] if diagnosis.root_causes else None
    comp = rc.component if rc else ""
    found = {
        "traces": _first(facts, comp, ("origin",)),
        "alert": _first(facts, comp, ("alert",)),
        "logs": _first(facts, comp, ("log",)),
        "state": next((x for x in facts if x.service == comp and x.kind in STATE_KINDS and anomalous(x)), None),
    }
    alert_on_comp = any(a.service == comp for a in inc.alerts)
    out = []
    for key, weight, meaning in CHECKS:
        if key == "single":
            ok, ev = len(diagnosis.root_causes) == 1, None
        elif key == "alert":
            ok, ev = bool(found["alert"]) or alert_on_comp, found["alert"].evidence_id if found["alert"] else None
        else:
            ok, ev = bool(found[key]), found[key].evidence_id if found[key] else None
        out.append({"check": key, "meaning": meaning, "weight": weight, "passed": bool(ok and comp), "evidence_id": ev})
    return out


def rank_evidence(evidence: list[Evidence], facts: list[Fact], component: str,
                  cited: set[str]) -> list[dict[str, Any]]:
    by_ev: dict[str, list[Fact]] = {}
    for x in facts:
        by_ev.setdefault(x.evidence_id, []).append(x)
    rows = []
    for e in evidence:
        fs = by_ev.get(e.id, [])
        mine = [x for x in fs if x.service == component]
        trace = 1.0 if any(x.kind == "origin" for x in mine) else 0.0
        strength = 1.0 if any(x.kind in ("origin", "alert") or (x.kind in STATE_KINDS and anomalous(x))
                              for x in mine) else 0.6 if any(
            x.kind == "log" for x in mine) else 0.5 if mine else 0.0
        svc = 1.0 if mine or e.args.get("service") == component else 0.0
        cite = 1.0 if e.id in cited else 0.0
        score = round(0.30 * trace + 0.35 * strength + 0.15 * svc + 0.20 * cite, 2)
        rows.append({"evidence_id": e.id, "source": e.source, "tool": e.tool, "summary": e.summary[:160],
                     "score": score, "trace_link": trace, "strength": strength, "service_match": svc, "cited": cite})
    rows.sort(key=lambda r: (-r["score"], r["evidence_id"]))
    return rows


def explain(inc: Incident, evidence: list[Evidence], diagnosis: Optional[Diagnosis] = None) -> dict[str, Any]:
    d = diagnosis or inc.diagnosis
    if d is None or not d.root_causes:
        return {"component": None, "score": 0.0, "band": "low", "checklist": [], "ranking": [], "warnings": [],
                "not_checked": [], "alternatives": []}
    facts = extract(evidence)
    rc = d.root_causes[0]
    checks = checklist(inc, d, facts)
    score = round(sum(c["weight"] for c in checks if c["passed"]), 2)
    cited_order = [c.evidence_id for c in rc.claims]
    ranking = rank_evidence(evidence, facts, rc.component, set(cited_order))
    warnings: list[str] = []
    if rc.confidence - score > 0.3:
        warnings.append(f"model confidence {rc.confidence:.2f} is well above the evidence score {score:.2f}: "
                        "check the evidence before approving")
    top3 = [r["evidence_id"] for r in ranking[:3]]
    decisive = next((e for e in cited_order if e in {r["evidence_id"] for r in ranking}), None)
    if decisive and decisive not in top3:
        warnings.append(f"the diagnosis leans on {decisive}, which the data ranks low "
                        f"(top evidence: {', '.join(top3)})")
    collected = {e.source for e in evidence}
    not_checked = [label for src, label in EXPECTED_SOURCES.items() if src not in collected]
    alternatives = [{"component": r.component, "cause": r.cause, "confidence": r.confidence}
                    for r in d.root_causes[1:]]
    return {"component": rc.component, "score": score, "band": band(score), "model_confidence": rc.confidence,
            "checklist": checks, "ranking": ranking[:8], "decisive_evidence": decisive, "warnings": warnings,
            "not_checked": not_checked, "alternatives": alternatives}


def to_markdown(x: dict[str, Any]) -> str:
    if not x.get("component"):
        return "## Why this diagnosis\n\nNo root cause was named, so there is nothing to explain.\n"
    lines = ["## Why this diagnosis", "",
             f"Evidence score **{x['score']:.2f} ({x['band']})** for `{x['component']}`, computed in code; "
             f"the model's own confidence was {x.get('model_confidence', 0):.2f}.", "",
             "| Check | Passed | Weight | Evidence |", "| --- | --- | --- | --- |"]
    for c in x["checklist"]:
        lines.append(f"| {c['meaning']} | {'yes' if c['passed'] else 'no'} | {c['weight']:.2f} | "
                     f"{c['evidence_id'] or '-'} |")
    lines += ["", "**Most important evidence (ranked by the data, not the model)**", "",
              "| Evidence | Score | Trace link | Strength | Cited | Summary |", "| --- | --- | --- | --- | --- | --- |"]
    for r in x["ranking"][:5]:
        lines.append(f"| {r['evidence_id']} | {r['score']:.2f} | {r['trace_link']:.0f} | {r['strength']:.1f} | "
                     f"{'yes' if r['cited'] else 'no'} | {r['summary'].replace('|', '/')} |")
    if x["alternatives"]:
        lines += ["", "**Other candidates**"] + [f"- {a['component']}: {a['cause']} ({a['confidence']:.2f})"
                                                  for a in x["alternatives"]]
    if x["not_checked"]:
        lines += ["", "**Not checked:** " + ", ".join(x["not_checked"])]
    if x["warnings"]:
        lines += ["", "**Warnings**"] + [f"- {w}" for w in x["warnings"]]
    return "\n".join(lines) + "\n"
