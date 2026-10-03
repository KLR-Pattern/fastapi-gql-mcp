# Changelog

## Unreleased

### Added


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
