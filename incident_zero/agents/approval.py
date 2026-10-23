"""Agent 6, the Approval Gate (REQUIREMENTS F5.1–F5.4, C4, C8, C9). No model: a human decides.

`request` marks the plan pending, bound to its hash, and notifies (event for the Control Room badge and browser
notification, email when configured). `decide` is called by the server or CLI: the approver's name is required and
the hash must match the plan that was shown, so a plan that changed after it was displayed cannot be approved.
`wait` blocks the incident's worker until a decision arrives or the timeout escalates it. Nothing executes without
"approved".
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from .. import notify
from ..models import Incident, iso, utcnow
from .context import Context

DECISIONS = ("approved", "rejected", "sent_back")


class ApprovalError(ValueError):
    pass


class ApprovalGate:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._signals: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _event(self, incident_id: str) -> threading.Event:
        with self._lock:
            return self._signals.setdefault(incident_id, threading.Event())

    def request(self, inc: Incident) -> Incident:
        assert inc.plan is not None
        now = utcnow()
        inc.approval.required = True
        inc.approval.decision = "pending"
        inc.approval.change_hash = inc.plan.change_hash
        inc.approval.requested_at = iso(now)
        inc.approval.expires_at = iso(now + timedelta(seconds=self.ctx.settings.approval_timeout_s))
        inc.approval.approver = inc.approval.timestamp = None
        inc.approval.note = ""
        self._event(inc.id).clear()
        self.ctx.store.save(inc)
        acts = "; ".join(f"{a.action} {a.parameters}" for a in inc.plan.actions)
        x = inc.explanation or {}
        self.ctx.bus.publish("approval", "approval", f"approval needed for {inc.id}: {acts}", incident_id=inc.id,
                             detail={"change_hash": inc.plan.change_hash, "expires_at": inc.approval.expires_at,
                                     "evidence_score": x.get("score"), "band": x.get("band"),
                                     "warnings": x.get("warnings", [])})
        diag = inc.diagnosis.summary if inc.diagnosis else ""
        why = ""
        if x.get("component"):
            checks = "\n".join(f"  [{'x' if c['passed'] else ' '}] {c['meaning']}"
                               + (f" ({c['evidence_id']})" if c.get("evidence_id") else "") for c in x["checklist"])
            top = ", ".join(f"{r['evidence_id']} ({r['score']:.2f})" for r in x.get("ranking", [])[:3])
            why = (f"Evidence score: {x['score']:.2f} ({x['band']}), computed in code; model confidence "
                   f"{x.get('model_confidence', 0):.2f}\n{checks}\nMost important evidence: {top}\n"
                   + (f"Not checked: {', '.join(x['not_checked'])}\n" if x.get("not_checked") else "")
                   + "".join(f"WARNING: {w}\n" for w in x.get("warnings", [])) + "\n")
        body = (f"Incident {inc.id}: {inc.title}\n\nDiagnosis: {diag}\n\n{why}Proposed: {acts}\n\n"
                f"Approve or reject in the Control Room: http://{self.ctx.settings.host}:{self.ctx.settings.port}"
                f"/incidents/{inc.id}/plan\nNothing changes without your approval. Expires {inc.approval.expires_at}.")
        result = notify.send(self.ctx.settings, f"[Incident Zero] approval needed: {inc.id} {inc.title}", body)
        self.ctx.bus.publish("approval", "step", result, incident_id=inc.id)
        return inc

    def decide(self, incident_id: str, decision: str, approver: str, change_hash: str, note: str = "") -> Incident:
        if decision not in DECISIONS:
            raise ApprovalError(f"decision must be one of {DECISIONS}")
        if not approver.strip():
            raise ApprovalError("an approver name is required")
        inc = self.ctx.store.load(incident_id)
        if inc is None:
            raise ApprovalError(f"no incident {incident_id}")
        if inc.phase != "awaiting_approval" or inc.approval.decision != "pending":
            raise ApprovalError(f"{incident_id} is not waiting for approval (phase {inc.phase})")
        if change_hash != inc.approval.change_hash:
            raise ApprovalError("the plan changed since it was shown; reload and review it again")
        if decision == "sent_back" and not note.strip():
            raise ApprovalError("sending back needs a note for the investigator")
        inc.approval.decision = decision  # type: ignore[assignment]
        inc.approval.approver = approver.strip()[:80]
        inc.approval.timestamp = iso()
        inc.approval.note = note.strip()[:1000]
        self.ctx.store.save(inc)
        self.ctx.bus.publish("human", "approval", f"{inc.approval.approver} {decision.replace('_', ' ')} the plan"
                             + (f": {inc.approval.note}" if inc.approval.note else ""), incident_id=inc.id,
                             detail={"decision": decision, "approver": inc.approval.approver})
        self._event(incident_id).set()
        return inc

    def wait(self, inc: Incident) -> str:
        """approved | rejected | sent_back | timed_out"""
        with self.ctx.status.working("approval", inc.id, "waiting for a human decision"):
            deadline = _parse(inc.approval.expires_at) or (utcnow() + timedelta(seconds=self.ctx.settings.approval_timeout_s))
            ev = self._event(inc.id)
            while True:
                cur = self.ctx.store.load(inc.id) or inc
                if cur.approval.decision in DECISIONS:
                    return cur.approval.decision
                remaining = (deadline - utcnow()).total_seconds()
                if remaining <= 0:
                    cur.approval.decision = "timed_out"
                    cur.approval.timestamp = iso()
                    self.ctx.store.save(cur)
                    self.ctx.bus.publish("approval", "approval", f"no decision within "
                                         f"{self.ctx.settings.approval_timeout_s:.0f}s; escalating", incident_id=inc.id)
                    return "timed_out"
                ev.wait(min(remaining, 2.0))
                ev.clear()


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
