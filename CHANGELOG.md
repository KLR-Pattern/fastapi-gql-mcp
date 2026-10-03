# Changelog

## 0.2.0 (2026-10-03)

P2/P3 feature wave over the 0.1 core.

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
- Restructured `demo/` nexusx-style: `python -m demo` (all-in-one server),
  `demo.mcp_stdio`, `demo.mcp_walkthrough` (in-memory agent walkthrough);
  richer demo app (query parameter models, tag domains, skip examples).

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
