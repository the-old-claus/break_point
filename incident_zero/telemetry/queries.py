"""PromQL for the demo's span metrics (the collector's span_metrics connector, exported over OTLP to Prometheus).

Request rate, error ratio and latency per service come from server and consumer spans; a service that is down
emits nothing itself, so its callers' client spans are the other half of the picture.
"""
from __future__ import annotations

CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
INBOUND = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
OUTBOUND = 'span_kind=~"SPAN_KIND_CLIENT|SPAN_KIND_PRODUCER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'


def _sel(*parts: str) -> str:
    return "{" + ",".join(p for p in parts if p) + "}"


def _off(offset_s: float) -> str:
    return f" offset {int(offset_s)}s" if offset_s else ""


def _svc(service: str | None) -> str:
    return f'service_name="{service}"' if service else ""


def rps(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return f"sum by ({by}) (rate({CALLS}{_sel(INBOUND, _svc(service))}[{window}]{_off(offset_s)}))"


def error_rps(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return f"sum by ({by}) (rate({CALLS}{_sel(INBOUND, ERROR, _svc(service))}[{window}]{_off(offset_s)}))"


def op_rps(window: str, service: str | None = None, offset_s: float = 0) -> str:
    """Request rate per (service, operation): a fault on one operation can hide inside a busy service's total."""
    return rps(window, service, offset_s, by="service_name, span_name")


def op_error_rps(window: str, service: str | None = None, offset_s: float = 0) -> str:
    return error_rps(window, service, offset_s, by="service_name, span_name")


def window_seconds(window: str) -> float:
    """'2m' -> 120.0; '30s' -> 30.0; '1h' -> 3600.0 (PromQL range syntax, single unit)."""
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        return float(window[:-1]) * units[window[-1]]
    except (KeyError, ValueError, IndexError):
        return 120.0


def p95(window: str, service: str | None = None, offset_s: float = 0, by: str = "service_name") -> str:
    return (f"histogram_quantile(0.95, sum by ({by}, le) "
            f"(rate({DURATION}{_sel(INBOUND, _svc(service))}[{window}]{_off(offset_s)})))")


def client_error_rps(window: str, service: str) -> str:
    """Errors on the calls a service makes, by operation: which dependency is failing it."""
    return f"sum by (span_name) (rate({CALLS}{_sel(OUTBOUND, ERROR, _svc(service))}[{window}]))"


def client_p95(window: str, service: str) -> str:
    return (f"histogram_quantile(0.95, sum by (span_name, le) "
            f"(rate({DURATION}{_sel(OUTBOUND, _svc(service))}[{window}])))")


def last_seen(service: str | None = None) -> str:
    """Seconds since each service last reported a span (absence is a signal)."""
    return f"time() - max by (service_name) (timestamp({CALLS}{_sel(_svc(service))}))"


DATASTORE_PREFIXES = {"valkey": "redis", "redis": "redis", "postgres": "postgresql", "astronomy-db": "postgresql",
                      "kafka": "kafka"}


def datastore_prefix(service: str) -> str | None:
    for k, v in DATASTORE_PREFIXES.items():
        if k in service:
            return v
    return None
