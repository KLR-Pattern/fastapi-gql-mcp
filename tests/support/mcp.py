"""MCP/HTTP protocol helpers shared by the e2e suites."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI


def tool_payload(result) -> dict:
    """Decode a fastmcp tool result's first text block as JSON."""
    return json.loads(result.content[0].text)


def jsonrpc_initialize(protocol_version: str = "2024-11-05") -> dict:
    """A minimal JSON-RPC initialize body for streamable-HTTP tests."""
    return {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }


def asgi_client_factory(app: FastAPI):
    """fastmcp httpx_client_factory hook routed through in-process ASGI."""

    def factory(**kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    return factory


@asynccontextmanager
async def http_client(
    app: FastAPI, base_url: str = "http://t"
) -> AsyncIterator[httpx.AsyncClient]:
    """Lifespan-managed httpx client against an in-process ASGI app."""
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url
        ) as client:
            yield client
