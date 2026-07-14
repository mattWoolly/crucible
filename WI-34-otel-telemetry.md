# WI-34 — Native OpenTelemetry Telemetry (`@determa/telemetry`)

## Goal

Emit OpenTelemetry **metrics and traces** from Determa to a local OTLP collector so
per-provider / per-model usage (tokens, cost, latency), agent-turn spans, and tool-call
spans land in the existing self-hosted Prometheus + Loki + Grafana stack — alongside the
Claude Code and Codex telemetry already flowing there.

Determa uses multiple providers/models (e.g. `minimax`, `glm`, anthropic, openai-compatible
gateways). Telemetry MUST be dimensioned by `providerId` and `modelId` so usage is separable
per model.

## Non-goals

- No changes to provider adapter wire logic (`@determa/providers`).
- No logging of prompt/response content by default (privacy). Content/attribute logging is a
  future opt-in flag, off by default.
- No vendor cloud. OTLP goes to the local collector only.

## Environment it targets (already running)

- **OTel Collector** (`otelcol`, otel/opentelemetry-collector-contrib:0.130.0) in
  `~/projects/docker-files/monitoring/`.
  - OTLP gRPC: `http://localhost:4317`
  - OTLP HTTP: `http://localhost:4318`
  - Metrics → Prometheus scrape `:8889`; logs/traces → Loki OTLP.
- Prometheus datasource uid `cfp9mcjjwhhq8d`, Loki uid `cfrnmtv9l2rcwe`, Grafana `:3000`.
- Existing Grafana dashboard uid `ai-agents` — extend it with a Determa row (see §7).

Use **gRPC → :4317** to match the Claude Code exporter path (Codex uses HTTP :4318). Either
works; gRPC chosen for consistency.

## 1. New package: `@determa/telemetry`

Create `packages/telemetry/` (mirror an existing leaf package's `package.json`/`tsconfig`,
e.g. `@determa/journal`). It depends only on `@determa/types` plus the OTel SDK.

### Dependencies (pin exact versions; respect root `pnpm-workspace.yaml`
`minimumReleaseAgeExclude` if a fresh release must be allowlisted)

```
@opentelemetry/api
@opentelemetry/sdk-node                        # or sdk-metrics + sdk-trace-node directly
@opentelemetry/exporter-metrics-otlp-grpc
@opentelemetry/exporter-trace-otlp-grpc
@opentelemetry/resources
@opentelemetry/semantic-conventions
```

ESM only (`"type": "module"`, matches repo). Verify OTel packages resolve under NodeNext
module resolution used by `tsconfig.base.json`.

### Public API (keep tiny + injectable, mirror the HookBus/CoreSessionDeps seam pattern)

```ts
export interface TelemetryConfig {
  enabled: boolean;                 // gate everything; default false
  endpoint?: string;                // default 'http://localhost:4317'
  serviceName?: string;             // default 'determa'
  environment?: string;             // resource attr 'env', default hostname or 'dev'
  metricExportIntervalMs?: number;  // default 10000
  traces?: boolean;                 // default true when enabled
}

export interface Telemetry {
  /** Record a completed model call's usage. Labels: provider, model, type. */
  recordUsage(args: {
    providerId: string;
    modelId: string;
    usage: Usage;                   // from @determa/types
    reason?: 'complete' | 'budget' | 'aborted' | 'error';
  }): void;

  /** Start a span for one agent turn (model call). Returns end() handle. */
  startTurn(args: { providerId: string; modelId: string; sessionId?: string }): TurnSpan;

  /** Start a span for one tool call. Nested under the active turn span. */
  startToolCall(args: { name: string }): ToolSpan;

  /** Counter for session spinups. */
  recordSessionStart(args?: { env?: string }): void;

  shutdown(): Promise<void>;        // flush + close exporters
}

export interface TurnSpan { end(args?: { error?: string }): void; }
export interface ToolSpan { end(args?: { ok: boolean; error?: string }): void; }

export function createTelemetry(cfg: TelemetryConfig): Telemetry;   // real SDK
export function noopTelemetry(): Telemetry;                          // used when disabled
```

`createTelemetry` when `enabled:false` MUST return the same no-op object as `noopTelemetry()`
so callers never branch. All hot-path methods are cheap/sync (OTel counters + span start are
non-blocking); only `shutdown()` awaits.

