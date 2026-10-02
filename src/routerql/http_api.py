"""GraphQL-over-HTTP endpoints: a GraphiQL playground + a POST executor."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from routerql.graphiql import GRAPHIQL_HTML

logger = logging.getLogger(__name__)


def create_graphql_router(
    handler: Any,
    *,
    graphql_path: str = "/graphql",
    graphiql_path: str = "/graphiql",
) -> APIRouter:
    """Build an APIRouter serving GraphiQL and a GraphQL HTTP endpoint.

    Args:
        handler: A ``RouterGraphQLHandler``.
        graphql_path: POST endpoint executing GraphQL documents. Body:
            ``{"query": str, "variables"?: dict, "operationName"?: str}``.
        graphiql_path: GET endpoint serving the playground, wired to
            ``graphql_path``.
    """
    router = APIRouter()

    @router.get(graphiql_path, include_in_schema=False)
    async def graphiql() -> HTMLResponse:
        html = GRAPHIQL_HTML.replace("{graphql_url}", graphql_path)
        return HTMLResponse(html)

    @router.post(graphql_path)
    async def execute_graphql(request: Request) -> JSONResponse:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse(
                {"errors": [{"message": "Request body must be JSON."}]}, status_code=400
            )
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return JSONResponse(
                {"errors": [{"message": "Missing 'query' in request body."}]},
                status_code=400,
            )
        variables = body.get("variables")
        operation_name = body.get("operationName")
        result = await handler.execute(
            query,
            variables=variables if isinstance(variables, dict) else None,
            operation_name=operation_name if isinstance(operation_name, str) else None,
        )
        status = 200 if result.get("data") is not None else 400
        return JSONResponse(result, status_code=status)

    @router.get(graphql_path, include_in_schema=False)
    async def graphql_usage() -> JSONResponse:
        return JSONResponse(
            {
                "error": "GraphQL over HTTP is POST-only.",
                "usage": {
                    "method": "POST",
                    "path": graphql_path,
                    "body": {"query": "...", "variables": {}, "operationName": None},
                    "playground": graphiql_path,
                },
            },
            status_code=405,
        )

    return router
