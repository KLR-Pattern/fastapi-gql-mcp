# Changelog

## Unreleased

### Added

- Routes explicitly annotated `-> None` (204-style deletes and
  side-effect calls) now bridge as a Boolean success field instead of
  being skipped as untyped: the call is the point, and "no response
  body" is an explicit contract — `gone(id): Boolean` returns true on
  2xx, failures surface as field errors as usual. Endpoints with NO
  return annotation at all still skip ("no typed response"): an absent
  annotation is no contract, and silently bridging it would let refactors
  degrade the schema without a peep.

### Fixed

- Query-parameter lists with mixed None items no longer stringify None
  into the literal value `"None"` (`?tag=None`): None items are dropped,
  an all-None list is simply not sent.

### Changed

- Every skip reason now states the remedy, not just the problem — e.g.
  "query parameter model mixed with plain query parameters is not
  supported — move the plain parameters into the model (FastAPI itself
  rejects the mixed form on the wire)"; required-header skips point at
  passthrough_headers, hidden-route skips at include_hidden, form/file
  skips note the route stays available over plain HTTP. Startup logs are
  the only place these explanations surface, so they teach rather than
  merely report.
- Parametrized generic models (`Page[Item]`) get clean GraphQL type names
  (`Page_Item`) derived from the generic origin plus argument names,
  instead of the sanitizer mangling the source spelling
  (`Page[Item].__name__` → `Page_Item_` with a warning). Uniqueness per
  parameterization is preserved and name collisions still dedup.

## 0.6.0 (2026-10-06)

### Added