## 2. Metrics to emit (names + attributes)

Follow OTel metric naming; the collector's `resource_to_telemetry_conversion` turns resource
attrs into Prometheus labels, and the OTLP→Prometheus translation appends `_total` to counters.

| Instrument | OTel name | Type | Attributes |
|---|---|---|---|
| Token usage | `determa.token.usage` | Counter (Int) | `provider`, `model`, `type` = input\|output\|cacheRead\|cacheWrite\|reasoning |
| Cost | `determa.cost.usage` | Counter (Double, USD) | `provider`, `model` |
| Session count | `determa.session.count` | Counter | `env` |
| Turn duration | `determa.turn.duration` | Histogram (ms) | `provider`, `model`, `reason` |
| Tool call count | `determa.tool.count` | Counter | `tool`, `ok` |
| Tool duration | `determa.tool.duration` | Histogram (ms) | `tool` |

`recordUsage` fans `usage` into the token counter with one add per non-zero `type`, and adds
`usage.costUsd` (if present) to the cost counter.

**Resource attributes** (set once on the SDK Resource): `service.name=determa`,
`service.version=<pkg version>`, `env=<config.environment>`. These become Prometheus labels
`service_name`, `service_version`, `env`.

Prometheus metric names will surface as e.g. `determa_token_usage_total{provider="minimax",
model="minimax-m3",type="output"}` and `determa_cost_usage_total{provider="glm",...}`.

## 3. Traces to emit

- **Turn span** `determa.turn` — one per model call. Attributes: `provider`, `model`,
  `session.id` (span attr, NOT a metric label — keep out of metrics for cardinality),
  `stop.reason`. Span status = error when `reason==='error'`.
- **Tool span** `determa.tool` — child of the active turn span. Attributes: `tool.name`,
  `ok`. Record exceptions on error.
- Traces export to the same collector → Loki OTLP (traces are stored/queried via Loki OTLP
  ingest per current stack; if a dedicated trace store is added later, only the collector
  config changes, not Determa).

## 4. Wiring points (exact locations)

All wiring rides existing injectable seams — do NOT hardcode a global singleton in core.

### 4a. `CoreSessionDeps` (packages/core/src/agent-loop/deps.ts)
Add optional field `telemetry?: Telemetry`. Because it's optional, the frozen
`SessionDeps`/`CreateSessionFn` contract stays structurally satisfied (same rationale the file
already documents for `hookBus`/`compact`). When absent, `createSession` uses
`noopTelemetry()`.

### 4b. Agent loop (packages/core/src/agent-loop/session.ts)
- The loop already computes `callUsage` and emits `{type:'done', reason, usage}` at the
  labeled lines (~849, ~921, ~942, ~946, ~990, ~1006, plus `handleAbort` ~689). At the point
  each model call's real `message.usage` is finalized (~955–984, where `addUsage` is applied),
  call `telemetry.recordUsage({ providerId, modelId, usage: message.usage })` with the
  `ModelRef` for that call. Record ONCE per model call (per-call `message.usage`), NOT on the
  aggregated `done` — to avoid double counting.
- Wrap each model call in `telemetry.startTurn(...)` / `turn.end({error})`; capture duration
  and stop reason.
- At tool dispatch (~963 tool_call filtering / ~1022 hookBus) wrap execution in
  `telemetry.startToolCall({name})` / `tool.end({ok,error})`.
- `recordSessionStart()` once in `createSession`.
- `providerId`/`modelId` come from the call's `ModelRef` (`cfg.model` / resolved per-call
  model). Confirm the exact `ModelRef` in scope at each call site.

### 4c. Composition roots (init + shutdown the SDK once per process)
- **`packages/cli/src/commands/serve.ts`** — build `createTelemetry(cfg)` at startup, pass
  into the `buildSession` deps closure, and call `telemetry.shutdown()` inside the existing
  `shutdown()` (the one that "aborts/awaits any active prompts", ~line 39). Add to SIGINT/
  SIGTERM path.
- **`packages/cli/src/commands/exec.ts`** — headless path: build telemetry, pass to session
  deps, `await telemetry.shutdown()` before process exit (headless runs are short; flush
  matters — set a short export interval or force-flush on shutdown).
