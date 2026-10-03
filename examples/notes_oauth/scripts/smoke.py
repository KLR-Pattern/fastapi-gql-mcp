"""Full-flow smoke without GitHub credentials.

Mints a local session cookie and walks the protected path end to end:
/graphql without cookie (401 field) -> with cookie (data) -> MCP over HTTP
carrying the cookie -> mutation -> cleanup. With the MCP OAuth proxy
configured, the MCP leg is skipped (that login is interactive — tested from
Claude Code). Run: uv run python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import json

import httpx
from app.main import app
from app.session import SESSION_COOKIE, encode_session
from asgi_lifespan import LifespanManager
from fastmcp import Client

COOKIE = f"{SESSION_COOKIE}={encode_session({'login': 'smoke-user', 'name': 'Smoke'})}"
NOTES_QUERY = "{ notes { mine { list_notes { id title owner } } } meta { stats { users } } }"


async def main() -> None:
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/graphql", json={"query": NOTES_QUERY})
            data = r.json()["data"]
            assert data["notes"]["mine"]["list_notes"] is None, "expected 401-nulled field"
            assert r.json()["errors"][0]["extensions"]["code"] == "HTTP_401"
            assert data["meta"]["stats"]["users"] >= 1, "public field must survive"
            print("① 无 cookie: notes 字段 401 置空, 公开字段正常 ✓")

            r = await c.post(
                "/graphql", json={"query": NOTES_QUERY}, headers={"cookie": COOKIE}
            )
            data = r.json()["data"]
            assert data["notes"]["mine"]["list_notes"] == [], "fresh user starts empty"
            print("② 带 cookie: 受保护查询成功（owner 隔离为空列表） ✓")

        from app.config import mcp_oauth_configured

        if mcp_oauth_configured():
            # With the OAuth proxy on, the MCP endpoint only admits clients
            # that completed the interactive login — that flow is tested from
            # Claude Code, not headless.
            print("③④⑤ MCP: 跳过 — OAuth 登录已启用（用 Claude Code 交互式验证） ✓")
        else:
            from fastmcp.client.transports import StreamableHttpTransport

            mcp_transport = StreamableHttpTransport(
                url="http://t/mcp/",
                headers={"cookie": COOKIE},
                httpx_client_factory=lambda **kw: httpx.AsyncClient(
                    transport=transport, base_url="http://t", **kw
                ),
            )
            async with Client(mcp_transport) as client:
                result = json.loads(
                    (
                        await client.call_tool(
                            "graphql_query", {"query": NOTES_QUERY}
                        )
                    ).content[0].text
                )
                assert result["success"] is True
                print("③ MCP(带 cookie 头): graphql_query 走受保护路由 ✓")

                created = json.loads(
                    (
                        await client.call_tool(
                            "graphql_mutation",
                            {
                                "mutation": 'mutation { notes { mine { create_note(payload:'
                                ' {title: "from smoke", body: "via MCP"}) { id owner } } } }',
                            },
                        )
                    ).content[0].text
                )
                created_note = created["data"]["data"]["notes"]["mine"]["create_note"]
                note_id = created_note["id"]
                assert created_note["owner"] == "smoke-user"
                print(f"④ MCP mutation 以本人身份建笔记 id={note_id} ✓")

                deleted = json.loads(
                    (
                        await client.call_tool(
                            "graphql_mutation",
                            {
                                "mutation": f"mutation {{ notes {{ mine {{ delete_note("
                                            f"note_id: {note_id}) {{ id }} }} }}"
                            },
                        )
                    ).content[0].text
                )
                assert deleted["data"]["data"]["notes"]["mine"]["delete_note"]["id"] == note_id
                print("⑤ MCP mutation 删除本人笔记 ✓")

    print("\nSMOKE OK — 全链路（除 GitHub 跳转外）验证通过")


if __name__ == "__main__":
    asyncio.run(main())
