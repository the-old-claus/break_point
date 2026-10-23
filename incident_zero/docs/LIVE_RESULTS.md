# Live results on the running OpenTelemetry Demo

Everything below comes from the real, running demo (all 28 services: `compose.yaml` + `compose.full.yaml` +
`compose.observability.yaml` on Docker Desktop, 8 GB), real telemetry (Prometheus span metrics, Jaeger v2,
OpenSearch logs, Docker), the real agents (Gemini `gemini-3.5-flash-lite` through its OpenAI-compatible endpoint)
and the mock Action API acting on the real containers and flag file. Nothing is simulated. Date: 2026-10-23.

## Baseline

The Sentinel ran ten ticks (15 s apart) on the healthy demo: **no alerts, no incidents**.

## The brief's first three faults (live mode, approved as a test operator)

| # | Fault | Detected | Diagnosis | Action executed (Action API) | Verified | Verdict |
|---|---|---|---|---|---|---|
| 1 | Payment fails when charging (`paymentFailure` 100%) | 35 s, error rate on checkout and payment | payment / application_error, confidence 1.0, from the model; evidence audit passed; quotes the real "Invalid token" span errors | `toggle_feature_flag paymentFailure → off` | recovered: 2 triggering signals clear for 2 consecutive checks | **pass**, resolved in 175 s ([INC-0003](live_reports/INC-0003.md)) |
| 2 | Cart fails intermittently (`cartFailure` 50%) | **no** (421 s window) | — | — | — | **fail**: missed |
| 3 | Product catalog fails for one product (`productCatalogFailure`) | an unrelated checkout latency alert at 276 s | product-catalog latency (application_error, 0.9) | `toggle_feature_flag productCatalogLockContention → off` (was already off: no change) | latency cleared on its own | **invalid test** (the Judge's 10/10 is wrong) |

Also: the same payment fault approved by a human in the Control Room ([INC-0002](live_reports/INC-0002.md),
approver "preetham") went through approval → live execution → verification → report. Its recovery is not proof of
the fix: the trial runner had already reverted the fault (fixed since).

### Why fault 2 was missed (from Prometheus history)

`cartFailure` fails only cart's `EmptyCart` operation: up to 6 errors per 2 minutes, against cart's 1.3–1.7 req/s
of total traffic, about 3 % at service level, under the 5 % `error_rate` threshold. Checkout's calls to `EmptyCart`
failed too, but checkout's own server spans did not, so it raised nothing.

### Why fault 3 is not a valid test (from Prometheus history)

product-catalog recorded **zero** error spans for the whole run: the failing product was not requested by the load
generator in that window, so the fault never reached telemetry. The incident the agents handled was a separate,
natural latency spike on checkout; the planner chose a flag by its name (lock contention ↔ latency), and that flag
was already off.

## Found and fixed during the live runs

| Found | Fix |
|---|---|
| Jaeger 2.x serves only `/api/v3` (OTLP JSON) under `/jaeger/ui` | Jaeger client rewritten for v3, OTLP → trace converter |
| OpenSearch log documents use flat dotted keys (`resource: {"service.name": ...}`) | field lookup handles nested and dotted keys |
| payment/checkout run at ~0.02–0.03 req/s | `min_rps` 0.02 → 0.01 |
| A queue consumer (fraud-detection) going quiet because orders stopped fired "telemetry silent" first | silence needs ≥ 0.5 req/s at baseline |
| Late alerts joined an incident already being verified, so a wrong fix looked like a recovery | alerts join only incidents still being investigated |
| The model had no way to say "false alarm", so it invented a fault | `false_alarm` diagnosis with audited health evidence; no plan, report status `false_alarm` |
| The trial runner reverted the fault after the first incident | the campaign approves every incident a fault causes and reverts after all close |

## Known gaps (not fixed yet)

1. **Per-operation alerting**: an operation-level error rule (e.g. `EmptyCart` failing heavily while cart overall
   is fine) is needed for fault 2. *Implemented (CP10: `op_error_rate`, thresholds `op_error_ratio` 0.2 and
   `op_min_errors` 3); not yet re-run live.*
2. **Fault 3 needs traffic for the failing product** during its test (and a check that errors appear before the
   run counts).
3. **The Judge is too lenient**: it should check the exact flag/parameters acted on and that the injected signal
   was detected.
4. **No codebase knowledge**: the planner picks flags from `runbook.yaml` (written by hand from the demo source) by
   name; a `code_search` tool over the demo source would let it pick the flag guarding the failing code path.
5. Faults 4–10 are not yet tested live.

## Reproduce

```bash
incident_zero demo up && incident_zero action-api &   # then:
IZ_ENV_FILE=<repo>/.env incident_zero up &
incident_zero practice campaign payment-charge-fails,cart-intermittent,catalog-one-product --as <name> --live
```
