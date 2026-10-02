"""Execute GraphQL field resolvers by calling FastAPI routes in-process.

Requests travel through the real ASGI app (httpx ``ASGITransport``), so
``Depends``/middleware/authentication behave exactly as they would over the
network. The app's lifespan is managed with ``asgi-lifespan`` — started lazily
on first use and held open until ``aclose()``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from asgi_lifespan import LifespanManager
from graphql import GraphQLError
from httpx import ASGITransport, AsyncClient, Response

from routerql.scanner import RouteInfo

logger = logging.getLogger(__name__)

HeadersProvider = Callable[[], "dict[str, str] | Awaitable[dict[str, str]]"]

_BASE_HEADERS: dict[str, str] = {"accept": "application/json"}
_MAX_ERROR_BODY = 500


class RouterQLRuntimeError(RuntimeError):
    """Raised when the invoker is used outside its lifecycle."""


@dataclass(frozen=True)
class RequestPlan:
    """A materialized HTTP request, pure data for easy testing."""

    method: str
    path: str
    params: tuple[tuple[str, str], ...]
    json_body: Any
    headers: dict[str, str]


def build_request(
    route: RouteInfo,
    kwargs: Mapping[str, Any],
    extra_headers: Mapping[str, str] | None = None,
) -> RequestPlan:
    """Translate GraphQL resolver kwargs into an HTTP request plan."""
    path = route.path
    params: list[tuple[str, str]] = []

    for param in route.path_params:
        value = kwargs.get(param.name, param.default)
        rendered = _render_param(value)
        if rendered is None:
            raise GraphQLError(
                f"Missing required path parameter '{param.name}' "
                f"for {route.method} {route.path}",
                extensions={"code": "BAD_REQUEST"},
            )
        # The path template holds the raw function-arg name, not any alias.
        path = path.replace("{" + param.raw_name + "}", quote(str(rendered), safe=""))

    for param in route.query_params:
        value = kwargs.get(param.name, param.default)
        rendered = _render_param(value)
        if rendered is None:
            continue  # optional params are simply not sent
        if isinstance(rendered, list):
            params.extend((param.name, str(item)) for item in rendered)
        else:
            params.append((param.name, str(rendered)))

    json_body: Any = None
    if route.body_params:
        if any(p.embed for p in route.body_params):
            json_body = {p.name: kwargs.get(p.name) for p in route.body_params}
        else:
            json_body = kwargs.get(route.body_params[0].name)

    headers = dict(_BASE_HEADERS)
    if json_body is not None:
        headers["content-type"] = "application/json"
    if extra_headers:
        headers.update(dict(extra_headers))

    return RequestPlan(
        method=route.method,
        path=path,
        params=tuple(params),
        json_body=json_body,
        headers=headers,
    )


def _render_param(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        rendered = [_render_param(v) for v in value]
        return rendered if any(v is not None for v in rendered) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class RouteInvoker:
    """Calls routes through the ASGI app, managing lifespan and auth headers."""

    def __init__(
        self,
        app: Any,
        *,
        headers_provider: HeadersProvider | None = None,
        timeout: float = 30.0,
        manage_lifespan: bool = True,
    ) -> None:
        self._app = app
        self._headers_provider = headers_provider
        self._timeout = timeout
        self._manage_lifespan = manage_lifespan
        self._client: AsyncClient | None = None
        self._lifespan: LifespanManager | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def manage_lifespan(self) -> bool:
        return self._manage_lifespan

    async def start(self) -> None:
        """Idempotently start the lifespan (when managed) and the HTTP client."""
        if self._closed:
            raise RouterQLRuntimeError("RouteInvoker has been closed")
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            if self._manage_lifespan:
                # LifespanManager only drives startup/shutdown; requests always
                # go to the original app (it is not an ASGI wrapper).
                manager = LifespanManager(self._app)
                await manager.__aenter__()
                self._lifespan = manager
            self._client = AsyncClient(
                transport=ASGITransport(app=self._app),
                base_url="http://routerql.local",
                timeout=self._timeout,
            )

    async def aclose(self) -> None:
        """Shut the client and lifespan down; the invoker is terminal afterwards."""
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)
            self._lifespan = None

    async def invoke(self, route: RouteInfo, kwargs: Mapping[str, Any]) -> Any:
        """Execute one route and return its JSON response."""
        await self.start()
        client = self._client
        if client is None:  # pragma: no cover - start() guarantees a client
            raise RouterQLRuntimeError("RouteInvoker failed to start")

        extra_headers: dict[str, str] = {}
        if self._headers_provider is not None:
            provided = self._headers_provider()
            if inspect.isawaitable(provided):
                provided = await provided
            extra_headers = {str(k): str(v) for k, v in dict(provided).items()}

        plan = build_request(route, kwargs, extra_headers)
        response = await client.request(
            plan.method,
            plan.path,
            params=list(plan.params),
            json=plan.json_body,
            headers=plan.headers,
        )
        if response.status_code >= 400:
            raise _http_error(route, response)
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return {"_raw": response.text}


def _http_error(route: RouteInfo, response: Response) -> GraphQLError:
    body: Any = response.text
    if "application/json" in response.headers.get("content-type", ""):
        body = response.json()
    rendered = str(body)
    if len(rendered) > _MAX_ERROR_BODY:
        rendered = rendered[:_MAX_ERROR_BODY] + "…"
    return GraphQLError(
        f"{route.method} {route.path} -> {response.status_code}: {rendered}",
        extensions={
            "code": f"HTTP_{response.status_code}",
            "http_status": response.status_code,
        },
    )
