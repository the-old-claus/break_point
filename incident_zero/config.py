"""Settings, layered: process environment (IZ_*) > incident_zero.yaml > defaults.

The LLM is configured exactly as for Break Point (LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL / LLM_API_KEY in .env) and
resolved through break_point.config, so there is one place to set a model. Telemetry endpoints left empty are
discovered from the running Compose project's published ports (telemetry.discovery), because the demo publishes
Jaeger and OpenSearch on random host ports.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
CONFIG_FILE = "incident_zero.yaml"

# The agent must never read feature-flag settings (REQUIREMENTS R1). These services are flag infrastructure: their
# logs and configuration are off limits to every tool. Their health and request metrics are not.
FLAG_SERVICES = ("flagd", "flagd-ui")


@dataclass
class Thresholds:
    error_ratio: float = 0.05        # share of a service's server spans with status ERROR
    min_rps: float = 0.01            # below this request rate a service's error ratio is noise (payment and
                                     # checkout run at ~0.02-0.03 req/s under the demo's default load)
    latency_p95_floor_ms: float = 800.0
    latency_factor: float = 3.0      # p95 above factor x baseline (and above the floor)
    traffic_factor: float = 3.0      # entry request rate above factor x baseline
    silence_after_s: float = 180.0   # a service that sent spans at baseline and none for this long
    silent_min_rps: float = 0.5      # only services this busy at baseline count as "gone silent" (queue consumers
                                     # like fraud-detection idle naturally when upstream traffic stops)
    op_error_ratio: float = 0.2      # one operation's share of failing server spans (catches a fault on a single
                                     # operation that is diluted below error_ratio at service level, e.g. EmptyCart)
    op_min_errors: float = 3.0       # and at least this many failing spans of that operation in the window


@dataclass
class Settings:
    data_dir: str = ""                       # incidents, events, mode; default <repo>/.incident_zero
    practice_dir: str = ""                   # practice ground truth; default <repo>/.incident_zero_practice
    compose_project: str = "opentelemetry-demo"
    demo_dir: str = ""                       # a checkout of open-telemetry/opentelemetry-demo
    # every service: base + Kafka group + observability, in the demo Makefile's own order
    compose_files: list[str] = field(default_factory=lambda: ["compose.yaml", "compose.full.yaml",
                                                              "compose.observability.yaml"])
    frontend_url: str = "http://127.0.0.1:8080"
    prometheus_url: str = ""
    jaeger_url: str = ""
    opensearch_url: str = ""
    action_api_url: str = "http://127.0.0.1:8701"
    action_api_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8700
    dry_run: bool = True                     # the starting mode; the live mode is persisted in data_dir/state.json
    eval_mode: bool = False                  # true: practice route, API and Simulator do not exist
    llm_enabled: bool = True
    approval_timeout_s: float = 600.0
    sentinel_interval_s: float = 15.0
    rate_window: str = "2m"                  # PromQL range for rates and quantiles
    baseline_s: float = 600.0                # how far back the baseline looks
    correlation_window_s: float = 180.0
    confirm_ticks: int = 2                   # a signal must hold this many Sentinel ticks before it is an alert
    realert_after_s: float = 900.0           # same signal after an unresolved incident closed: stay quiet this long
    entry_services: list[str] = field(default_factory=lambda: ["frontend-proxy", "frontend"])
    investigator_budget: int = 16            # tool calls per investigation round
    audit_retries: int = 2                   # send-backs from the Auditor before rule-based diagnosis takes over
    max_plan_actions: int = 3
    verify_settle_s: float = 30.0
    verify_window_s: float = 240.0
    verify_consecutive: int = 2
    kill_rollback_window_s: float = 600.0
    thresholds: Thresholds = field(default_factory=Thresholds)
    ignore_services: list[str] = field(default_factory=lambda: ["load-generator", "telemetry-docs", "flagd-ui",
                                                                "grafana", "opamp-server", "otelcol-contrib",
                                                                "jaeger", "frontend-web"])
    notify_email: str = ""                   # approval mail goes here when SMTP is configured in .env
    report_schema_file: str = ""             # the official report schema from the template repo; default: ours
    loadgen_url: str = ""                    # Locust web UI; default <frontend_url>/loadgen
    normal_load_users: int = 5               # the demo's LOCUST_USERS: what throttle_load_generator restores

    def path(self, name: str) -> Path:
        return Path(getattr(self, name)).expanduser().resolve()


ENV_PREFIX = "IZ_"


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def load_dotenv() -> None:
    """Make .env (cwd, then the repository root) visible to break_point.config without overriding the shell."""
    for p in (Path(os.environ.get("IZ_ENV_FILE", "")), Path.cwd() / ".env", REPO_ROOT / ".env"):
        if str(p) and p.is_file():
            for k, v in _dotenv(p).items():
                os.environ.setdefault(k, v)


def _coerce(cur, raw: str):
    if isinstance(cur, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(cur, int):
        return int(raw)
    if isinstance(cur, float):
        return float(raw)
    if isinstance(cur, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def load_settings(path: Optional[str] = None) -> Settings:
    load_dotenv()
    s = Settings()
    cfg = Path(path or os.environ.get("IZ_CONFIG") or (Path.cwd() / CONFIG_FILE))
    if not cfg.is_file():
        cfg = REPO_ROOT / CONFIG_FILE
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text()) or {}
        for f in fields(Settings):
            if f.name == "thresholds":
                for k, v in (data.get("thresholds") or {}).items():
                    if hasattr(s.thresholds, k):
                        setattr(s.thresholds, k, type(getattr(s.thresholds, k))(v))
            elif f.name in data and data[f.name] is not None:
                setattr(s, f.name, data[f.name])
    for f in fields(Settings):
        raw = os.environ.get(ENV_PREFIX + f.name.upper())
        if raw not in (None, "") and f.name != "thresholds":
            setattr(s, f.name, _coerce(getattr(s, f.name), raw))
    s.data_dir = s.data_dir or str(REPO_ROOT / ".incident_zero")
    s.practice_dir = s.practice_dir or str(REPO_ROOT / ".incident_zero_practice")
    return s


def break_point_settings():
    """Break Point's Settings, for the LLM and SMTP fields only."""
    load_dotenv()
    from break_point.config import load_settings as bp_load

    return bp_load()
