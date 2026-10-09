"""Introspect a FastAPI app's routes into GraphQL field metadata.

The scanner is the single source of truth for "what the GraphQL schema will
contain": every rule that excludes a route (no typed response, hidden route,
mutation disabled, …) lives here and produces a ``SkipRecord``; the caller
decides whether those skips are acceptable.
"""

from __future__ import annotations

import fnmatch
import inspect
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.params import File, Form
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.utils import DefaultPlaceholder  # type: ignore[attr-defined]
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from fastapi_gql_mcp.domains import domains_for
from fastapi_gql_mcp.naming import field_name_for
from fastapi_gql_mcp.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    _first_wire_name,
    sanitize_graphql_name,
    union_members,
)

logger = logging.getLogger(__name__)

# Priority when a route registers multiple verbs (e.g. @app.api_methods).
_VERB_PRIORITY = ("GET", "POST", "PUT", "PATCH", "DELETE")
_MUTATION_VERBS = {"POST", "PUT", "PATCH", "DELETE"}

# Route-level serialization kwargs that reshape the response AFTER
# validation — the JSON the resolver sees may lack keys (or use different
# keys) than the response model declares, so per-field GraphQL promises
# cannot hold. exclude_none is NOT here: it only drops Optional-valued
# keys, which the bridge maps to nullable fields anyway.
_RESPONSE_FILTERS = (
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_include",
    "response_model_exclude",
)


def response_filter_kwarg(route: Any) -> str | None:
    """The first route kwarg that makes response fields unpredictable.

    Returns the kwarg name (for documentation), or None when the response
    shape is trustworthy. ``response_model_by_alias=False`` retargets keys
    rather than filtering them, but the consequence is identical: the
    JSON no longer matches the alias-built GraphQL field names.
    """
    for key in _RESPONSE_FILTERS:
        if getattr(route, key, None):
            return key
    if getattr(route, "response_model_by_alias", True) is False:
        return "response_model_by_alias=False"
    return None


@dataclass(frozen=True)
class ParamInfo:
    """One GraphQL-argument-worthy parameter of a route."""

    name: str
    annotation: Any
    required: bool
    default: Any = None
    embed: bool = False
    raw_name: str = ""  # function-arg name, used for path-template replacement
    description: str | None = None  # from Query()/Body() metadata
    # GraphQL-side argument name when the wire name is not a legal GraphQL
    # identifier (empty = same as ``name``); the resolver translates back.
    gname: str = ""


@dataclass(frozen=True)
class RouteInfo:
    """Everything the schema builder needs about one routable endpoint."""

    route: Any = field(repr=False)  # APIRoute or FastAPI >=0.142 include leaf
    method: str
    path: str
    field_name: str
    path_params: tuple[ParamInfo, ...] = ()
    query_params: tuple[ParamInfo, ...] = ()
    body_params: tuple[ParamInfo, ...] = ()
    response_annotation: Any = None
    tags: tuple[str, ...] = ()
    description: str | None = None
    deprecated: bool = False
    response_filter: str | None = None  # kwarg that unshapes the response JSON
    domains: frozenset[tuple[str, ...]] = frozenset()

    @property
    def is_mutation(self) -> bool:
        return self.method in _MUTATION_VERBS


@dataclass(frozen=True)
class SkipRecord:
    """A route that was excluded from the schema, and why."""

    path: str
    method: str
    reason: str
    tags: tuple[str, ...] = ()  # the route's string tags (which domain it belongs to)


@dataclass(frozen=True)
class DegradedRecord:
    """A route that DID enter the schema but degraded to raw JSON (no field
    selection), and why. Still callable — just not structurally exposed."""

    path: str
    method: str
    field_name: str  # the GraphQL field carrying the raw JSON
    reason: str
    tags: tuple[str, ...] = ()  # the route's string tags (which domain it belongs to)


@dataclass(frozen=True)
class ReadinessReport:
    """The exposure audit the startup notices are built from: which routes
    the bridge skips, which it degrades to raw JSON, and which model fields
    degrade. Pure data — get one from ``RouterScanner(app).readiness()``
    (standalone, no handler) or ``handler.readiness()`` (over stored
    results, no re-scan); the assembly itself is an internal detail."""

    skips: tuple[SkipRecord, ...]  # never entered the schema
    degraded: tuple[DegradedRecord, ...]  # in the schema, no field selection
    degraded_fields: tuple[tuple[str, str], ...]  # (Model.field, reason)

    @property
    def ready(self) -> bool:
        """True when nothing is skipped or degraded — assert this
        in CI to pin the exposure you expect."""
        return not (self.skips or self.degraded or self.degraded_fields)


