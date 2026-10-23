"""Agent 3, the Diagnostician: names the failing component(s), the root cause(s) and a confidence, each supported by
claims that quote evidence (REQUIREMENTS F3.1, F3.2).

With a model: one JSON request over the alerts and the full evidence. Without one (or after the Auditor has
rejected the model twice): rules over facts parsed from the evidence. Either way the Auditor checks the result.
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from pydantic import ValidationError

from ..models import Claim, Diagnosis, Evidence, Incident, RootCause
from ..telemetry.backends import Topology
from .context import Context
from .facts import Fact, extract

SYSTEM = """You diagnose production incidents in the OpenTelemetry Demo from collected evidence only.
Return one JSON object:
{"summary": "<2 sentences>",
 "root_causes": [{"component": "<service name exactly as in the evidence>",
                  "category": "application_error|dependency_failure|container_down|resource_cpu|resource_memory|network|traffic|unknown",
                  "cause": "<what is wrong, one sentence>",
                  "confidence": <0..1>,
                  "claims": [{"text": "<what this evidence shows>", "evidence_id": "E<n>",
                              "quote": "<an EXACT substring copied character for character from that evidence>"}]}],
 "explained_alerts": ["A1", ...],
 "false_alarm": false,
 "claims": []}
If the evidence shows the alerted services are healthy (the alert was noise, a measurement artifact, or a downstream
consequence that has cleared), set "false_alarm": true, leave "root_causes" empty, and put the evidence that shows
health in top-level "claims" (same quote rules). Never invent a fault to have something to fix.
Rules:
- Quotes are checked by code against the evidence text. Copy them exactly (numbers, spacing, punctuation). A quote
  that is not in the cited evidence rejects the whole diagnosis.
- Cite 2-4 claims per root cause, from different sources where possible (metrics, traces, logs, containers).
- The root cause is the deepest failing component, not the service where the symptom shows.
- List several root causes only if the evidence shows independent failures; explained_alerts must cover every
  active alert.