- Non-Optional union responses (`-> Item | Error`, OpenAPI 3.1 oneOf style)
  now bridge as the JSON scalar instead of vanishing: which member arrives
  is a runtime decision, so the field carries no per-member promises —
  descriptions name the possible shapes ("raw JSON whose shape is one of:
  Item, Error"). Granularity is field-level: a union nested in a response
  model degrades only that field (`result: JSON!`), the rest stays
  selectable. Previously these routes (and any route with a union-typed
  field) were skipped entirely with "unsupported type". Request-body unions
  still skip — GraphQL has no input unions. Degraded routes are announced
  at startup ("bridged N route(s) as raw JSON (no field selection)" naming
  the union or the filtering kwarg, plus how to restructure to regain
  field selection) — the same notice covers response-filtering routes.
- Routes marked `deprecated=True` now map onto GraphQL-native deprecation:
  the field carries `@deprecated(reason: ...)` in the SDL, disappears from
  default introspection listings (`includeDeprecated: true` still shows
  it), and progressive-discovery briefs gain a `"deprecated": true` flag.
  Deprecated fields remain executable — deprecation is metadata, not
  access control. Previously the flag was silently ignored and deprecated
  routes surfaced as ordinary fields.

### Fixed

- `__version__` is now derived from installed package metadata instead of
  a hardcoded copy — the copy was left at 0.4.0 when 0.5.0 shipped, and
  the published package reported the wrong runtime version. pyproject is
  the single source of truth; a drift test pins it.
- Example: `legacy_stats` returned a coroutine (`return stats()` without
  `await`), so the deprecation showcase route failed validation.
- Routes using response-filtering serialization kwargs
  (`response_model_exclude_unset` / `_exclude_defaults` / `_include` /
  `_exclude` / `by_alias=False`) no longer break at runtime: the schema
  previously promised every model field as non-null, but filtering runs
  after validation, so a dropped (or retargeted) key nullified the whole
  object with "Cannot return null for non-nullable field". These routes
  now bridge as a raw `JSON` scalar — no skip, no field selection, no
  promises filtering would break — and their field description tells
  agents why. `exclude_none` stays structured: it only drops
  Optional-valued keys, which map to nullable fields anyway.
- Form/File routes are now skipped at scan time with an explicit reason
  ("form/file parameter 'x' cannot be bridged"). Previously a Form-only
  endpoint whose annotations were valid GraphQL scalars (`Annotated[str,
  Form()]`) slipped into the schema and always failed at runtime with 422
  (the invoker sends JSON bodies); File-based routes were skipped only by
  coincidence (`bytes`/`UploadFile` having no GraphQL mapping), not by an
  explicit rule. Side effect: the OAuth token endpoint (form-encoded
  credentials) can no longer leak into the schema without an exclude —
  the leak is now structurally impossible, with the exclude glob remaining
  defense-in-depth.
- Request bodies (and body-bound variables) carrying custom-scalar fields —
  `Decimal`, `UUID`, `datetime`/`date`/`time`, at any nesting depth —
  crashed at the wire with "Object of type Decimal is not JSON
  serializable": the scalars' parse_value produces typed Python objects
  inside resolver kwargs, and json.dumps cannot encode them. The invoker
  now converts them to their JSON wire forms at the request boundary
  (query/path params were already stringified). Found by a 19-case
  parameter-shape matrix probe.

## 0.5.0 (2026-10-05)

### Added

- **`document_cache_size` (default 128, 0 disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: an LRU for the compile front half of
  execution — parse + depth guard + validate — keyed by the query string.
  Agents repeat documents constantly, and validation over an immutable
  schema is a pure function of the document, so a hit skips straight to
  execution (measured 1.52ms → 0.69ms on a 2-field query; new/unique
  queries are cost-neutral). Rejected documents cache their errors too.
  Execution results are never cached: per-call credentials run for real
  every time, and the cache lives on the handler instance so same-process
  handlers over different schemas never cross-contaminate.

### Changed

- The query string is parsed **once** per compile: the depth guard's parse
  is reused by execution (previously the guard parsed, discarded the AST,
  and graphql() re-parsed the same string). The handler now drives
  `validate` + `execute` directly instead of the `graphql()` wrapper.
- `validation_rules=` now **extends** the standard rule set instead of
  replacing it — passing a custom rule previously (and silently) dropped
  all 32 standard validation rules for that handler.

### Removed

- The GH Pages deploy workflow: `comparison/index.html` was deliberately
  folded into `comparison/README.md` in 0.4.0 (mermaid renders natively on
  GitHub), leaving the workflow deploying a directory with no entry page —
  the cause of its last failed run. The comparison report lives in the
  README; re-add a Pages workflow if a standalone visual page is ever
  wanted.

## 0.4.0 (2026-10-04)

### Added

- **Observability (OpenTelemetry), one waterfall per query**: the bridge
  injects W3C trace context (`traceparent`/`tracestate`/`baggage`) into
  every in-process route call — bridge-generated, so it flows regardless of
  `passthrough_headers`, and is a no-op without an SDK (new core dependency
  `opentelemetry-api`, non-recording by default). A `graphql.execute` span
  covers the GraphQL orchestration layer; route-call timeouts and
  concurrency queue waits surface as span events (`route.timeout`,
  `route.queue`). With fastmcp's tool spans and FastAPI >= 0.142's native
  route spans, one MCP query now lands as
  `tools/call > graphql.execute > GET /route` in a single trace (previously
  the route spans were orphan traces). Regression-locked by
  `tests/test_otel_propagation.py`; walkthrough in
  `examples/otel_smoke.md`.

- **`max_depth` (default 10, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: recursive models make selection-set
  nesting unbounded and an MCP caller is an LLM that can emit runaway
  documents — overly deep ones are now rejected before execution with a
  validation-style error. Fragment spreads resolve inline (spreading a
  deep query across fragments cannot hide it; cyclic spreads are
  rejected), inline fragments are transparent, and the root selection set
  counts as depth 1. `validation_rules=` on the handler passes extra
  graphql-core validation rules through to `graphql()` for anything
  policy-shaped. README gained a "Hardening the bridge" section covering
  these knobs plus the fastmcp rate-limiting / response-limiting
  middleware (per-client by default) for the MCP face.

- **`max_concurrency` (default 16, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`: a bound on in-flight route calls
  across all queries. Sibling GraphQL fields resolve concurrently, so one
  wide query fans out into parallel ASGI calls that could hammer the
  wrapped app's upstream (DB, external APIs); the invoker-global semaphore
  bounds that fan-out. The slot is acquired inside the timeout window, so
  queueing time counts against `request_timeout` — a call waiting for a
  slot cannot outlive its own deadline.

- **`request_timeout` (default 30s, `None` disables)** on
  `RouterGraphQLHandler` / `RouterMCP`, threaded to the invoker. Enforced
  with `asyncio.wait_for` and surfaced as a field-level `TIMEOUT` error
  (http_status 504) — see Fixed below for why the httpx timeout alone
  could never fire.

- **`handler.skips`**: the scanner's skip report (path/method/reason per
  excluded route) is now caller-visible instead of log-only — CI can
  assert `skips == []` (or an expected set) so a route silently falling
  out of the schema fails the build. Completes the scanner's day-one
  "the caller decides whether skips are acceptable" contract.

- GraphiQL shows an actionable offline notice (10s) when the esm.sh CDN
  is unreachable, pointing at the raw `POST /graphql` endpoint.

### Fixed

- **max_depth no longer rejects introspection**: `__`-prefixed meta-fields
  don't count toward the depth guard — GraphiQL/codegen/IDE plugins ship a
  fixed ~15-deep introspection document, which the default `max_depth=10`
  rejected ("Error fetching schema" on a stock GraphiQL page). The guard
  still bounds runaway DATA selections.

- `RouteInvoker(timeout=...)` was dead configuration: httpx's
  `ASGITransport` never enforces timeouts (in-process calls bypass
  httpcore) — a route could hang forever regardless of the setting.
  Enforcement now lives in `asyncio.wait_for` inside `invoke()`.

- `content-type` / `accept` are refused by `filter_passthrough_headers`
  even when whitelisted: a forwarded `content-type` would retype the JSON
  body request (text/plain + json body → FastAPI 422). Custom headers
  still forward as before.

### Changed

- Progressive disclosure caches each domain's SDL fragment (the schema is
  immutable after build; agents re-explore the same domain often), and
  `DomainRegistry` answers `children()` from a precomputed index instead
  of a full node scan per `list_domains` call.

- `mount_to(auth_at_root=True)` now carries an explicit checklist comment
  of the fastmcp private surface it relies upon (per-route auth guard,
  app-level auth/request-context middleware, well-known routes) — the
  things to re-verify on any fastmcp major bump.

- **JSON pass-through for dynamic shapes**: endpoints and fields annotated
  `dict`, `dict[K, V]`, `Any` (and `list`s / model fields thereof) bridge
  as the `JSON` scalar instead of being skipped — an author declaring a
  dynamic shape gets reachability, not invisibility. The line drawn: an
  explicit `dict`/`Any` annotation passes through; a route with no
  annotation and no `response_model` still skips (no contract). Both
  directions: a `JSON` input argument lands as the raw request body.

- **`RouterMCP(auth=...)`**: optional fastmcp auth provider (e.g.
  `GitHubProvider`) passed through to `FastMCP` untouched — the MCP
  endpoint then speaks OAuth 2.1 (401 discovery, DCR, PKCE) and clients'
  Bearer tokens travel into route calls via `passthrough_headers` like any
  other caller's. The bridge verifies nothing. When `auth` is set,
  `mount_to` also re-exposes the provider's `/.well-known/*` discovery
  routes at the host app's root — the 401 challenge advertises them there
  (RFC 8414), but the mount alone would shift them under the mount path.
  `mount_to(..., auth_at_root=True)` goes further: MCP endpoint at `path`,
  OAuth/discovery routes at the host root — for reusing an IdP app whose
  registered callback URL lives at the root domain (subdirectory
  redirect_path). Implemented by splicing the wrapped app's routes (host
  routes keep precedence) and copying its middleware stack wholesale — a
  plain `Mount("")` catch-all would silently shadow host routes added
  later, and dropping the app-level middleware (auth verification,
  request-context capture) silently 401s every request.

- **Consumer example `examples/notes_oauth`**: a full Notes app behind
  GitHub OAuth — browser session cookie, `POST /auth/token` Bearer, and
  MCP OAuth 2.1 login (Claude Code) as three interchangeable credential
  carriers, with the fastmcp GitHub proxy reusing the app's OAuth callback
  via a redirect subdirectory (`auth_at_root`). Guarded by
  `tests/test_example_public_api.py`: examples import the public API only.

### Fixed

- Python 3.10 support for recursive Pydantic models: CPython < 3.11 leaves
  ``list["Node"]`` with the plain string inside the PEP 585 generic (only
  ``typing.Union`` converts str args to ``ForwardRef``), and every evaluator
  only evaluates ``ForwardRef`` — so ``FieldInfo.annotation`` stayed
  unresolved and recursive models were skipped as unsupported types on 3.10
  while working on 3.11+. The type builder now rewraps string args as
  ``ForwardRef`` (leaving ``Literal`` values untouched — those strings are
  values, not types) and evaluates them against the model's namespaces,
  seeding the namespace with the model's own name (the enclosing scope binds
  a class name only after the class body runs, so neither module globals
  nor pydantic's parent-namespace snapshot can resolve a self-reference).

- The demo app imported ``datetime.UTC`` (Python 3.11+), failing import on
  3.10; it now uses ``timezone.utc``.

- A leaf field colliding with a child domain segment inside the same group
  (e.g. a `catalog` endpoint tagged `shop` plus routes tagged
  `shop:catalog`) was silently dropped: the child group field overwrote
  it in the object type, with no warning — and the progressive-disclosure
  index kept advertising the ghost field. Same-named leaves already failed
  fast with `DuplicateFieldError`; leaf-vs-subdomain collisions now fail
  fast the same way, naming both claimants.

- `mode="auto"` counted routes via `isinstance(app.routes, APIRoute)`,
  which counts ZERO on FastAPI >= 0.142 (include_router results are
  wrapped in `_IncludedRouter`) — a 30-route app stayed in simple mode,
  handing agents one giant SDL. It also ignored `include`/`exclude`: an
  app narrowed to 3 schema routes still switched to progressive. The
  threshold decision now uses the scanned route count (what actually
  enters the schema).

- Single non-model body parameters (e.g. `payload: dict[str, Any]`) were
  wrongly wrapped as `{"payload": ...}` — FastAPI gives a lone body param
  the WHOLE body unless `Body(embed=True)` or multiple body params are
  involved. `_body_embeds` now replicates FastAPI's predicate exactly.

### Changed

- **`headers_provider` removed; `passthrough_headers` defaults to
  `("authorization",)`** (breaking). Credentials now have a single source —
  the caller: each MCP/GraphQL client connects with its own credentials and
  the bridge forwards them (case-insensitive whitelist; an explicitly empty
  list disables forwarding). The server-side provider (and the
  credential-amplification risk it created when mounted publicly) is gone;
  machines without a user context configure the service credential on the
  MCP client side, or use `handler.execute(..., headers=...)` directly.
  With no HTTP request context (in-memory client) protected routes simply
  answer 401.

- **`RouterMCP.run()` is HTTP-only** (streamable HTTP with `host`/`port`
  parameters, default `127.0.0.1:8000`). The wrapped FastAPI app is a
  service whose routes speak HTTP, and per-caller credential passthrough
  needs an HTTP request context that stdio has no notion of — so the stdio
  transport and the `demo.mcp_stdio` entry point are removed (breaking).
  Use `mount_to(app, "/mcp")` to serve MCP on the app's own port.

## 0.3.0 (2026-10-03)

Schema shape + documentation wave (breaking).

### Changed

- **Renamed from `routerql` to `fastapi-gql-mcp`** (package
  `fastapi_gql_mcp`): the distribution name now carries both FastAPI and MCP.
  Exception prefixes `RouterQL*` became `GQLMCP*`; `RouterMCP` /
  `RouterGraphQLHandler` keep their Router-based names.

### Changed

- **Domain-grouped schema (UseCaseService-style hierarchy)**: fields live
  under their tag domain tree — a route tagged `shop:catalog` answers at
  `{ shop { catalog { list_products } } }`. Untagged routes join the domain of
  their first path segment. Domain SDL fragments now show the exact grouped
  address an agent should query.
- **GraphQL field names now come from the endpoint function name** (e.g.
  `async def get_user` → `get_user`) instead of being reconstructed from the
  URL path + verb. Path/query/body parameters still become the field's
  arguments. Two routes sharing a function name fail fast with
  `DuplicateFieldError` (previously the `_by_{param}` suffix silently
  disambiguated collection/item pairs).

### Added

- Upgraded the optional `mcp` extra to **fastmcp 4.x** (from 3.1): zero code
  changes needed — `FastMCP` construction, tool registration, `http_app`
  mounting and `run()` all carried over. Verified end-to-end (tools, queries,
  mutations over streamable HTTP) on 4.0.10.
- Mutation execution semantics pinned by tests: cross-domain writes are
  serial (spec-guaranteed at the mutation root); same-domain writes run in
  parallel. Mutation-only schemas now fail fast with guidance (GraphQL
  requires a Query root).
- **Argument descriptions**: `Query()/Path()/Body(description=...)` metadata
  maps onto GraphQL argument descriptions, completing the doc chain
  (model docstrings → types, `Field(description)` → fields, endpoint
  docstrings/`summary=` → fields).
- Runnable demo suite: `python -m demo` (all-in-one server, progressive mode),
  `demo.mcp_stdio`, `demo.mcp_walkthrough`; full documentation coverage in the
  demo app for inspection.

## 0.2.0 (2026-10-03)

Feature wave over the 0.1 core.

### Added

- **Progressive disclosure MCP tools** (4 layers over the tag tree):
  `list_domains` → `list_queries(domain)` / `list_mutations(domain)` →
  `get_query_schema(domain)` → `graphql_query`. Domain SDL fragments are
  filtered sub-schemas (only reachable types). Discovery is scoped per domain;
  execution always runs against the full schema. `mode="auto"` switches to
  progressive above `progressive_threshold` (default 25, configurable).
- **Query Parameter Models**: a lone `Annotated[Model, Query()]` flattens into
  individual alias-aware query arguments. Models mixed with plain query params
  are skipped with a reason.
- **GraphiQL playground + GraphQL-over-HTTP**: `handler.mount_graphql(app)`
  serves `/graphiql` (CDN build, explorer plugin) wired to `POST /graphql`
  accepting `{query, variables, operationName}`.
- **`mutation_include`** glob whitelist: with `allow_mutation=True`, write
  routes must match to become mutations.
- Restructured `demo/` nexusx-style.

### Fixed

- Type descriptions no longer inherit BaseModel's docstring when a model has
  none of its own.

## 0.1.0 (2026-10-03)

Initial release.

- RouterScanner: routes → field metadata (verb mapping, dependency-tree param
  flattening, SkipRecord taxonomy, tag domains).
- TypeBuilder: Pydantic → graphql-core (custom DateTime/Date/Time/UUID/Decimal
  scalars, Literal/Enum, cycle-safe registries, alias-first naming).
- RouteInvoker: in-process ASGI execution with lazy lifespan management and
  `headers_provider` credential passthrough; HTTP ≥ 400 → `GraphQLError`
  with `extensions.code=HTTP_{status}`.
- SchemaBuilder + RouterGraphQLHandler on graphql-core standard execution;
  route responses nullable per field so sibling results survive failures.
- RouterMCP: stdio/mount MCP server, `get_schema` + `graphql_query` /
  `graphql_mutation` with operation-type guards and hint-chain envelopes.