def _readiness_report(
    routes: Sequence[RouteInfo], skips: Sequence[SkipRecord], types: TypeBuilder
) -> ReadinessReport:
    """Assemble the report from one scan's three products — the single
    source shared by ``RouterScanner.readiness()`` and
    ``RouterGraphQLHandler.readiness()``. Internal on purpose: callers
    asking for a report should never need scan's intermediates."""
    return ReadinessReport(
        skips=tuple(skips),
        degraded=tuple(
            DegradedRecord(r.path, r.method, r.field_name, reason, tags=r.tags)
            for r in routes
            if (reason := _degraded_reason(r)) is not None
        ),
        degraded_fields=tuple(types.degraded_fields),
    )


def _degraded_reason(route: RouteInfo) -> str | None:
    """Why one scanned route degrades to raw JSON (loses field selection),
    or None when its response stays structured. Shared by the startup
    notice and ReadinessReport so the two can never drift apart."""
    if route.response_annotation is Any:
        return (
            "no typed response — bridged as raw JSON; add a return "
            "annotation or response_model for a structured type"
        )
    if route.response_filter:
        return f"response filtered via {route.response_filter}"
    if (members := union_members(route.response_annotation)) is not None:
        names = "|".join(getattr(m, "__name__", str(m)) for m in members)
        return (
            f"union response ({names}) — restructure into one model "
            "per shape to regain field selection"
        )
    return None


def _param_required(field_info: FieldInfo) -> bool:
    try:
        return bool(field_info.is_required())
    except Exception:  # pragma: no cover - defensive for fastapi internals
        return field_info.default is None


def _expand_query_model(model: type[BaseModel]) -> list[ParamInfo]:
    """Flatten a Query Parameter Model into individual query params.

    Mirrors FastAPI's wire format for a lone ``Annotated[Model, Query()]``
    parameter: each model field becomes one query key (alias-aware), validated
    by the model's own field metadata.
    """
    params: list[ParamInfo] = []
    for field_name, info in model.model_fields.items():
        request_name: str = field_name
        raw = info.validation_alias or info.alias
        if isinstance(raw, str) and raw:
            request_name = raw
        elif raw is not None:
            request_name = _first_wire_name(raw, field_name)
        gname = sanitize_graphql_name(request_name, what="argument name")
        required = bool(info.is_required())
        params.append(
            ParamInfo(
                name=request_name,
                annotation=info.annotation,
                required=required,
                default=None if required else info.get_default(call_default_factory=False),
                raw_name=field_name,
                description=info.description,
                gname="" if gname == request_name else gname,
            )
        )
    return params


def _to_param_info(model_field: Any, *, path_param: bool = False) -> ParamInfo:
    field_info: FieldInfo = model_field.field_info
    annotation = field_info.annotation
    # Request-side name: FastAPI validates against validation_alias/alias/name
    # (AliasChoices yields its first string choice — FastAPI accepts any).
    raw = model_field.validation_alias or model_field.alias
    name = raw if isinstance(raw, str) and raw else model_field.name
    if not (isinstance(raw, str) and raw):
        name = _first_wire_name(raw, model_field.name)
    # The GraphQL argument name must be a legal identifier; when sanitizing
    # changes it, the schema uses the sanitized name and the resolver
    # translates it back to the wire name.
    gname = sanitize_graphql_name(name, what="argument name")
    required = path_param or _param_required(field_info)
    return ParamInfo(
        name=name,
        annotation=annotation,
        required=required,
        default=None if required else field_info.default,
        embed=bool(getattr(field_info, "embed", False)),
        raw_name=model_field.name,
        description=getattr(field_info, "description", None),
        gname="" if gname == name else gname,
    )


