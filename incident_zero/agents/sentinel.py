"""Agent 1, the Sentinel: detects incidents and opens them (REQUIREMENTS F1.1–F1.3). No model.

Every `sentinel_interval_s` it evaluates the alert rules on Prometheus span metrics and Docker state:

  error_rate          a service's share of failing server spans above the threshold (and not chronic at baseline)
  op_error_rate       one operation's share of failing server spans above `op_error_ratio`, with at least
                      `op_min_errors` failures in the window, when the service as a whole is under `error_ratio`
  latency_p95         p95 above max(floor, factor x baseline)
  traffic_spike       an entry service's request rate above factor x baseline
  telemetry_silent    a service that served traffic at baseline and now reports none
  container_down      a container that is not running (exited, paused, dead, restarting)
  container_restarts  a restart count that went up, or an OOM kill

A signal must hold for `confirm_ticks` ticks. The baseline is learned from quiet ticks (an average per service),
falling back to the same query `baseline_s` ago. Alerts join the open incident while it is still being investigated
or was opened within the correlation window; otherwise they open a new one. `evaluate` is also what the Verifier
uses to decide whether an incident's signals have cleared.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..evidence import EvidenceLedger
from ..models import Alert, Incident, iso
from ..telemetry import queries as Q
from ..telemetry.backends import BackendError
from .context import Context

EARLY_PHASES = ("detected", "investigating", "diagnosing", "auditing", "planning")
RULE_LABEL = {"error_rate": "Error rate", "op_error_rate": "Operation errors", "latency_p95": "Latency", "traffic_spike": "Traffic spike",
              "telemetry_silent": "Telemetry silent", "container_down": "Container down",
              "container_restarts": "Container restarting", "entry_unhealthy": "Entry unhealthy"}


@dataclass
class Signal:
    rule: str
    service: str
    signal: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    baseline: Optional[float] = None
    unit: str = ""
    query: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.service)


def red(ctx: Context, window: str, offset: float = 0) -> dict[str, dict]:
    p = ctx.telemetry.prom
    rps = p.vector(Q.rps(window, offset_s=offset), "service_name")
    err = p.vector(Q.error_rps(window, offset_s=offset), "service_name")
    lat = p.vector(Q.p95(window, offset_s=offset), "service_name")
    return {s: {"rps": rps.get(s, 0.0), "error_ratio": (err.get(s, 0.0) / rps[s]) if rps.get(s) else 0.0,
                "p95": lat.get(s)} for s in set(rps) | set(lat)}


def red_ops(ctx: Context, window: str, offset: float = 0) -> dict[tuple[str, str], dict]:
    """{(service, operation): {"rps", "error_rps"}} from server and consumer span metrics."""
    p = ctx.telemetry.prom

    def by_op(promql: str) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for item in p.query(promql):
            m = item.get("metric", {})
            try:
                v = float(item["value"][1])
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if m.get("service_name") and m.get("span_name") and v == v:
                out[(m["service_name"], m["span_name"])] = v
        return out

    rps, err = by_op(Q.op_rps(window, offset_s=offset)), by_op(Q.op_error_rps(window, offset_s=offset))
    return {k: {"rps": rps.get(k, 0.0), "error_rps": err.get(k, 0.0)} for k in set(rps) | set(err)}


def op_signals(ctx: Context, window: str, service_firing: set[str]) -> list[Signal]:
    """Operation-level errors that a service-level ratio dilutes (one signal per service: its worst operation)."""
    s, th = ctx.settings, ctx.settings.thresholds
    try:
        now = red_ops(ctx, window)
        base = red_ops(ctx, window, s.baseline_s)
    except (BackendError, AttributeError):
        return []
    win_s = Q.window_seconds(window)
    worst: dict[str, Signal] = {}
    for (svc, op), n in sorted(now.items()):
        if svc in s.ignore_services or svc in service_firing or not n["rps"]:
            continue
        ratio = n["error_rps"] / n["rps"]
        errors = n["error_rps"] * win_s
        b = base.get((svc, op), {})
        b_ratio = (b.get("error_rps", 0.0) / b["rps"]) if b.get("rps") else 0.0
        if ratio < th.op_error_ratio or errors < th.op_min_errors or b_ratio >= th.op_error_ratio:
            continue
        sig = Signal("op_error_rate", svc, f"{svc} operation={op} error_ratio={ratio:.3f} errors={errors:.0f} "
                     f"over {window} (threshold {th.op_error_ratio:.3f}, baseline {b_ratio:.3f})",
                     ratio, th.op_error_ratio, b_ratio, "ratio", Q.op_error_rps(window, svc))
        if svc not in worst or (worst[svc].value or 0) < ratio:
            worst[svc] = sig
    return list(worst.values())


def evaluate(ctx: Context, learned: Optional[dict[str, dict]] = None, window: Optional[str] = None,
             restart_seen: Optional[dict[str, int]] = None) -> tuple[list[Signal], dict[str, dict]]:
    """(signals firing now, current RED per service)."""
    s, th = ctx.settings, ctx.settings.thresholds
    window = window or s.rate_window
    signals: list[Signal] = []
    try:
        now = red(ctx, window)
        offset_base = red(ctx, window, s.baseline_s)
    except BackendError:
        now, offset_base = {}, {}
    learned = learned or {}
    for svc in sorted(set(now) | set(offset_base)):
        if svc in s.ignore_services:
            continue
        n = now.get(svc, {"rps": 0.0, "error_ratio": 0.0, "p95": None})
        b = learned.get(svc) or offset_base.get(svc) or {}
        q_err = Q.error_rps(window, svc)
        if n["rps"] >= th.min_rps and n["error_ratio"] >= th.error_ratio and b.get("error_ratio", 0) < th.error_ratio:
            signals.append(Signal("error_rate", svc, f"{svc} error_ratio={n['error_ratio']:.3f} over {window} "
                                  f"(threshold {th.error_ratio:.3f}, baseline {b.get('error_ratio', 0):.3f})",
                                  n["error_ratio"], th.error_ratio, b.get("error_ratio"), "ratio", q_err))
        if n.get("p95") and b.get("p95") and n["rps"] >= th.min_rps:
            limit = max(th.latency_p95_floor_ms, th.latency_factor * b["p95"])
            if n["p95"] > limit:
                signals.append(Signal("latency_p95", svc, f"{svc} p95_ms={n['p95']:.0f} over {window} "
                                      f"(threshold {limit:.0f}, baseline {b['p95']:.0f})", n["p95"], limit, b["p95"],
                                      "ms", Q.p95(window, svc)))
        if svc in s.entry_services and b.get("rps", 0) >= th.min_rps and n["rps"] > th.traffic_factor * b["rps"]:
            signals.append(Signal("traffic_spike", svc, f"{svc} rps={n['rps']:.2f} over {window} "
                                  f"(baseline {b['rps']:.2f}, factor {th.traffic_factor:g})", n["rps"],
                                  th.traffic_factor * b["rps"], b["rps"], "req/s", Q.rps(window, svc)))
        if b.get("rps", 0) >= th.silent_min_rps and n["rps"] == 0:
            signals.append(Signal("telemetry_silent", svc, f"{svc} rps=0 over {window} (baseline {b['rps']:.2f})",
                                  0.0, None, b["rps"], "req/s", Q.rps(window, svc)))
    signals += op_signals(ctx, window, {sg.service for sg in signals if sg.rule == "error_rate"})
    try:
        containers = ctx.telemetry.docker.containers()
    except Exception:
        containers = {}
    for svc, c in sorted(containers.items()):
        if svc in s.ignore_services:
            continue
        try:
            st = ctx.telemetry.docker.status(c)
        except Exception:
            continue
        if not st["running"] or st["restarting"] or st["paused"]:
            signals.append(Signal("container_down", svc, f"{svc} container state={st['state']} "
                                  f"exit_code={st['exit_code']}", None, None, None, "", "docker inspect"))
        prev = (restart_seen or {}).get(svc)
        if restart_seen is not None:
            restart_seen[svc] = st["restart_count"]
        if (prev is not None and st["restart_count"] > prev) or st["oom_killed"]:
            signals.append(Signal("container_restarts", svc, f"{svc} restarts={st['restart_count']} "
                                  f"oom_killed={str(st['oom_killed']).lower()}", float(st["restart_count"]), None,
                                  float(prev or 0), "restarts", "docker inspect"))
    return signals, now


class Sentinel:
    def __init__(self, ctx: Context, on_new_incident: Callable[[str], None]):
        self.ctx, self.on_new_incident = ctx, on_new_incident
        self.pending: dict[tuple[str, str], int] = {}
        self.learned: dict[str, dict] = {}
        self.learn_count: dict[str, int] = {}
        self.restart_seen: dict[str, int] = {}
        self._stop = threading.Event()

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        last_err = 0.0
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                if time.time() - last_err > 60:
                    self.ctx.bus.publish("sentinel", "error", f"sentinel tick failed: {exc}")
                    last_err = time.time()
            self._stop.wait(self.ctx.settings.sentinel_interval_s)

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> list[Signal]:
        if self.ctx.safety.frozen:
            return []
        with self.ctx.status.working("sentinel", None, "evaluating alert rules"):
            learned = {k: v for k, v in self.learned.items() if self.learn_count.get(k, 0) >= 3}
            signals, now = evaluate(self.ctx, learned, restart_seen=self.restart_seen)
        firing = {sg.key for sg in signals}
        self.pending = {k: self.pending.get(k, 0) + 1 for k in firing}
        confirmed = [sg for sg in signals if self.pending[sg.key] >= self.ctx.settings.confirm_ticks
                     or sg.rule in ("container_down", "container_restarts")]
        self._learn(now, {sg.service for sg in signals})
        self._clear(firing)
        if confirmed:
            self.raise_alerts(confirmed)
        return confirmed

    def _learn(self, now: dict[str, dict], noisy: set[str]) -> None:
        for svc, v in now.items():
            if svc in noisy or not v.get("rps"):
                continue
            prev = self.learned.get(svc)
            if prev is None:
                self.learned[svc] = dict(v)
            else:
                for k in ("rps", "error_ratio", "p95"):
                    if v.get(k) is not None and prev.get(k) is not None:
                        prev[k] = 0.8 * prev[k] + 0.2 * v[k]
                    elif v.get(k) is not None:
                        prev[k] = v[k]
            self.learn_count[svc] = self.learn_count.get(svc, 0) + 1

    def _clear(self, firing: set[tuple[str, str]]) -> None:
        for inc in self.ctx.store.open_incidents():
            if inc.phase not in EARLY_PHASES:
                continue
            changed = False
            for a in inc.alerts:
                if a.active and (a.rule, a.service) not in firing:
                    a.active = False
                    changed = True
                    self.ctx.bus.publish("sentinel", "alert", f"{a.id} cleared: {a.signal}", incident_id=inc.id)
            if changed:
                self.ctx.store.save(inc)

    # ------------------------------------------------------------ incidents
    def _suppressed(self, key: tuple[str, str]) -> bool:
        cutoff = time.time() - self.ctx.settings.realert_after_s
        for inc in self.ctx.store.list():
            if inc.terminal and inc.phase != "resolved" and _ts(inc.updated_at) > cutoff:
                if any((a.rule, a.service) == key for a in inc.alerts):
                    return True
        return False

    def raise_alerts(self, signals: list[Signal]) -> Optional[Incident]:
        store = self.ctx.store
        open_incs = store.open_incidents()
        signals = [sg for sg in signals if not self._suppressed(sg.key)]
        if not signals:
            return None
        # join only an incident still being investigated: once one is planned, a new signal is a new incident
        # (found live: late alerts attached to a verifying incident made it look resolved)
        target = next((i for i in open_incs if i.phase in EARLY_PHASES), None)
        fresh: list[Signal] = []
        for sg in signals:
            owner = next((i for i in open_incs if any((a.rule, a.service) == sg.key for a in i.alerts)), None)
            if owner is not None:
                self._touch(owner, sg)
            else:
                fresh.append(sg)
        if not fresh:
            return None
        created = target is None
        inc = target or Incident(id=store.next_incident_id(), title=f"{RULE_LABEL[fresh[0].rule]} on {fresh[0].service}")
        ledger = EvidenceLedger(store, self.ctx.bus, inc.id)
        for sg in fresh:
            a = Alert(id=f"A{len(inc.alerts) + 1}", rule=sg.rule, service=sg.service, signal=sg.signal,  # type: ignore[arg-type]
                      value=sg.value, threshold=sg.threshold, baseline=sg.baseline, unit=sg.unit, query=sg.query)
            inc.alerts.append(a)
            if sg.service not in inc.services:
                inc.services.append(sg.service)
            ledger.add("sentinel", "alert_rule", {"rule": sg.rule, "service": sg.service, "query": sg.query},
                       f"alert {a.id}: {sg.signal}", f"alert {a.id} {sg.rule}: {sg.signal}", agent="sentinel")
            self.ctx.bus.publish("sentinel", "alert", f"{a.id} {RULE_LABEL[sg.rule]}: {sg.signal}", incident_id=inc.id,
                                 detail=a.model_dump())
        store.save(inc)
        if created:
            self.ctx.bus.publish("sentinel", "phase", f"opened {inc.id}: {inc.title}", incident_id=inc.id)
            self.on_new_incident(inc.id)
        return inc

    def _touch(self, inc: Incident, sg: Signal) -> None:
        for a in inc.alerts:
            if (a.rule, a.service) == sg.key:
                if not a.active:
                    a.active = True
                    self.ctx.bus.publish("sentinel", "alert", f"{a.id} firing again: {sg.signal}", incident_id=inc.id)
                a.value, a.last_seen = sg.value, iso()
        self.ctx.store.save(inc)


def _ts(s: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
