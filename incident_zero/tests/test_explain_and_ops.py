"""Per-operation alerting (the missed intermittent cart fault) and the code-computed explanation."""
from __future__ import annotations

from incident_zero.agents.context import Context
from incident_zero.agents.sentinel import evaluate, op_signals
from incident_zero.agents.facts import Fact
from incident_zero.explain import anomalous, band, explain, to_markdown
from incident_zero.llm import LLM

from . import fakes
from .test_pipeline import build, payment_failure, tick_until_incident, wait_phase


class OpProm(fakes.FakeProm):
    """FakeProm plus per-(service, operation) span metrics."""

    def __init__(self, svcs, ops_now, ops_base):
        super().__init__(svcs)
        self.ops_now, self.ops_base = ops_now, ops_base  # {(svc, op): (rps, error_rps)}

    def query(self, promql, at=None):
        if "by (service_name, span_name)" in promql:
            ops = self.ops_base if " offset " in promql else self.ops_now
            i = 1 if "STATUS_CODE_ERROR" in promql else 0
            return [{"metric": {"service_name": s, "span_name": o}, "value": [0, str(v[i])]}
                    for (s, o), v in ops.items()]
        return super().query(promql, at)


def ctx_with(settings, store, bus, svcs, ops_now, ops_base):
    tel = fakes.telemetry(settings, svcs)
    tel.prom = OpProm(svcs, ops_now, ops_base)
    return Context(settings=settings, store=store, bus=bus, telemetry=tel, actions=None, safety=None,  # type: ignore[arg-type]
                   llm=LLM())


def cart_intermittent():
    svcs = fakes.healthy()
    svcs["cart"].now = {"rps": 1.5, "error_ratio": 0.03, "p95": 20}   # 3 % at service level: under error_ratio
    svcs["cart"].base = {"rps": 1.5, "error_ratio": 0.0, "p95": 20}
    now = {("cart", "oteldemo.CartService/EmptyCart"): (0.08, 0.045), ("cart", "oteldemo.CartService/GetCart"): (1.4, 0.0)}
    base = {("cart", "oteldemo.CartService/EmptyCart"): (0.08, 0.0), ("cart", "oteldemo.CartService/GetCart"): (1.4, 0.0)}
    return svcs, now, base


def test_operation_rule_catches_a_fault_diluted_at_service_level(settings, store, bus):
    svcs, now, base = cart_intermittent()
    ctx = ctx_with(settings, store, bus, svcs, now, base)
    signals, _ = evaluate(ctx)
    assert not [s for s in signals if s.rule == "error_rate"]
    ops = [s for s in signals if s.rule == "op_error_rate"]
    assert [s.service for s in ops] == ["cart"]
    assert "operation=oteldemo.CartService/EmptyCart" in ops[0].signal
    assert ops[0].value and ops[0].value > settings.thresholds.op_error_ratio


def test_operation_rule_ignores_too_few_errors_and_chronic_errors(settings, store, bus):
    svcs, now, base = cart_intermittent()
    few = {k: (v[0], 0.01 if v[1] else 0.0) for k, v in now.items()}       # ~1 error in 2 minutes
    assert op_signals(ctx_with(settings, store, bus, svcs, few, base), "2m", set()) == []
    assert op_signals(ctx_with(settings, store, bus, svcs, now, now), "2m", set()) == []   # same at baseline
    assert op_signals(ctx_with(settings, store, bus, svcs, now, base), "2m", {"cart"}) == []  # service rule fired


def test_explanation_checklist_ranking_and_report(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    x = inc.explanation
    assert x["component"] == "payment"
    passed = {c["check"] for c in x["checklist"] if c["passed"]}
    assert {"traces", "alert"} <= passed
    assert x["score"] == round(sum(c["weight"] for c in x["checklist"] if c["passed"]), 2)
    assert x["band"] == band(x["score"])
    assert x["ranking"] and x["ranking"][0]["trace_link"] == 1.0          # the trace origin ranks first
    ev_ids = {e.id for e in store.evidence(inc_id)}
    assert {r["evidence_id"] for r in x["ranking"]} <= ev_ids               # nothing invented
    md = to_markdown(x)
    assert "## Why this diagnosis" in md and "payment" in md
    again = explain(inc, store.evidence(inc_id))
    assert again["score"] == x["score"]                                     # deterministic


def test_explanation_warns_when_model_confidence_outruns_the_evidence(settings, store, bus):
    svcs, traces = payment_failure()
    orch, _ = build(settings, store, bus, svcs, traces)
    inc_id = tick_until_incident(orch)
    orch.open(inc_id)
    inc = wait_phase(store, inc_id, ("awaiting_approval",))
    d = inc.diagnosis.model_copy(deep=True)
    d.root_causes[0].component = "currency"        # a component the evidence does not point at
    d.root_causes[0].confidence = 0.95
    x = explain(inc, store.evidence(inc_id), d)
    assert x["band"] == "low"
    assert any("well above the evidence score" in w for w in x["warnings"])


def test_only_abnormal_state_corroborates():
    ok = {"state": "running", "exit_code": "0", "oom": False, "restarts": 0, "networks": "demo"}
    assert not anomalous(Fact("container", "cart", "E3", "cart: state=running", ok))
    assert anomalous(Fact("container", "cart", "E3", "cart: state=exited", {**ok, "state": "exited"}))
    assert not anomalous(Fact("resources", "ad", "E4", "", {"cpu": 5.0, "throttled": 0, "memory_percent": 30.0}))
    assert anomalous(Fact("resources", "ad", "E4", "", {"cpu": 97.0, "throttled": 0, "memory_percent": 30.0}))
    assert anomalous(Fact("datastore", "valkey-cart", "E7", "", {"stale_s": None}))
