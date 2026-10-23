# Incident Zero — Checkpoints

Each checkpoint has an exit test. A checkpoint is done only when its exit test passes; its status and the
evidence (test command, output) are recorded here and in BUILD_LOG.md.

| CP | Scope | Requirements | Exit test | Status |
|---|---|---|---|---|
| CP0 | REQUIREMENTS.md, CHECKPOINTS.md, BUILD_LOG.md | — | The three docs exist and agree with the brief and the Control Room decisions | done |
| CP1 | Foundation: settings, models (incl. the report schema), incident store, event bus, evidence ledger, redaction | F2.3, F7.2, R2, N2 | `pytest incident_zero/tests/test_foundation.py` | done (20 tests) |
| CP2 | Telemetry tools: Prometheus, Jaeger, logs (OpenSearch → docker logs), containers, topology; tool registry | F2.1, R1, R2, N4, N5 | `pytest incident_zero/tests/test_telemetry.py`; flag data never reaches evidence | done (12 tests) |
| CP3 | Action API: client adapter, mock server over Docker, allowlist + parameter validation, dry run, kill switch with rollback | F4.1, F4.3, F6.1, S1–S4 | `pytest incident_zero/tests/test_actions.py`: unknown action refused, dry run changes nothing, stop blocks calls, rollback runs; agents/telemetry cannot import flags or ground truth | done (10 tests) |
| CP4 | Agents: Sentinel, Investigator, Diagnostician, Evidence Auditor, Remediation Planner, Approval Gate, Executor, Verifier, Reporter; the orchestrator | F1–F7, R5, S5, N3 | `pytest incident_zero/tests/test_pipeline.py`: app fault → dry run, stopped cache → resolved live, failed fix → rollback + escalate, reject, send back, timeout, kill switch mid-approval, invented evidence rejected, LangGraph tool loop | done (10 tests; 51 total) |
| CP5 | Server: JSON API, server-sent events, email notification; CLI (`up`, `stop`, `resume`, `status`, `report`, `demo`, `practice`) | C9, C11, N1 | Server tests: decision endpoint enforces hash and approver; eval mode hides practice | done |
| CP6 | Control Room UI: React + router, all routes, approval flow, notifications, minimal dark style | C1–C11 | `npm run build` and typecheck pass; every route renders against a running server | done (a human approved a live incident through it) |
| CP7 | Practice: Simulator scenarios (flags, infra, combinations), hidden ground truth, Judge | P1–P4, R3 | Practice tests; ground truth unreadable from the tool layer | done (Judge too lenient: see LIVE_RESULTS) |
| CP8 | Live run on the OTel demo: practice incidents end to end | all | At least one application fault and one infrastructure fault detected, diagnosed with audited evidence, approved, executed (dry run and live), verified, reported | **partial**: brief faults 1–3 run live (stopped there on request): 1 pass, 2 missed, 3 invalid test; infrastructure faults not yet run. See LIVE_RESULTS.md |
| CP9 | Commit, push, draft PR, final BUILD_LOG update | — | Branch pushed, PR open, docs current | done |
| CP10 | Architecture-plan gaps: per-operation error alerting (LIVE_RESULTS gap 1); explainability computed in code (checklist, evidence score, ranked evidence, warnings) shown in the approval request and report | F1, F5, C-approval | `pytest incident_zero/tests/test_explain_and_ops.py`: a fault on one operation diluted below the service threshold is caught; too few or chronic errors are not; the explanation is deterministic, cites only real evidence, and warns when model confidence outruns the evidence | done (4 tests; not yet re-run live) |

Test count: 74 unit/integration tests (`pytest incident_zero/tests`).
