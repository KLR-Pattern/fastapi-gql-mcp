"""Introspect a FastAPI app's routes into GraphQL field metadata.

The scanner is the single source of truth for "what the GraphQL schema will
contain": every rule that excludes a route (no typed response, hidden route,
mutation disabled, …) lives here and produces a ``SkipRecord``; the caller
decides whether those skips are acceptable.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import FastAPI
from fastapi.params import File, Form
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.utils import DefaultPlaceholder  # type: ignore[attr-defined]
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from fastapi_gql_mcp.domains import domains_for
from fastapi_gql_mcp.naming import field_name_for
from fastapi_gql_mcp.type_builder import TypeBuilder, UnsupportedFieldTypeError

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
        for candidate in (info.validation_alias, info.alias):
            if isinstance(candidate, str) and candidate:
                request_name = candidate
                break
        required = bool(info.is_required())
        params.append(
            ParamInfo(
                name=request_name,
                annotation=info.annotation,
                required=required,
                default=None if required else info.get_default(call_default_factory=False),
                raw_name=field_name,
                description=info.description,
            )
        )
    return params


def _to_param_info(model_field: Any, *, path_param: bool = False) -> ParamInfo:
    field_info: FieldInfo = model_field.field_info
    annotation = field_info.annotation
    # Request-side name: FastAPI validates against validation_alias/alias/name.
    name = model_field.validation_alias or model_field.alias or model_field.name
    required = path_param or _param_required(field_info)
    return ParamInfo(
        name=name,
        annotation=annotation,
        required=required,
        default=None if required else field_info.default,
        embed=bool(getattr(field_info, "embed", False)),
        raw_name=model_field.name,
        description=getattr(field_info, "description", None),
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
        allow_mutation: bool = False,
        include_hidden: bool = False,
        mutation_include: Sequence[str] | None = None,
    ) -> None:
        self._app = app
        self._include = include
        self._exclude = exclude
        self._allow_mutation = allow_mutation
        self._include_hidden = include_hidden
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

            if method in _MUTATION_VERBS and not self._allow_mutation:
                skips.append(
                    SkipRecord(r.path, method, "mutation endpoints are disabled "
                              "(pass allow_mutation=True to expose them)")
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
                        "mutation endpoint not matched by mutation_include globs",
                    )
                )
                continue

            if not r.include_in_schema and not self._include_hidden:
                skips.append(
                    SkipRecord(r.path, method, "hidden route (include_in_schema=False)")
                )
                continue

            route_info = self._build_route_info(r, method, types, skips)
            if route_info is not None:
                routes.append(route_info)

        if skips:
            rendered = "; ".join(f"{s.method} {s.path}: {s.reason}" for s in skips)
            logger.warning("fastapi-gql-mcp skipped %d route(s): %s", len(skips), rendered)
        return routes, skips

    # ----------------------------------------------------------------- helpers

    def _build_route_info(
        self,
        route: Any,
        method: str,
        types: TypeBuilder,
        skips: list[SkipRecord],
    ) -> RouteInfo | None:
        def skip(reason: str) -> None:
            skips.append(SkipRecord(route.path, method, reason))

        flat = _flatten_params(route.dependant)
        path_p, query_p, header_p, cookie_p, body_p = flat

        for p in header_p + cookie_p:
            if _param_required(p.field_info):
                skip(
                    f"required header/cookie parameter "
                    f"'{p.validation_alias or p.alias or p.name}' cannot be a GraphQL argument"
                )
                return None

        for p in path_p:
            annotation = p.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                skip(f"path parameter model '{annotation.__name__}' is not supported")
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
                        f"plain query parameters is not supported"
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
                    f"(the invoker sends JSON bodies only)"
                )
                return None
            annotation = p.field_info.annotation
            if annotation is None:
                skip("body parameter without a typed annotation")
                return None

        response_annotation = self._response_annotation(route)
        if response_annotation is None:
            skip("no typed response (response_model or return annotation required)")
            return None
        if isinstance(response_annotation, type) and issubclass(response_annotation, Response):
            skip("returns a raw Response object, no typed body to expose")
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
        try:
            if response_filter is None:
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
        # OpenAPI typing allows Enum tags; only string tags form domains.
        str_tags = tuple(t for t in route.tags or () if isinstance(t, str))
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
            return get_typed_return_annotation(route.endpoint)
        return response_model