- Do NOT init the SDK inside `@determa/core` — core stays a library; process lifecycle belongs
  to the CLI composition roots.

## 5. Configuration

Read telemetry config from the existing config layer (`.determa/config.json`, see
`packages/cli/src/config.ts`) with env overrides. Precedence: env > config file > defaults.

- Env: `DETERMA_OTEL=1` (enable), `DETERMA_OTEL_ENDPOINT`, `DETERMA_OTEL_ENV`,
  `DETERMA_OTEL_TRACES=0` to disable traces.
- Default **disabled**. When disabled, zero OTel code runs (noop object).

## 6. Tests (vitest, matches repo)

- `noopTelemetry()` methods are safe no-ops; `createTelemetry({enabled:false})` deep-equals
  noop behavior.
- `recordUsage` fans a `Usage` into the correct per-`type` counter adds + cost add (inject a
  fake meter/exporter; assert recorded points). Zero-valued types are skipped.
- Session/agent-loop test injects a fake `Telemetry` via `CoreSessionDeps.telemetry` and
  asserts: one `recordUsage` per model call (no double count on `done`), turn span
  start/end pairing, tool span nesting, `recordSessionStart` once.
- No network in unit tests — exporter is faked/injected.
- Lint + typecheck clean (`pnpm lint`, `pnpm typecheck`).

## 7. Grafana dashboard (post-merge, ops step — not code)

Extend dashboard uid `ai-agents` (source JSON
`~/projects/docker-files/monitoring/grafana-ai-agents-dashboard.json`) with a "Determa" row:
- Token rate by model: `sum by (model) (rate(determa_token_usage_total[5m]))`
- Token rate by type: `sum by (type) (rate(determa_token_usage_total{model=~"$model"}[5m]))`
- Cost rate by model: `sum by (model) (rate(determa_cost_usage_total[5m]))`
- Turn latency p95: `histogram_quantile(0.95, sum by (le,model) (rate(determa_turn_duration_milliseconds_bucket[5m])))`
- Tool call rate by tool + tool error rate
- Session spinups: `rate(determa_session_count_total[10m])`
Add `provider`/`model` template vars sourced from `label_values(determa_token_usage_total, model)`.
(The `$model` template var already exists on the dashboard; confirm it queries Determa metrics too.)

## 8. Acceptance criteria

1. `pnpm build && pnpm typecheck && pnpm lint && pnpm test` all pass.
2. With `DETERMA_OTEL=1`, running a real `determa exec` or `determa serve` prompt against a
   minimax and a glm model produces, on the collector `:8889` endpoint:
   `determa_token_usage_total{provider=...,model="minimax-...",type="output"}` and the glm
   equivalent, plus `determa_cost_usage_total`, `determa_session_count_total`, and
   `determa_turn_duration_milliseconds_*`.
3. Turn + tool spans arrive in Loki (queryable by `service_name="determa"`).
4. With telemetry disabled (default), no OTel network activity and no measurable hot-path
   overhead.
5. No prompt/response content leaves the process (only counts, cost, model ids, tool names,
   durations, stop reasons).

## 9. Verification commands (for the implementing agent)

```bash
# metrics present on collector after a real run:
curl -s http://172.17.0.1:8889/metrics | grep -E '^determa_' | sort -u
# prometheus scraped them:
curl -s 'http://localhost:9090/api/v1/query?query=sum by (model)(determa_token_usage_total)'
# logs/traces in loki:
curl -s -G http://localhost:3100/loki/api/v1/query_range -H 'X-Scope-OrgID: fake' \
  --data-urlencode '{service_name="determa"}'
```

## Notes / gotchas

- OTLP→Prometheus appends `_total` to counters and converts `.`→`_`; histograms get
  `_milliseconds_bucket/_sum/_count`. Name instruments accordingly (use the `.` form in code).
- Keep high-cardinality ids (`session.id`, any request id) as SPAN attributes only, never
  metric attributes. The collector also drops known high-card keys, but don't rely on it for
  Determa's own names.
- Delta vs cumulative temporality: the default OTLP metrics temporality works with this
  Prometheus setup (Claude Code uses delta successfully). If counters look wrong in Grafana,
  set `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` on the process, or the
  SDK equivalent.
- Force-flush on headless `exec` shutdown or short runs may export nothing before exit.
```
