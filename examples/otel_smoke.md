# OTel 冒烟测试启动指南（给执行 agent 的操作手册）

分支：**`otel-smoke`**（基于 master，新增 `examples/otel_smoke.py`）

目标：验证 fastapi-gql-mcp 的可观测性现状 —— 装上 OpenTelemetry SDK 后，
fastmcp 的 tool 级 span 和 FastAPI ≥0.142 的路由级 span 免费出现，零桥代码。
**所有依赖通过 `uv run --with` 临时安装，不修改项目环境。**

---

## 步骤 0：进入仓库与分支

```bash
cd <仓库路径>
git fetch
git checkout otel-smoke
git log --oneline -1   # 应看到: 09f0b81 docs(examples): otel_smoke ...
```

前置条件：uv 已安装；模式二需要 Docker。

---

## 步骤 1（模式一）：console 输出 —— 零外部依赖，先跑这个

```bash
uv run --with opentelemetry-sdk python examples/otel_smoke.py --mode console
```

**预期输出**：最后打印 `query result: {'success': True, ...}`，
此前输出若干 JSON 格式的 span（约 11 个 `"name"` 字段）。

**核对 span 名**（出现即通过）：

- `tools/call graphql_query` —— fastmcp 发的 tool 级 span
- `GET /things`、`fastapi.dependencies`、`fastapi.endpoint`、
  `fastapi.serialization` —— FastAPI 0.142 原生路由级 span
- `server/discover` / `tools/list` —— MCP 握手 span

**关键观察**：`GET /things` 的 `trace_id` 与 `tools/call graphql_query`
的 `trace_id` **不同** —— 两棵树是断开的（桥的进程内调用未携带
traceparent，即待实施的 L3 工作）。这是本冒烟测试要证明的核心事实。

---

## 步骤 2（模式二）：Jaeger 可视化 —— 浏览器看 span 瀑布

### 2.1 启动 Jaeger

```bash
docker run -d --name jaeger-smoke \
    -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
```

如果 Docker Hub 拉取超时，换镜像仓库前缀：

```bash
docker pull docker.m.daocloud.io/jaegertracing/all-in-one:latest
docker run -d --name jaeger-smoke \
    -p 16686:16686 -p 4317:4317 \
    docker.m.daocloud.io/jaegertracing/all-in-one:latest
```

### 2.2 运行脚本（OTLP 导出）

```bash
uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
    python examples/otel_smoke.py --mode otlp
```

若 grpcio 安装慢，加清华镜像环境变量：

```bash
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
    python examples/otel_smoke.py --mode otlp
```

### 2.3 验证 span 已到达（两种方式任选）

浏览器：打开 http://localhost:16686 → Search → Service 选
`fastapi-gql-mcp-smoke` → Find Traces。

命令行（agent 可直接判定）：

```bash
curl -s "http://localhost:16686/api/services"          # 应含 fastapi-gql-mcp-smoke
curl -s "http://localhost:16686/api/traces?service=fastapi-gql-mcp-smoke&limit=5"
```

**预期**：3 条左右 trace。其中一条含 `tools/call graphql_query`（MCP 树），
另一条含 `GET /things` + 三个 `fastapi.*`（路由树）—— 两条 trace 的
traceID 不同，与步骤 1 的结论一致。

---

## 步骤 3：清理

```bash
docker stop jaeger-smoke && docker rm jaeger-smoke
```

`--with` 安装的包随 uv 临时环境消失，无需清理。

---

## 判定标准汇总

| 检查项 | 通过条件 |
|---|---|
| 脚本执行 | 输出 `query result: {...success: True...}` |
| console span | 出现 `tools/call graphql_query` 与 `GET /things` |
| Jaeger 接收 | `/api/services` 含 `fastapi-gql-mcp-smoke` |
| 两树分离 | MCP 树与路由树 traceID 不同（记录下来，作为 L3 完成前的基线） |
