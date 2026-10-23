"""The contract between the agents. Every agent reads and writes these, persisted by store.IncidentStore.

The jury's report is a different, stricter shape (report_schema.py); the Reporter maps an Incident onto it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[datetime] = None) -> str:
    return (ts or utcnow()).isoformat(timespec="seconds").replace("+00:00", "Z")


Phase = Literal[
    "detected", "investigating", "diagnosing", "auditing", "planning", "awaiting_approval", "executing",
    "verifying", "reporting",
    # terminal
    "resolved", "dry_run_complete", "escalated", "rejected", "failed", "false_alarm",
]
TERMINAL: tuple[str, ...] = ("resolved", "dry_run_complete", "escalated", "rejected", "failed", "false_alarm")

Category = Literal[
    "application_error",   # the service's own code fails requests
    "dependency_failure",  # a cache, database or downstream service it needs is down or failing
    "container_down",      # stopped, crashed, or restarting in a loop
    "resource_cpu",        # CPU starvation, throttling, runaway CPU or GC
    "resource_memory",     # memory leak, starvation, OOM kills
    "network",             # latency or loss on the path between services
    "traffic",             # a load spike or flood
    "unknown",
]


class Alert(BaseModel):
    id: str                                   # A1, A2 ... within the incident
    rule: Literal["error_rate", "op_error_rate", "latency_p95", "container_down", "container_restarts",
                  "traffic_spike", "telemetry_silent", "entry_unhealthy"]
    service: str
    signal: str                               # human description of what was measured
    value: Optional[float] = None
    threshold: Optional[float] = None
    baseline: Optional[float] = None
    unit: str = ""
    query: str = ""                           # the PromQL or docker check that produced it
    first_seen: str = Field(default_factory=iso)
    last_seen: str = Field(default_factory=iso)
    active: bool = True


class Evidence(BaseModel):
    id: str                                   # E1, E2 ... within the incident
    source: Literal["prometheus", "jaeger", "logs", "docker", "topology", "action_api", "sentinel"]
    tool: str
    args: dict[str, Any] = {}
    summary: str                              # one line for the timeline
    content: str                              # the rendered text claims must quote from, verbatim
    data: Any = None                          # trimmed raw result, for the Control Room's expand view
    created_at: str = Field(default_factory=iso)


class Claim(BaseModel):
    text: str                                 # what the claim says, in the Diagnostician's words
    evidence_id: str
    quote: str                                # an exact excerpt of that evidence's content


class RootCause(BaseModel):
    component: str
    category: Category = "unknown"
    cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    claims: list[Claim] = []


class Diagnosis(BaseModel):
    summary: str
    root_causes: list[RootCause] = []
    explained_alerts: list[str] = []          # alert ids the root causes account for
    false_alarm: bool = False                 # the evidence shows nothing is wrong; `claims` must show it
    claims: list[Claim] = []                  # evidence for a false alarm (root causes carry their own)
    produced_by: Literal["llm", "rules"] = "rules"
    created_at: str = Field(default_factory=iso)

    @property
    def confidence(self) -> float:
        return max((r.confidence for r in self.root_causes), default=0.0)


class AuditResult(BaseModel):
    passed: bool
    problems: list[str] = []
    unexplained_alerts: list[str] = []
    checked_claims: int = 0
    created_at: str = Field(default_factory=iso)


class ProposedAction(BaseModel):
    action: str
    parameters: dict[str, Any] = {}
    reason: str
    dry_run: bool = True
    preview: Optional[str] = None             # what the Action API says it would do (its dry-run answer)
    reversible: bool = False


class Plan(BaseModel):
    actions: list[ProposedAction]
    change_hash: str                          # sha256 of exactly what the human is shown
    produced_by: Literal["llm", "rules"] = "rules"
    no_action_reason: str = ""                # set when nothing in the runbook fits: escalate instead
    created_at: str = Field(default_factory=iso)


class ApprovalRecord(BaseModel):
    required: bool = True
    decision: Literal["pending", "approved", "rejected", "sent_back", "timed_out", "not_requested"] = "not_requested"
    approver: Optional[str] = None
    timestamp: Optional[str] = None
    note: str = ""
    change_hash: Optional[str] = None
    requested_at: Optional[str] = None
    expires_at: Optional[str] = None


class ActionResult(BaseModel):
    action: str
    parameters: dict[str, Any] = {}
    execution_id: Optional[str] = None
    ok: bool
    dry_run: bool
    message: str = ""
    at: str = Field(default_factory=iso)


class ExecutionRecord(BaseModel):
    attempted: bool = False
    action_api_only: bool = True
    results: list[ActionResult] = []
    rollback_performed: bool = False
    rollback_results: list[ActionResult] = []
    notes: str = ""


class VerificationRecord(BaseModel):
    checked: bool = False
    recovered: Optional[bool] = None
    signals_checked: list[str] = []
    notes: str = ""
    samples: list[dict[str, Any]] = []


class Incident(BaseModel):
    id: str
    title: str
    external_id: Optional[str] = None         # the practice incident id the environment (faultctl) supplies
    phase: Phase = "detected"
    created_at: str = Field(default_factory=iso)
    updated_at: str = Field(default_factory=iso)
    resolved_at: Optional[str] = None
    alerts: list[Alert] = []
    services: list[str] = []                  # services named by the alerts
    rounds: int = 0                           # investigation rounds (send-backs add one)
    investigation_notes: list[str] = []       # the Investigator's own summary per round
    human_notes: list[str] = []               # notes from "send back"
    diagnosis: Optional[Diagnosis] = None
    audits: list[AuditResult] = []
    plan: Optional[Plan] = None
    approval: ApprovalRecord = Field(default_factory=ApprovalRecord)
    execution: ExecutionRecord = Field(default_factory=ExecutionRecord)
    verification: VerificationRecord = Field(default_factory=VerificationRecord)
    prevention: list[str] = []
    limitations: list[str] = []
    escalation_reason: str = ""
    report_summary: str = ""
    llm: str = ""                             # which model ran, or why none did
    explanation: dict = {}                    # explain.explain(): evidence score, checklist, ranked evidence, warnings

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL


class Event(BaseModel):
    seq: int = 0
    ts: str = Field(default_factory=iso)
    incident_id: Optional[str] = None
    agent: str                                # sentinel, investigator, ..., system, human
    kind: Literal["phase", "alert", "tool", "step", "approval", "action", "verify", "report", "system", "error"]
    title: str
    detail: Optional[dict[str, Any]] = None
    evidence_id: Optional[str] = None