def _flatten_params(
    dependant: Dependant,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    """Walk the dependency tree collecting params by location (name-deduped)."""
    path_params: list[Any] = []
    query_params: list[Any] = []
    header_params: list[Any] = []
    cookie_params: list[Any] = []
    body_params: list[Any] = []
    seen: set[int] = set()
    stack: list[Dependant] = [dependant]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        path_params.extend(current.path_params)
        query_params.extend(current.query_params)
        header_params.extend(current.header_params)
        cookie_params.extend(current.cookie_params)
        body_params.extend(current.body_params)
        stack.extend(reversed(current.dependencies))

    def dedup(params: list[Any]) -> list[Any]:
        by_name: dict[str, Any] = {}
        for p in params:
            name = p.validation_alias or p.alias or p.name
            by_name.setdefault(name, p)
        return list(by_name.values())

    return (
        dedup(path_params),
        dedup(query_params),
        dedup(header_params),
        dedup(cookie_params),
        dedup(body_params),
    )


def _body_embeds(body_params: Sequence[ParamInfo]) -> bool:
    """Replicates fastapi.dependencies.utils._should_embed_body_fields
    (JSON body params only — Form/File are not bridged): a single body
    parameter without an explicit ``Body(embed=True)`` takes the WHOLE
    body as its value, whatever its type (BaseModel, dict, scalar) —
    embedding applies to multiple body params or explicit embed only."""
    if len({p.name for p in body_params}) > 1:
        return True
    return bool(body_params[0].embed)


def _matches(path: str, patterns: Sequence[str] | None) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns or ())


def _tags_match(tags: Sequence[str], patterns: Sequence[str] | None) -> bool:
    """A route matches when ANY of its string tags fnmatches ANY pattern
    ("iam:*" matches "iam:users"; an exact pattern matches too). Empty tags
    match nothing, so include_tags is a strict whitelist (untagged drops)."""
    return any(_matches(tag, patterns) for tag in tags)


def _iter_api_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Flatten app.routes into scannable route objects (duck-typed).

    FastAPI >= 0.142 wraps ``include_router`` results in a private
    ``_IncludedRouter`` that resolves lazily; its ``effective_candidates()``
    leaves (``_EffectiveRouteContext``) carry the PREFIX-RESOLVED path plus
    the usual metadata (methods/response_model/dependant/tags). Older
    FastAPI copies plain ``APIRoute`` objects flat into ``app.routes``.
    Both shapes are yielded uniformly; everything else (Mount, WebSocket,
    docs routes) is skipped.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "effective_candidates"):
            # include_router wrapper (nested includes recurse the same way)
            yield from _iter_api_routes(route.effective_candidates())
        elif hasattr(route, "dependant") and hasattr(route, "methods"):
            # _EffectiveRouteContext leaf with the resolved path
            yield route