- Confidence reflects how directly the evidence shows the cause, not how sure you feel."""


def _downstream(topo: Topology, start: str) -> set[str]:
    seen, q = {start}, deque([start])
    while q:
        for n in topo.callees(q.popleft()):
            if n not in seen:
                seen.add(n)
                q.append(n)
    return seen


def explains(rc: RootCause, alert_service: str, alert_rule: str, topo: Topology) -> bool:
    if rc.component == alert_service:
        return True
    if alert_rule == "traffic_spike":
        return rc.category == "traffic"
    if not topo.edges:
        return True  # no call graph known: cannot tell, do not block on it
    return rc.component in _downstream(topo, alert_service)


@dataclass
class _Finding:
    component: str
    category: str
    cause: str
    weight: float = 0.0
    claims: list[Claim] = field(default_factory=list)

    def add(self, w: float, fact: Fact, text: str, quote: Optional[str] = None) -> None:
        self.weight += w
        if not any(c.evidence_id == fact.evidence_id and c.quote == (quote or fact.line) for c in self.claims):
            self.claims.append(Claim(text=text, evidence_id=fact.evidence_id, quote=(quote or fact.line)[:240]))


def rule_diagnosis(inc: Incident, evidence: list[Evidence], topo: Topology,
                   ignore: tuple[str, ...] = ()) -> Diagnosis:
    facts = extract(evidence)
    found: dict[tuple[str, str], _Finding] = {}

    def f(svc: str, cat: str, cause: str) -> _Finding:
        key = (svc, cat)
        if key not in found:
            found[key] = _Finding(svc, cat, cause)
        return found[key]

    for x in facts:
        v, s = x.values, x.service
        if s in ignore:
            continue
        if x.kind == "container":
            if v["state"] not in ("running",):
                f(s, "container_down", f"{s}'s container is {v['state']} (exit code {v['exit_code']})").add(
                    6, x, f"{s} is not running", f"{s}: state={v['state']}")
            elif v["networks"] == "none":
                f(s, "network", f"{s} is disconnected from the application network").add(
                    6, x, f"{s} has no network attached", "networks=none")
            elif v["oom"]:
                f(s, "resource_memory", f"{s} ran out of memory and was OOM-killed").add(
                    5, x, f"{s} was OOM killed", f"oom_killed=true restarts={v['restarts']}")
            elif v["restarts"] and any(a.rule == "container_restarts" and a.service == s for a in inc.alerts):
                f(s, "container_down", f"{s} keeps restarting").add(4, x, f"{s} restarted", f"restarts={v['restarts']}")
        elif x.kind == "origin":
            if v["connectivity"]:
                f(s, "dependency_failure", f"{s} cannot reach a dependency: {v['message'][:120]}").add(
                    2, x, "deepest failing span is a connectivity error")
            else:
                f(s, "application_error", f"{s} fails {v['operation']}: {v['message'][:120]}").add(
                    4, x, f"traces show the failure originates in {s}")
        elif x.kind == "gap" and v["gap_ms"] >= 200:
            f(s, "network", f"calls from {v['caller']} to {s} spend {v['gap_ms']:.0f} ms in the network").add(
                5, x, "client-server latency gap")
        elif x.kind == "resources":
            if v["throttled"] > 0 or v["cpu"] >= 85:
                f(s, "resource_cpu", f"{s} is CPU-starved or burning CPU ({v['cpu']}%)").add(
                    4, x, f"{s} CPU {v['cpu']}%", _field(x.line, "cpu_percent"))
            if v["memory_percent"] is not None and v["memory_percent"] >= 85:
                f(s, "resource_memory", f"{s} memory is at {v['memory_percent']}% of its limit").add(
                    4, x, f"{s} memory near limit", _field(x.line, "memory_percent"))
        elif x.kind == "datastore":
            f(s, "dependency_failure", f"{s} is unreachable: its metrics receiver has no fresh samples").add(
                4, x, f"{s} metrics are stale or absent", x.line)
        elif x.kind == "alert" and v["rule"] == "op_error_rate":
            op = re.search(r"operation=(\S+)", x.line)
            f(s, "application_error", f"{s} fails its {op.group(1) if op else 'one'} operation").add(
                3, x, f"one operation of {s} fails while the service total looks healthy", _field(x.line, "operation"))
        elif x.kind == "alert" and v["rule"] == "traffic_spike":
            f(s, "traffic", f"a traffic flood hit {s}").add(5, x, "request rate far above baseline")
        elif x.kind == "overview" and "high error ratio" in v["notes"]:
            f(s, "application_error", f"{s} fails a large share of its requests").add(
                1, x, f"{s} error ratio", _field(x.line, "error_ratio"))
    # supporting log lines and alert evidence for every finding's component
    for fd in found.values():
        for x in facts:
            if x.service == fd.component and x.kind == "log" and len([c for c in fd.claims]) < 4:
                fd.add(0.5, x, f"{fd.component} logs the failure", x.line[:200])
                break
        for x in facts:
            if x.kind == "alert" and x.service == fd.component and v_rule_ok(x):
                fd.add(0.5, x, f"alert {x.values['alert_id']} on {fd.component}", x.line[:200])
                break
    ranked = sorted(found.values(), key=lambda fd: -fd.weight)
    causes: list[RootCause] = []
    active = [a for a in inc.alerts if a.active] or inc.alerts
    unexplained = list(active)
    for fd in ranked:
        rc = RootCause(component=fd.component, category=fd.category, cause=fd.cause,  # type: ignore[arg-type]
                       confidence=round(min(0.95, 0.3 + 0.1 * len(fd.claims) + 0.04 * min(fd.weight, 8)), 2),
                       claims=fd.claims[:4])
        covers = [a for a in unexplained if explains(rc, a.service, a.rule, topo)]
        if causes and (not covers or fd.weight < 3):
            continue
        if causes and any(c.component == rc.component for c in causes):
            continue
        causes.append(rc)
        unexplained = [a for a in unexplained if a not in covers]
        if not unexplained:
            break
    explained = [a.id for a in inc.alerts if any(explains(rc, a.service, a.rule, topo) for rc in causes)]
    if causes:
        main = causes[0]
        summary = (f"{main.cause}." + (f" Also: {'; '.join(c.cause for c in causes[1:])}." if causes[1:] else ""))
    else:
        summary = "No root cause could be established from the evidence collected."
    return Diagnosis(summary=summary, root_causes=causes, explained_alerts=explained, produced_by="rules")


def _field(line: str, name: str) -> str:
    """`name=value` exactly as it appears in the line (a verbatim quote)."""
    m = re.search(rf"{name}=\S+", line)
    return m.group(0) if m else line


def v_rule_ok(x: Fact) -> bool:
    return x.values.get("rule") in ("error_rate", "op_error_rate", "latency_p95", "traffic_spike", "telemetry_silent",
                                    "container_down", "container_restarts")


class Diagnostician:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident, evidence: list[Evidence], summary: str, use_model: bool = True) -> Diagnosis:
        with self.ctx.status.working("diagnostician", inc.id, "naming the root cause"):
            if use_model and self.ctx.llm.enabled:
                d = self._model(inc, evidence, summary)
                if d is not None:
                    return d
                self.ctx.bus.publish("diagnostician", "error", "model gave no usable diagnosis; using rules",
                                     incident_id=inc.id)
            return rule_diagnosis(inc, evidence, self.ctx.telemetry.topology, tuple(self.ctx.settings.ignore_services))

    def _model(self, inc: Incident, evidence: list[Evidence], summary: str) -> Optional[Diagnosis]:
        alerts = "\n".join(f"{a.id} [{'active' if a.active else 'cleared'}] {a.rule} on {a.service}: {a.signal}"
                           for a in inc.alerts)
        budget, parts = 45000, []
        for e in evidence:
            block = f"--- {e.id} [{e.source}/{e.tool}] {json.dumps(e.args)}\n{e.content[:2500]}"
            if budget - len(block) < 0:
                parts.append(f"--- {e.id} [{e.source}/{e.tool}] (omitted: {e.summary})")
                continue
            budget -= len(block)
            parts.append(block)
        user = (f"Incident {inc.id}: {inc.title}\nAlerts:\n{alerts}\n\nInvestigator's summary: {summary}\n\n"
                f"Known services: {', '.join(sorted(self.ctx.services()))}\n\nEvidence:\n" + "\n".join(parts))
        data = self.ctx.llm.json(SYSTEM, user, max_tokens=2000)
        if not data:
            return None
        try:
            return Diagnosis.model_validate({**data, "produced_by": "llm"})
        except ValidationError:
            return None


def drop_unsupported(d: Diagnosis, evidence: list[Evidence]) -> Diagnosis:
    """Keep only claims that pass the audit's checks, and root causes that still have one."""
    from ..evidence import quote_in

    by_id = {e.id: e for e in evidence}
    kept = []
    for rc in d.root_causes:
        rc.claims = [c for c in rc.claims if c.evidence_id in by_id and quote_in(by_id[c.evidence_id], c.quote)]
        if rc.claims:
            kept.append(rc)
    d.root_causes = kept
    return d
