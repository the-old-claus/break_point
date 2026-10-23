# Incident Zero — an AI on-call engineer

Detects an incident in the OpenTelemetry Demo, investigates it with real telemetry, names the root cause with
evidence that exists in the collected data, proposes a fix from the Action API's allowed actions, waits for a human,
executes through the Action API only, verifies recovery (rolling back and escalating if it fails), and writes a
JSON + Markdown incident report.

> **Status.** Tested live on the full running demo for the brief's first three faults: payment failure passes end
> to end; the intermittent cart failure is missed (needs per-operation alerting); the one-product catalog failure
> did not reach telemetry in its run. Details and open gaps: [LIVE_RESULTS](docs/LIVE_RESULTS.md).

Docs: [REQUIREMENTS](docs/REQUIREMENTS.md) · [CHECKPOINTS](docs/CHECKPOINTS.md) · [BUILD_LOG](docs/BUILD_LOG.md)
(what is built and how) · [LIVE_RESULTS](docs/LIVE_RESULTS.md) · [report schema](schema/incident_report.schema.json)
· [brief](docs/hackathon_brief.json) · real reports: [INC-0002](docs/live_reports/INC-0002.md),
[INC-0003](docs/live_reports/INC-0003.md)

## Setup

```bash
pip install -e '.[incident_zero]'               # from the repository root (Python 3.10+)
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-demo ~/Developer/opentelemetry-demo
cp incident_zero/incident_zero.example.yaml incident_zero.yaml   # set demo_dir
incident_zero ui-build                          # builds the Control Room (needs Node 20+)
```

The model is configured in `.env` exactly as for Break Point: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`,
`LLM_API_KEY` (any OpenAI-compatible endpoint, Anthropic, or a local Ollama model). Without one, every agent falls
back to its deterministic path. SMTP settings in `.env` enable approval emails.

## Run

```bash
incident_zero demo up          # the demo + Jaeger, Prometheus, OpenSearch, Grafana (about 6 GB of Docker memory)
incident_zero action-api       # the mock Action API, its own process (the only thing that changes the system)
incident_zero up               # Sentinel + agents + Control Room on http://127.0.0.1:8700
incident_zero up --eval        # the same with practice switched off entirely
```

Practice (not in eval mode): `/practice` in the Control Room, or `incident_zero practice list | run <id> | runs`.

## The agents

| # | Agent | Model | Job |
|---|---|---|---|
| 1 | Sentinel | no | Alert rules on error rate (per service and per operation), p95 latency, traffic, silence, container state; correlates alerts into one incident |
| 2 | Investigator | yes (LangGraph tool loop; playbook fallback) | Picks the next telemetry tool from findings so far; every result becomes evidence `E<n>` |
| 3 | Diagnostician | yes (rules fallback) | Component, root cause, confidence, claims that quote evidence |
| 4 | Evidence Auditor | no | Cited ids exist, quotes are verbatim, every alert explained; else back to the Investigator. Then scores the diagnosis in code: a checklist of independent signals, ranked evidence and warnings (`explain.py`) |
| 5 | Remediation Planner | choice only | Allowed actions only, validated by code, previewed as a dry run, hashed |
| 6 | Approval Gate | human | Approve / reject / send back; timeout escalates |
| 7 | Executor | no | Action API only; kill switch checked before every call |
| 8 | Verifier | no | Re-checks the triggering signals; rolls back and escalates if not recovered |
| 9 | Reporter | wording only | Report built by code from the record, validated against the schema |

## How the approval gate works

The plan (actions, parameters, reasons, the diagnosis summary) is hashed when it is shown. Approving requires the
approver's name and that hash: a plan that changed after it was displayed cannot be approved. Send back needs a note,
which goes to the Investigator. No decision before `approval_timeout_s` escalates the incident and nothing runs.
Decide in the Control Room (`/`, `/approvals`, `/incidents/<id>/plan`) or with
`incident_zero decide INC-0001 approve --as <name>`.

## How the approver sees why

Before a plan is shown, `explain.py` scores the diagnosis in code, with no model involved:

- **Checklist:** traces show the failure originates in the component (0.30), an alert fired on it (0.20), its own
  logs show errors (0.20), metrics or container state show it is abnormal (0.20; a healthy container does not count), only one root cause survived (0.10).
  The passed weights sum to the evidence score; 0.70+ is high, 0.40–0.69 medium, below that low.
- **Most important evidence:** every evidence item is ranked by trace link, signal strength, service match and
  whether it was cited, so the approver sees which evidence matters without extra model calls.
- **Warnings:** a model confidence more than 0.30 above the evidence score, or decisive cited evidence the data
  ranks low; plus the evidence kinds that were never collected (`not_checked`).

It appears in the approval request, in the `approval` event, on the incident (`explanation`) and as a
"Why this diagnosis" section in the Markdown report.

## How the kill switch works

"Stop all" (top bar, `POST /api/stop`, or `incident_zero stop --as <name>`, which works without the server) writes
the STOP file, tells the Action API to refuse every execution, and rolls back reversible actions executed in the last
`kill_rollback_window_s`. Agents freeze where they stand. It stays engaged until an explicit resume. Dry run is on at
start and shown in the top bar; going live is a confirmed, deliberate switch.

## Integrity

- Feature-flag data is stripped from every trace, log and metric before it becomes evidence; flagd's logs and flag
  queries are refused. Tests enforce it, and the agents cannot import the flag file or the practice ground truth.
- Evidence quotes are checked verbatim by code: invented evidence cannot reach a human.

## Known limitations

- Action API parameter names are a guess; the real API is one adapter (`actions/client.py`).
- Faults whose cause is a CPU/memory limit or a detached network have no allowed fix: the agent escalates.
- `toggle_feature_flag` uses the demo source's knowledge of which flags guard each service (`runbook.yaml`); it
  never reads flag values. Confirm with the organisers that this is acceptable.
- Kafka (`compose.full.yaml`) is off by default to fit 8 GB of Docker memory, so Kafka incidents are untested.
- Diagnosis accuracy on unseen incidents is measured with the practice Judge, not guaranteed.
