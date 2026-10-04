# fastapi-gql-mcp vs fastapi-mcp 对照分析

> 对照对象：**fastapi-gql-mcp 0.3.0**（本仓库，FastAPI → GraphQL → MCP）vs
> **fastapi-mcp 0.4.0**（[tadata-org/fastapi_mcp](https://github.com/tadata-org/fastapi_mcp) ⭐12k，FastAPI → MCP 直暴露）
>
> 完整可视化报告：[index.html](./index.html) · 基准数据与复现方法：[bench/](./bench/)
> 所有数字均为本机实测（2026-10-04），非估算。

## 一句话结论

两条路线解决同一个问题的不同侧面：**fastapi-mcp 把端点变成工具**（端点 = 工具，最小心智负担），
**fastapi-gql-mcp 把 API 变成一张可组合的类型化查询图**（schema = 契约，上下文经济性 + 组合能力）。
小而稳的 API 用前者足够；API 规模增长、需要给 agent 省上下文、需要组合查询/字段投影/写操作门禁/OAuth 登录体验时，后者的架构优势开始兑现。

## 架构差异（根源）

| | fastapi-mcp | fastapi-gql-mcp |
|---|---|---|
| 中间层 | 无（OpenAPI schema → 工具清单） | GraphQL schema（域树、类型、描述链） |
| 工具模型 | **每个端点一个工具**（N 工具） | **固定 2–6 个工具** + schema 即契约（simple/progressive 两模式） |
| 工具命名 | FastAPI operationId：`list_notes_api_notes_get` | 端点函数名：`list_notes` |
| 调用执行 | OpenAPI 参数 → 还原 HTTP 请求（ASGI 进程内） | GraphQL 执行 → 路由调用（ASGI 进程内） |
| 组合能力 | 无，一次调用 = 一个端点 | 一次 `graphql_query` 组合多域多字段、别名、字段投影 |

## 实测关键数字（共享同一个 notes 应用，双环境对称驱动）

### 上下文经济性（agent 需要吞下的工具目录 + schema，字节/4 ≈ tokens）

| 端点数 | fastapi-mcp 工具目录 | fastapi-gql-mcp 全量（工具+SDL） | fastapi-gql-mcp 渐进式（单域发现） |
|---|---|---|---|
| 5 | 685 tok | 796 tok | 1,239 tok |
| 10 | 1,219 tok | 943 tok | 1,280 tok |
| 25 | 2,835 tok | 1,181 tok | 1,281 tok |
| 50 | 5,542 tok | 1,581 tok | **1,281 tok（持平）** |
| 100 | **10,954 tok** | 2,381 tok | **1,281 tok（持平）** |

- fastapi-mcp 随端点数**线性增长**（100 端点 ≈ 11k tokens 只算工具目录）
- fastapi-gql-mcp simple 模式缓增（SDL 很紧凑），**渐进式模式上下文恒定**（单域按需发现）
- 诚实反例：**5 端点的小 API，fastapi-mcp 反而更省**（685 vs 796）—— 没有 N+1 组合需求时它的简单就是优势

### 往返与响应体积

| 任务 | fastapi-mcp | fastapi-gql-mcp |
|---|---|---|
| "过滤笔记 + 统计" 组合任务 | **2 次工具调用**（2 个 agent 轮次） | **1 次** `graphql_query` |
| 同一列表（20 条笔记）响应 | 2,875 B（全量 + indent=2） | 全量 2,341 B / **投影后 610 B（4.7×↓）** |

### 延迟（进程内微基准，200 次迭代）

| | p50 | p95 |
|---|---|---|
| fastapi-mcp 单调用 | **0.91 ms** | 1.00 ms |
| fastapi-gql-mcp 单查询 | 1.36 ms | 1.93 ms |

诚实结论：**单次平凡调用他们更快**（无 GraphQL 执行层）。但真实 agent 成本由**轮次**主导
（每轮含 LLM 推理，秒级），1ms 级差距远小于"1 轮 vs 2 轮"的差别 —— 组合能力才是省时间的杠杆。

## 定性差异（详见 index.html）

| 维度 | fastapi-mcp | fastapi-gql-mcp |
|---|---|---|
| 错误语义 | HTTP ≥400 → **整个工具调用失败** | **字段级置空** + `extensions.code=HTTP_401/404`，兄弟字段照常返回 |
| 写操作门禁 | 无概念（全部端点可写） | `allow_mutation` + `mutation_include` 白名单 + 工具级操作类型守卫 |
| 认证 | OAuth 发现/授权**代理** + 假 DCR；端点防护靠自带 FastAPI `Depends`；不验 token | `auth=` 完整 OAuth 2.1 代理（DCR+PKCE+consent+引用型 token+**端点门禁**）；`passthrough_headers` 按调用者透传 |
| 传输 | SSE / streamable HTTP / stdio（可分离部署） | streamable HTTP（按调用者凭据透传需要 HTTP 上下文，stdio 已移除） |
| 人的入口 | 无 | GraphiQL + `POST /graphql`（同一 schema 服务人和 agent） |
| 生态兼容 | `mcp>=1.12` **无上界**，但在 mcp 2.x 上**直接崩溃**（`Server` 签名变更）——本基准被迫双环境 | `fastmcp<5` 锁上界，4.0.10 端到端验证 |
| 代码规模 | ~2.0k LOC，直接基于 mcp SDK | ~2.6k LOC，基于 graphql-core + fastmcp |

## 规范演进：MCP 的 lazy 机制与本对照的关系（2026-10 核实）

用户容易把几层东西混为一谈，分层核实如下（依据：本地 mcp SDK 2.3.0 内嵌协议类型 + 生态检索）：

| 层 | 机制 | 状态 | 省不省 agent 上下文 |
|---|---|---|---|
| 核心规范 | `tools/list` 分页（`nextCursor`，2025-06-18 起） | 已发布 | **不省** —— 分页只是传输分块，agent 选工具仍需全量目录 |
| 核心规范 | `server/discover` + `cacheScope`（2026-07-28 修订） | 已发布 | 不减体积 —— 帮的是 **prompt cache 复用**（public/private 缓存语义） |
| 规范扩展 | **Tool Search 扩展**（`tools/search`，只留名称/摘要、按需拉取完整定义） | **draft，需客户端+服务端双边支持**，生态采纳进行中 | **省** —— 这是真正的"lazy 工具加载" |
| 库层 | fastmcp 4.x `SearchTransform`（Regex/BM25）：整个目录折叠成 `search_tools` + `call_tool` 两个普通工具 | **今天可用**，任意 MCP 客户端（纯工具实现，无需扩展） | 省 |

**对本对照的含义（诚实修正）**：

1. fastapi-mcp 的"目录线性增长"是**开箱即用的现状**，不是永久死刑 —— 他们可以自己实现 search 折叠（其裸 mcp SDK 1.x 无现成件），或等 Tool Search 扩展普及且客户端支持
2. 我们库的**渐进式披露与上述 lazy 模式同型**，且是 plain-tools 实现 —— 不依赖任何扩展，今天在任何客户端上工作；即便工具目录问题未来被扩展彻底解决，**组合查询、字段投影、字段级错误隔离、写门禁仍只有 GraphQL 路线能给** —— 工具目录体积只是本对照的一个维度，不是全部
3. fastmcp 的 `SearchTransform` 对我们属于"备用弹药"：我们的目录本来就恒定在 2–6 个工具，无需折叠；若未来 simple 模式想给超小上下文选项，可叠加

## 选型建议

- **选 fastapi-mcp**：端点少（<10）且稳定、只给 agent 用、想要零配置最快接入、认证已有 FastAPI 依赖兜底
- **选 fastapi-gql-mcp**：API 会长大；上下文预算紧；agent 需要一次拿到组合视图；需要字段投影控制响应体积；需要细粒度写门禁；需要 OAuth 登录的完整体验；顺便想让人类也用 GraphQL

## 复现

前提：把对照库克隆到本仓库旁边（`env_theirs` 以 path 依赖引用它）：

```bash
gh repo clone tadata-org/fastapi_mcp ../fastapi-mcp   # 对照组源码
cd Comparison/bench
uv run --project env_ours   python run_ours.py     # 本库侧
uv run --project env_theirs python run_theirs.py   # fastapi-mcp 侧
python3 merge_results.py                             # 合并 → results.json
```

共享目标应用在 `shared_app.py`（同一份代码接入两个框架 —— 这也是"对接两个框架"的桥接代码本体）。