class RouterScanner:
    """Scans ``app.routes`` into ``RouteInfo`` / ``SkipRecord`` lists."""

    def __init__(
        self,
        app: FastAPI,
        *,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        include_tags: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        allow_mutation: bool = False,
        include_hidden: bool = False,
        exclude_deprecated: bool = False,
        mutation_include: Sequence[str] | None = None,
    ) -> None:
        self._app = app
        self._include = include
        self._exclude = exclude
        self._include_tags = include_tags
        self._exclude_tags = exclude_tags
        self._allow_mutation = allow_mutation
        self._include_hidden = include_hidden
        self._exclude_deprecated = exclude_deprecated
        self._mutation_include = mutation_include

    def scan(
        self, types: TypeBuilder | None = None
    ) -> tuple[list[RouteInfo], list[SkipRecord]]:
        """Scan the app; ``types`` (shared TypeBuilder) trials response models."""
        types = types or TypeBuilder()
        routes: list[RouteInfo] = []
        skips: list[SkipRecord] = []

        for r in _iter_api_routes(self._app.routes):

            route_methods = r.methods or set()
            verbs = sorted(route_methods & set(_VERB_PRIORITY), key=_VERB_PRIORITY.index)
            if not verbs:
                continue  # HEAD / OPTIONS only
            if len(verbs) > 1:
                logger.warning(
                    "Route %s registers multiple verbs %s; using %s (priority order)",
                    r.path,
                    sorted(route_methods),
                    verbs[0],
                )
            method = verbs[0]

            if self._exclude and _matches(r.path, self._exclude):
                continue
            if self._include is not None and not _matches(r.path, self._include):
                continue

            # Tag filters mirror path filters: config-level, silent drop.
            # OpenAPI allows Enum tags; only string tags participate (the
            # same rule as domain grouping in _build_route_info).
            route_tags = tuple(t for t in (r.tags or ()) if isinstance(t, str))
            if self._exclude_tags and _tags_match(route_tags, self._exclude_tags):
                continue
            if self._include_tags is not None and not _tags_match(
                route_tags, self._include_tags
            ):
                continue

            # Deprecated filtering, same config-level silent drop. Routes that
            # stay carry a GraphQL-native deprecation mark in the schema.
            if self._exclude_deprecated and getattr(r, "deprecated", False):
                continue

            if method in _MUTATION_VERBS and not self._allow_mutation:
                skips.append(
                    SkipRecord(r.path, method, "mutation endpoints are disabled "
                              "(pass allow_mutation=True to expose them)",
                              tags=route_tags)
                )
                continue

            if (
                method in _MUTATION_VERBS
                and self._mutation_include is not None
                and not _matches(r.path, self._mutation_include)
            ):
                skips.append(
                    SkipRecord(
                        r.path, method,
                        "mutation endpoint not matched by mutation_include globs "
                        "(widen the pattern, or pass mutation_include=None to allow all)",
                        tags=route_tags,
                    )
                )
                continue

            if not r.include_in_schema and not self._include_hidden:
                skips.append(
                    SkipRecord(
                r.path, method,
                "hidden route (include_in_schema=False; pass include_hidden=True to expose it)",
                tags=route_tags,
            )
                )
                continue

            route_info = self._build_route_info(r, method, types, skips)
            if route_info is not None:
                routes.append(route_info)

        if skips:
            rendered = "; ".join(f"{s.method} {s.path}: {s.reason}" for s in skips)
            logger.warning("fastapi-gql-mcp skipped %d route(s): %s", len(skips), rendered)

        # Degraded-but-present routes deserve a startup notice too: the field
        # is callable but has NO field selection. Naming the cause (and the
        # way out) turns a silent downgrade into an actionable one.
        degraded = [
            (r, reason) for r in routes if (reason := _degraded_reason(r)) is not None
        ]
        if degraded:
            rendered = "; ".join(f"{r.method} {r.path}: {reason}" for r, reason in degraded)
            logger.warning(
                "fastapi-gql-mcp bridged %d route(s) as raw JSON (no field "
                "selection): %s",
                len(degraded),
                rendered,
            )
        if types.degraded_fields:
            rendered = "; ".join(
                f"{path} ({reason})" for path, reason in types.degraded_fields
            )
            logger.warning(
                "fastapi-gql-mcp bridged %d model field(s) as raw JSON — "
                "restructure the union away or give the field a JSON-"
                "compatible annotation to regain field selection: %s",
                len(types.degraded_fields),
                rendered,
            )
        return routes, skips

    def readiness(self) -> ReadinessReport:
        """Scan and classify without building anything else — the same audit
        the startup notices come from, runnable on its own (CI gate, pre-
        deploy checklist). Pass the same filters your deployment uses so
        the report reflects what IT would expose."""
        types = TypeBuilder()
        routes, skips = self.scan(types)
        return _readiness_report(routes, skips, types)

    # ----------------------------------------------------------------- helpers

    def _build_route_info(
        self,
        route: Any,
        method: str,
        types: TypeBuilder,
        skips: list[SkipRecord],
    ) -> RouteInfo | None:
        # OpenAPI allows Enum tags; only string tags participate (domain
        # grouping below applies the same rule).
        str_tags = tuple(t for t in route.tags or () if isinstance(t, str))

        def skip(reason: str) -> None:
            skips.append(SkipRecord(route.path, method, reason, tags=str_tags))

        flat = _flatten_params(route.dependant)
        path_p, query_p, header_p, cookie_p, body_p = flat

        for p in header_p + cookie_p:
            if _param_required(p.field_info):
                skip(
                    f"required header/cookie parameter "
                    f"'{p.validation_alias or p.alias or p.name}' cannot be a GraphQL "
                    f"argument — make it optional (caller credentials ride "
                    f"passthrough_headers instead of GraphQL arguments)"
                )
                return None

        for p in path_p:
            annotation = p.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                skip(
                f"path parameter model '{annotation.__name__}' is not supported "
                f"— use scalar path parameters (str/int/uuid) and accept the "
                f"model inside the handler"
            )
                return None
        for p in query_p:
            annotation = p.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                # FastAPI flattens a LONE BaseModel query param ("Query
                # Parameter Models") into individual query keys on the wire;
                # a model mixed with plain query params has no such shape.
                if len(query_p) != 1:
                    skip(
                        f"query parameter model '{annotation.__name__}' mixed with "
                        f"plain query parameters is not supported — move the "
                        f"plain parameters into the model (FastAPI itself "
                        f"rejects the mixed form on the wire)"
                    )
                    return None
        for p in body_p:
            # Form/File bodies are deliberately not bridged: the invoker
            # speaks JSON, and MCP tool arguments have no file channel
            # (SEP-2631 is the protocol-level fix, still draft). Skip must
            # happen HERE — a Form-only scalar annotation (str/int/...) is a
            # perfectly valid GraphQL input, so the type check below would
            # let the route through into a field that always 422s.
            if isinstance(p.field_info, Form | File):
                name = p.validation_alias or p.alias or p.name
                skip(
                    f"form/file parameter '{name}' cannot be bridged "
                    f"(the invoker sends JSON bodies only) — the route stays "
                    f"available over plain HTTP"
                )
                return None
            annotation = p.field_info.annotation
            if annotation is None:
                skip(
                "body parameter without a typed annotation — annotate it "
                "(e.g. payload: MyModel)"
            )
                return None

        response_annotation = self._response_annotation(route)
        if response_annotation is None:
            skip(
                "no typed response (response_model or return annotation required) "
                "— return a typed model or set response_model"
            )
            return None
        if isinstance(response_annotation, type) and issubclass(response_annotation, Response):
            skip(
                "returns a raw Response object, no typed body to expose "
                "— return a typed model (or add response_model) instead"
            )
            return None

        query_params = (
            _expand_query_model(query_p[0].field_info.annotation)
            if len(query_p) == 1
            and isinstance(query_p[0].field_info.annotation, type)
            and issubclass(query_p[0].field_info.annotation, BaseModel)
            else [_to_param_info(p) for p in query_p]
        )

        # Filtered responses bypass the structured type entirely (the JSON
        # scalar carries whatever arrives); only their INPUT types are trialed.
        response_filter = response_filter_kwarg(route)
        is_void = response_annotation is type(None)
        try:
            if response_filter is None and not is_void:
                types.output_type(
                    response_annotation, context=f"response of {route.path}"
                )
            for param in (
                *(_to_param_info(p, path_param=True) for p in path_p),
                *query_params,
                *(_to_param_info(p) for p in body_p),
            ):
                types.input_type(
                    param.annotation, context=f"parameter '{param.name}' of {route.path}"
                )
        except UnsupportedFieldTypeError as exc:
            skip(f"unsupported type: {exc}")
            return None

        body_params = [_to_param_info(p) for p in body_p]
        if body_params and _body_embeds(body_params):
            body_params = [replace(p, embed=True) for p in body_params]

        description = route.summary or route.description or None
        # OpenAPI's `deprecated: true` becomes GraphQL-native deprecation.
        deprecated = bool(getattr(route, "deprecated", False))
        return RouteInfo(
            route=route,
            method=method,
            path=route.path,
            field_name=field_name_for(route),
            path_params=tuple(_to_param_info(p, path_param=True) for p in path_p),
            query_params=tuple(query_params),
            body_params=tuple(body_params),
            response_annotation=response_annotation,
            tags=str_tags,
            description=description,
            deprecated=deprecated,
            response_filter=response_filter,
            domains=domains_for(str_tags, route.path),
        )

    @staticmethod
    def _response_annotation(route: Any) -> Any:
        response_model = route.response_model
        # Both an explicit response_model=None (Response-returning routes) and
        # the DefaultPlaceholder (unset) fall back to the typed return annotation.
        if response_model is None or isinstance(response_model, DefaultPlaceholder):
            raw = inspect.signature(route.endpoint).return_annotation
            # `-> None` (and its string form under future-annotations) is an
            # EXPLICIT "no response body" contract — bridged as a Boolean
            # success field. No annotation at all still bridges, as the JSON
            # scalar via the Any marker: available rather than skipped, and
            # flagged in the degraded-routes startup notice so an annotation
            # lost to refactoring stays visible.
            if raw is None or raw == "None":
                return type(None)
            typed = get_typed_return_annotation(route.endpoint)
            return Any if typed is None else typed
        return response_model
