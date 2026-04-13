# AgentCore Runtime 核心机制：容器契约、Session、Agent 生命周期、工具集成

> 深入解析 Strands Agents 框架如何在 Amazon Bedrock AgentCore Runtime 中运行，涵盖 MicroVM Session 模型、Agent 生命周期（per-request vs per-session）、容器契约、会话管理、工具集成、启动流程。
>
> 姊妹篇：[部署与运维](./agentcore-runtime-deploy.md)（CDK 部署、架构模式、安全、可观测性、框架对比）

---

## 1. AgentCore 是什么

Amazon Bedrock AgentCore 是 AWS 提供的 **Agent 托管平台**，它的核心价值是：把 Agent 容器跑在 AWS 托管的无服务器环境中，附加 Memory、Gateway、Identity、Observability 等企业级服务。

关键特性：

- **框架无关** — 支持 Strands、LangGraph、CrewAI、LlamaIndex、OpenAI Agents SDK 等
- **模型无关** — Bedrock (Claude, Nova, Llama)、OpenAI、Gemini 均可
- **无服务器** — 自动扩缩容，无需管理基础设施
- **Session 隔离** — 每个 Session 独占一个 MicroVM（隔离 CPU/内存/文件系统）

AgentCore 提供的模块化服务：

| 服务 | 用途 |
|------|------|
| **Runtime** | 托管 Agent 容器，提供安全的无服务器执行环境 |
| **Memory** | 短期（多轮对话）+ 长期（跨 session）记忆管理 |
| **Gateway** | 将 API/Lambda 转换为 MCP 兼容工具，透明注入 OAuth 凭证（详见 [部署篇 Section 4](./agentcore-runtime-deploy.md#4-安全模型) 及 [OAuth 独立文档](./agentcore-oauth-integration.md)） |
| **Identity** | Inbound JWT 认证 + Outbound OAuth 凭证管理，支持 25+ IdP（详见 [部署篇 Section 4](./agentcore-runtime-deploy.md#4-安全模型)） |
| **Observability** | OpenTelemetry 集成，CloudWatch 统一监控 |
| **Code Interpreter** | 隔离沙箱中执行 Python/JS/TS |
| **Policy** | Cedar 或自然语言定义的治理规则 |

### 阅读导航：两条主线 × 两种认证

本文涉及多个维度的选择，先看清自己的路径再往下读：

**维度 1：构建方式（怎么写代码）**

| | BedrockAgentCoreApp | FastAPI 自建 |
|---|---|---|
| 代码风格 | `@app.entrypoint` + `yield` | `@app.post("/invocations")` + `StreamingResponse` |
| 端点生成 | 自动（`/invocations`、`/ping`、`/ws`） | 手动定义每个端点 |
| 异步任务 | 内置 `add_async_task` + Worker Loop | 需自建 |
| 详见 | [部署篇 Section 6](./agentcore-runtime-deploy.md#6-bedrockagentcoreapp-vs-fastapi-自建构建方式对比) | 本文 Section 2-5（容器契约、请求格式、Agent 生命周期） |

**维度 2：部署方式（怎么上线）**

| | CDK L2（推荐） | CDK L1 + 自建容器 | Starter Toolkit CLI |
|---|---|---|---|
| 命令 | `AgentRuntimeArtifact.fromAsset/fromS3` | `CfnRuntime` + Dockerfile + ECR | `agentcore deploy` |
| 需要 Docker | 视 artifact 来源（`fromS3` 不需要） | 是 | 否 |
| 适用 | **生产环境**（统一编排所有资源） | 生产环境（需要完全控制构建） | **Demo / 原型验证** |
| 详见 | [部署篇 Section 1](./agentcore-runtime-deploy.md#1-cdk-部署) | [部署篇 Section 1](./agentcore-runtime-deploy.md#1-cdk-部署) | [部署篇 Section 6](./agentcore-runtime-deploy.md#6-bedrockagentcoreapp-vs-fastapi-自建构建方式对比) |

**维度 3：认证方式（谁来调用 Agent）**

| | IAM SigV4（默认） | JWT Bearer Token（OAuth） |
|---|---|---|
| 配置 | 无需额外配置 | `authorizerConfiguration` + `customJWTAuthorizer` |
| 调用方式 | `boto3.invoke_agent_runtime()` | HTTPS + `Authorization: Bearer {JWT}` |
| 二者关系 | **互斥**，一个 Runtime 只能选一种 | |
| 详见 | — | [部署篇 Section 4](./agentcore-runtime-deploy.md#4-安全模型) 及 [OAuth 独立文档](./agentcore-oauth-integration.md) |

> **快速定位**：核心运行机制 → 本文。部署、安全、可观测性、框架对比 → [部署篇](./agentcore-runtime-deploy.md)。OAuth 详解 → [OAuth 独立文档](./agentcore-oauth-integration.md)。

---

## 2. Runtime 容器契约

AgentCore Runtime 本质上是一个**基于 MicroVM 的容器托管服务**。你提供一个 Docker 镜像，AgentCore 为每个 Session 启动一个独占的 MicroVM 来运行它。容器必须满足以下契约：

### 2.1 必须实现的端点

| 端点 | 方法 | 用途 | 必须 |
|------|------|------|------|
| `/invocations` | POST | 接收聊天请求，返回 SSE 流 | 是 |
| `/ping` | GET | 健康检查。返回 `{"status": "Healthy"}` 或 `{"status": "HealthyBusy"}`（有后台任务时），可选 `time_of_last_update` 字段 | 是 |
| `/health` | GET | 详细健康状态 | 否（可选） |
| `/ws` | WebSocket | WebSocket 双向通信（与 /invocations 共用 8080 端口） | 否 |

### 2.2 容器要求

**AgentCore 要求：**
```
端口:     8080
平台:     linux/arm64（官方文档要求 ARM64 兼容）
协议:     HTTP / MCP / A2A / AGUI（选一）
用户:     非 root（如 uid=1000, bedrock_agentcore）
网络模式:  PUBLIC 或 VPC
```

### 2.3 CORS 配置

建议配置 CORS 允许 AgentCore 域名。AgentCore 调用容器是服务端行为，CORS 主要用于 WebSocket 或浏览器直连场景（如 AgentCore Console 测试）：

```python
# [FastAPI 自建]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://bedrock-agentcore.{aws_region}.amazonaws.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 2.4 认证

AgentCore 支持两种**互斥**的认证方式：**IAM SigV4**（默认）和 **JWT Bearer Token**（需配置 `authorizerConfiguration`）。

**JWT Bearer 模式**下，AgentCore 在请求到达容器之前自动验证 JWT 签名和 claims，容器**不需要**自行验证。容器获取用户身份：
- **方式 A**：从请求体中提取 `user_id`（简单直接，调用方传入）
- **方式 B**：解码 JWT claims 获取 `sub`、`scope` 等信息（需配置 `--request-header-allowlist "Authorization"`）

**SigV4 模式**下，调用方通过 AWS SDK（boto3 等）调用，IAM 策略控制访问。如果 Agent 仍需代表特定用户获取 Outbound OAuth token：
- **方式 C**：调用方通过 `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header 指定用户 ID（需额外 IAM 权限 `InvokeAgentRuntimeForUser`）

详见 [部署篇 Section 4](./agentcore-runtime-deploy.md#4-安全模型) 及 [OAuth 独立文档](./agentcore-oauth-integration.md)。

---

## 3. 请求/响应格式

AgentCore 的 `InvokeAgentRuntime` API 将 `payload` 作为字节流传入容器的 `/invocations` 端点，内部格式由应用自行定义。以下是一个典型的应用层格式：

### 3.1 请求体 (ChatRequest)

```json
{
  "id": "chat-session-abc123",
  "user_id": "user@example.com",
  "messages": [
    {
      "id": "msg-001",
      "role": "user",
      "content": "帮我查一下最近的订单",
      "parts": [
        { "type": "text", "text": "帮我查一下最近的订单" }
      ]
    }
  ]
}
```

| 字段 | 含义 |
|------|------|
| `id` | Session ID，用于多轮对话关联 |
| `user_id` | 用户标识，来自 JWT sub/email |
| `messages` | 消息列表，包含完整对话历史 |
| `messages[].parts` | 消息内容的结构化表示（支持 text、attachment 等） |

### 3.2 响应格式 (SSE Stream)

响应必须是 `text/event-stream`，逐事件推送：

```
data: {"type":"start","session_id":"chat-session-abc123","status":"executing"}

data: {"type":"tool-input-start","toolCallId":"tool-001","toolName":"search_orders"}

data: {"type":"tool-output-available","toolCallId":"tool-001","executionTimeMs":42}

data: {"type":"text-delta","delta":"找到 2 个订单："}

data: {"type":"text-delta","delta":"\n1. ORD-2024-001..."}

data: {"type":"finish","session_id":"chat-session-abc123"}

data: [DONE]
```

事件类型：

| type | 含义 |
|------|------|
| `start` | Agent 开始执行 |
| `text-delta` | 增量文本输出（流式打字效果） |
| `tool-input-start` | 工具调用开始 |
| `tool-output-available` | 工具执行完成 |
| `error` | 执行出错 |
| `finish` | Agent 执行完成 |
| `[DONE]` | SSE 流结束标记 |

### 3.3 外部调用示例（JWT Bearer 模式）

> 认证方式选择（JWT Bearer vs SigV4）见 [Section 2.4](#24-认证)，完整说明见 [部署篇 Section 4](./agentcore-runtime-deploy.md#4-安全模型) 及 [OAuth 独立文档](./agentcore-oauth-integration.md)。

当 Runtime 配置了 JWT Inbound Auth 时，调用方直接发起 HTTPS 请求到 AgentCore 平台端点：

```
POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urlEncodedArn}/invocations?qualifier=DEFAULT
```

**完整请求示例（TypeScript）：**

```typescript
const escapedArn = encodeURIComponent(agentRuntimeArn);
const endpoint = `https://bedrock-agentcore.${settings.region}.amazonaws.com`
  + `/runtimes/${escapedArn}/invocations?qualifier=DEFAULT`;

const response = await fetch(endpoint, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${auth.user.access_token}`,               // Cognito JWT
    'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': currentChatId,      // Session 关联
    'X-Amzn-Bedrock-AgentCore-Runtime-User-Id': userId,                // 可选
  },
  body: JSON.stringify({
    id: currentChatId,
    user_id: userId,
    messages: messageHistory,
  }),
});
// response 是 SSE 流 (text/event-stream)
```

**等效 cURL：**

```bash
ESCAPED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$AGENT_ARN', safe=''))")

curl -N \
  "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: session-001" \
  -d '{"id":"session-001","user_id":"user@example.com","messages":[{"role":"user","content":"Hello","parts":[{"type":"text","text":"Hello"}]}]}'
```

**关键点：**

| 要素 | 说明 |
|------|------|
| **端点** | `https://bedrock-agentcore.{region}.amazonaws.com` |
| **路径** | `/runtimes/{urlEncodedArn}/invocations` — ARN 必须 URL encode（含 `/` 和 `:` 等特殊字符） |
| **查询参数** | `qualifier=DEFAULT`（必须） |
| **认证** | `Authorization: Bearer {JWT}` — 由 Inbound JWT Authorizer 在平台层验证 |
| **Session Header** | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` — 相同 Session ID 的请求路由到同一个 MicroVM |
| **请求体** | 应用自定义 JSON，AgentCore 透传到容器 `/invocations` 端点 |
| **响应** | SSE 流（`text/event-stream`），格式见 [Section 3.2](#32-响应格式-sse-stream) |

> **`InvokeAgentRuntimeCommand`** 也是通过 AgentCore 平台端点调用（非容器内端点），HTTP 路径推测为 `/runtimes/{arn}/command`，但截至 boto3 1.42.64 该 API 尚未在 SDK 中发布，官方文档仅提供 SDK 示例（见 [Section 5.5](#55-异步后台任务)）。

---

## 4. MicroVM Session 模型

理解 AgentCore Runtime 的关键在于：**一个 Session = 一个独占的 MicroVM**。这不是传统的无服务器容器池（如 Lambda），而是有状态的、会话亲和的执行环境。

### 4.1 Session 生命周期

```
客户端首次调用 InvokeAgentRuntime(sessionId=abc)
                              │
                    ┌─────────v──────────┐
                    │  创建 MicroVM      │
                    │  (独占 CPU/Mem/FS) │
                    │  启动容器进程      │
                    └─────────┬──────────┘
                              │
              ┌───────────────v───────────────┐
              │          Active               │
              │  处理请求 / 执行后台任务       │
              └───────────────┬───────────────┘
                              │ 请求完成
              ┌───────────────v───────────────┐
              │           Idle                │
              │  保持上下文，等待下次请求      │
              │  (MicroVM 仍在运行)           │
              └───────────────┬───────────────┘
                              │
                   ┌──────────┴──────────┐
                   │                     │
              新请求到来              超时/到期
              (同一 sessionId)            │
                   │          ┌──────────v──────────┐
                   │          │     Terminated      │
                   │          │  MicroVM 销毁       │
                   │          │  内存清洗           │
                   │          └─────────────────────┘
                   v
              回到 Active（同一个 MicroVM、同一个进程）
```

**关键参数：**

| 参数 | 值 |
|------|-----|
| 空闲超时 | 15 分钟无请求 → 自动终止 |
| 最大生命周期 | 8 小时 |
| Session ID 要求 | 至少 33 字符（建议 UUID） |
| 终止后行为 | 同一 sessionId 的新请求会创建**全新 MicroVM**（状态丢失） |

**核心保证：同一 Session 的所有请求，始终路由到同一个 MicroVM。** 容器内的进程、内存、文件系统在 Session 生命周期内持续存在。

### 4.2 Session 状态

| 状态 | 含义 |
|------|------|
| **Active** | 正在处理同步请求、执行命令或后台任务 |
| **Idle** | 无活跃请求，但 MicroVM 保持运行，等待下次调用 |
| **Terminated** | 因空闲超时(15min)、达到最大时长(8h)或健康检查失败而终止 |

### 4.3 与传统无服务器的对比

| 维度 | Lambda / 传统 Serverless | AgentCore Runtime |
|------|-------------------------|-------------------|
| 执行单元 | 请求级（每次调用可能不同实例） | **Session 级（独占 MicroVM）** |
| 状态模型 | 无状态 | **Session 内有状态** |
| 最大运行时间 | 15 分钟 | **8 小时** |
| 文件系统 | 临时（/tmp），跨请求不保证 | **Session 内持久** |
| 内存状态 | 冷启动可能丢失 | **Session 内持久** |
| 隔离粒度 | 函数级 | **Session 级 MicroVM** |
| 硬件配置 | 可选（内存 128MB-10GB） | **不可选**（AWS 托管，未公开 vCPU/内存/存储规格） |

---

## 5. Agent 生命周期模式

AgentCore 的 MicroVM 模型为 Strands Agent 的生命周期管理提供了两种可行模式。

### 5.1 Per-request 模式（当前实现）

每次 `/invocations` 请求创建一个新的 Agent 实例，通过 `session_manager` 从外部存储恢复对话历史：

```
请求 1 (session=abc, MicroVM-A)
  → 新建 Agent + session_manager.load()   # 反序列化历史
  → agent.stream_async("你好")
  → session_manager.save()                # 序列化回存储
  → Agent 实例丢弃（但 MicroVM 仍在）

请求 2 (session=abc, MicroVM-A)           # 同一个 MicroVM！
  → 新建 Agent + session_manager.load()   # 再次反序列化
  → agent.stream_async("查询订单")
  → session_manager.save()
  → Agent 实例丢弃
```

对应代码：

```python
# [FastAPI 自建]
@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    session_id = request.id
    user_id = request.user_id

    # 每次请求都新建 session_manager 和 Agent
    session_manager = _create_session_manager(session_id, user_id)
    model = BedrockModel(model_id=model_id, boto_session=boto_session)
    agent = Agent(
        system_prompt=agent_system_prompt,
        model=model,
        tools=all_tools,
        session_manager=session_manager,
    )

    async def event_generator():
        async for event in agent.stream_async(user_message):
            yield sse_event(event)
    ...
```

**特点：** 实现简单，但每次请求都有 session_manager 反序列化开销。

### 5.2 Per-session 模式（可优化方向）

利用 AgentCore MicroVM 的 Session 亲和性，在进程内缓存 Agent 实例。同一 Session 的后续请求直接复用，省去反序列化开销：

```
请求 1 (session=abc, MicroVM-A)
  → 新建 Agent + session_manager.load()   # 首次：从外部存储恢复
  → agent.stream_async("你好")
  → 缓存 Agent 实例到进程内存

请求 2 (session=abc, MicroVM-A)           # 同一个 MicroVM！
  → 命中缓存，直接复用 Agent              # 历史已在内存中
  → agent.stream_async("查询订单")          # 无需反序列化
```

Strands Agent 天然支持多次调用——每次 `agent(message)` 会将新消息**追加**到内部对话历史，而不是重新开始。这使得 per-session 复用是安全的：

```python
# Strands Agent 支持多次调用，自动累积历史
agent = Agent(system_prompt="...", model=model, tools=tools)
agent("你好")              # 第 1 轮
agent("帮我查一下最近的订单")  # 第 2 轮，自动带上第 1 轮的上下文
agent("那上个月的呢？")       # 第 3 轮，自动带上前 2 轮的上下文
```

Per-session 模式的实现思路：

```python
# [FastAPI 自建]
_agent_instance: Agent | None = None

@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    global _agent_instance

    if _agent_instance is None:
        # 首次请求：创建 Agent（session_manager 恢复历史）
        session_manager = _create_session_manager(request.id, request.user_id)
        _agent_instance = Agent(
            system_prompt=agent_system_prompt,
            model=model,
            tools=all_tools,
            session_manager=session_manager,
        )

    # 后续请求：复用 Agent，对话历史已在内存中
    async def event_generator():
        async for event in _agent_instance.stream_async(user_message):
            yield sse_event(event)
    ...
```

> 注：示例使用 `global` 变量仅为简化说明。生产代码可以用 `app.state` 或 FastAPI 依赖注入替代。

**为什么在 AgentCore 中是安全的：**
- **一个 MicroVM = 一个 Session = 一个进程** — 不存在不同 Session 共享进程的情况
- **`/invocations` 请求串行** — 同一 Session 同一时刻只处理一个 `/invocations` 请求（但 `InvokeAgentRuntimeCommand` 可与之并发执行，后台任务也可同时运行）
- **MicroVM 在 Session 内持久** — 进程内存不会被意外回收

### 5.3 两种模式对比

| 维度 | Per-request（当前） | Per-session |
|------|-------------------|-------------|
| Agent 创建 | 每次请求新建 | 首次请求创建，后续复用 |
| 历史恢复 | 每次从 session_manager 反序列化 | 首次反序列化，后续靠内存 |
| 第 N 轮延迟 | 与第 1 轮相同（都要反序列化） | 显著降低（跳过反序列化） |
| 实现复杂度 | 简单 | 需处理 MicroVM 终止后的重建 |
| AgentCore 兼容性 | 完全兼容 | 完全兼容 |
| **可移植性** | **任何平台均可运行** | **仅限 AgentCore** |

> **可移植性是关键权衡。** Per-session Agent 依赖 AgentCore MicroVM 的 Session 亲和保证（同一 Session 的请求始终路由到同一个 MicroVM）。如果未来需要将 Agent 迁移到 ECS、EKS、Lambda 等无状态平台，per-session 模式将失效——这些平台不保证同一 session 的请求落在同一个容器实例上。Per-request + session_manager 模式则天然兼容任何部署目标，因为状态完全外置。
>
> **MCPClient 的生命周期与 Agent 无关。** 无论 per-request 还是 per-session，MCPClient 都应在 startup 建立一次并在进程级别复用。详见 [Section 7.4](#74-mcpclient-生命周期管理)。

### 5.4 session_manager 的角色

无论采用哪种 Agent 生命周期模式，`session_manager` 都有其存在价值：

| 场景 | session_manager 的作用 |
|------|----------------------|
| **Per-request 模式** | 每次请求从外部存储恢复历史，执行后回写 |
| **Per-session 模式** | 仅在 MicroVM 首次启动时恢复历史；后续请求靠 Agent 内存 |
| **MicroVM 终止后重建** | 15 分钟超时后 MicroVM 销毁，新 MicroVM 需要从 Memory/S3 恢复历史 |
| **AgentCore Memory 前端可见性** | 将对话历史同步到 Memory 服务，使前端 chat history 可见 |

### 5.5 异步后台任务

AgentCore Runtime 支持**异步后台任务**：Agent 在响应用户后继续执行长时间运行的工作（数据处理、文件生成、模型训练等），同时保持 Session 存活。

> **构建方式说明**：本节的 `@app.async_task`、`add_async_task`、`@app.ping` 等 API 均为 **BedrockAgentCoreApp** SDK 提供。如果使用 **FastAPI 自建**，只需让 `/ping` 端点在有后台任务时返回 `{"status": "HealthyBusy"}` 即可（见本节末尾的 FastAPI 示例）。

#### 核心问题：15 分钟空闲超时

正常情况下，Session 在最后一次 `/invocations` 请求完成后进入 Idle 状态，15 分钟无新请求则自动终止。如果 Agent 启动了后台任务但已返回响应，AgentCore 会认为 Session 空闲并销毁 MicroVM，导致后台任务被强杀。

**解决方案：`/ping` 返回 `HealthyBusy` 状态。** AgentCore 持续轮询 `/ping`，当返回 `HealthyBusy` 时，Session 保持 Active 状态，空闲超时计时器暂停。

```
请求完成后的 Session 状态：

/ping → {"status": "Healthy"}      → Idle 状态 → 15 分钟后终止
/ping → {"status": "HealthyBusy"}  → Active 状态 → 不终止，直到任务完成或达到 8 小时上限
```

#### `/ping` 响应格式

```json
{
  "status": "Healthy",           // 或 "HealthyBusy"
  "time_of_last_update": 1752275688  // Unix 时间戳，状态最后一次变更的时间
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `"Healthy"`（空闲，可接受新请求）或 `"HealthyBusy"`（有后台任务） |
| `time_of_last_update` | int | 状态最后一次变更的 Unix 时间戳，AgentCore 用来判断 Session 在当前状态持续了多久 |

#### BedrockAgentCoreApp SDK 提供的三种方式

`bedrock-agentcore` SDK 的 `BedrockAgentCoreApp` 类内置了完整的异步任务管理（源码见 `bedrock_agentcore/runtime/app.py`）：

**方式 1：`@app.async_task` 装饰器**

> 此装饰器存在于 SDK 源码（`bedrock_agentcore/runtime/app.py`）中，但**未在官方文档中记录**，属于未公开 API，接口可能随版本变更。生产环境建议优先使用方式 2（手动管理）。

自动跟踪任务生命周期，函数执行期间 `/ping` 返回 `HealthyBusy`，完成或异常后自动恢复 `Healthy`：

```python
# [BedrockAgentCoreApp]
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool

app = BedrockAgentCoreApp()

@app.async_task      # 自动管理 HealthyBusy 状态
async def process_large_dataset(file_path: str):
    """耗时数据处理任务。"""
    # 函数执行期间 /ping → HealthyBusy
    data = load_data(file_path)
    result = await heavy_computation(data)
    save_result(result)
    # 函数返回后 /ping → Healthy（自动恢复）

@tool
def start_data_processing(file_path: str) -> str:
    """启动后台数据处理任务。"""
    import asyncio
    asyncio.ensure_future(process_large_dataset(file_path))
    return f"数据处理已启动: {file_path}，处理完成后会通知您。"

# Agent 拥有 start_data_processing 工具
# 用户说 "处理 /data/input.csv" → Agent 调用工具 → 工具启动后台任务
agent = Agent(tools=[start_data_processing])

@app.entrypoint
def main(payload):
    # Agent 根据用户意图自主决定是否调用 start_data_processing 工具
    return {"message": agent(payload.get("prompt", "")).message}

if __name__ == "__main__":
    app.run()
```

> 注意：`@app.async_task` 只能装饰 `async` 函数，非 async 函数会抛出 `ValueError`。

**方式 2：手动 `add_async_task` / `complete_async_task`（精细控制）**

适合在线程或复杂工作流中使用，需要手动管理生命周期：

```python
# [BedrockAgentCoreApp]
import threading

@tool
def start_background_task(duration: int = 60) -> str:
    """启动一个后台处理任务。"""
    # 注册任务 → /ping 立即变为 HealthyBusy
    task_id = app.add_async_task("background_processing", {"duration": duration})

    def background_work():
        try:
            time.sleep(duration)  # 模拟耗时操作
        finally:
            # 标记完成 → 如果没有其他活跃任务，/ping 恢复 Healthy
            app.complete_async_task(task_id)

    threading.Thread(target=background_work, daemon=True).start()
    return f"后台任务已启动 (ID: {task_id})，预计 {duration} 秒完成。"
```

关键细节（来自 SDK 源码）：
- `add_async_task(name, metadata=None)` → 返回 `int` 类型的 task_id
- `complete_async_task(task_id)` → 返回 `bool`（True=成功完成，False=task_id 不存在）
- 多个任务可同时运行，只要 `_active_tasks` 字典非空，`/ping` 就返回 `HealthyBusy`
- 任务计数器使用 `threading.Lock` 保证线程安全

**方式 3：自定义 `@app.ping` 处理器（完全自定义）**

```python
# [BedrockAgentCoreApp]
from bedrock_agentcore.runtime.models import PingStatus

@app.ping
def custom_ping():
    """自定义健康检查逻辑。"""
    if gpu_task_running() or queue_has_pending_jobs():
        return PingStatus.HEALTHY_BUSY
    return PingStatus.HEALTHY
```

还可以强制覆盖状态（调试或特殊场景）：

```python
app.force_ping_status(PingStatus.HEALTHY_BUSY)   # 强制 Busy
app.clear_forced_ping_status()                     # 恢复自动检测
```

#### `/ping` 状态优先级

SDK 内部按以下优先级决定 `/ping` 响应（`get_current_ping_status()` 源码逻辑）：

```
1. forced_ping_status（force_ping_status() 设置的强制状态）
   ↓ 未设置
2. @app.ping 自定义处理器返回值
   ↓ 未注册
3. 自动检测：_active_tasks 非空 → HealthyBusy，否则 → Healthy
```

#### Worker Loop 架构：防止 `/ping` 被阻塞

AgentCore 持续轮询 `/ping` 判断 Session 健康状态。如果 `/ping` 无响应，Session 会被判定为 Unhealthy 并终止。SDK 通过 **Worker Loop 架构** 解决这个问题：

```
主线程 (uvicorn event loop)
    │
    ├── GET /ping → 直接响应（永远不阻塞）
    │
    └── POST /invocations
            │
            └── _invoke_handler()
                    │
                    ├── async 函数 → 提交到 Worker Loop（独立线程）
                    │                  ↳ agentcore-worker-loop 线程
                    │                    asyncio.run_coroutine_threadsafe()
                    │
                    ├── async generator → 通过 Worker Loop 桥接为 sync generator
                    │                      ↳ queue.Queue 生产-消费模型
                    │
                    └── sync 函数 → run_in_threadpool（Starlette 线程池）
```

**关键设计**：`/invocations` 的 handler 在**独立的 Worker Event Loop**（后台守护线程）中执行，即使 handler 阻塞了数分钟，主线程的 `/ping` 仍然可以正常响应。

> 这是 `BedrockAgentCoreApp` 相对于直接使用 FastAPI/Starlette 的核心优势。如果使用 FastAPI 自建 `/ping` 端点，且 handler 中发生长时间阻塞，理论上可能导致 `/ping` 延迟响应。

#### `InvokeAgentRuntimeCommand`：同一 Session 执行命令

除了 `InvokeAgentRuntime`（对应 `/invocations`），AgentCore 还提供 `InvokeAgentRuntimeCommand`，可以在**同一个 MicroVM Session** 中执行 shell 命令：

```python
# 调用方使用同一个 session_id
response = client.invoke_agent_runtime_command(
    agentRuntimeArn=agent_arn,
    runtimeSessionId="user-123-session-abc",  # 与 InvokeAgentRuntime 共享 Session
    qualifier='DEFAULT',
    contentType='application/json',
    accept='application/vnd.amazon.eventstream',
    body={
        'command': '/bin/bash -c "git status"',
        'timeout': 60  # 秒
    }
)
```

> **前提条件**：2026 年 3 月 17 日之后创建的 Agent 自动支持命令执行。在此日期之前部署的 Agent 需要重新部署才能使用此功能。

> **并发特性**：命令执行**不阻塞** Agent 调用。`InvokeAgentRuntime` 和 `InvokeAgentRuntimeCommand` 可以在同一 Session 上并发执行。

适用场景：
- **确定性操作**：`git commit`、`npm test`、`python -m pytest` — 无需经过 LLM 推理
- **环境检查**：查看文件系统、检查进程状态、读取日志
- **与 Agent 协同**：Agent 通过 `/invocations` 推理 → 调用方通过 `Command` 执行确定性步骤

两个 API 共享同一个 MicroVM：容器、文件系统、环境变量完全一致。

#### 异步任务典型场景

| 场景 | 做法 | /ping 状态 |
|------|------|-----------|
| **文档 OCR + 信息提取** | Agent 立即回复"正在处理"，后台线程执行 OCR | HealthyBusy → Healthy |
| **批量数据查询** | Agent 提交查询请求，后台线程轮询结果 | HealthyBusy → Healthy |
| **模型推理缓存预热** | startup 时预加载模型/缓存，完成前不接受业务请求 | HealthyBusy → Healthy |
| **用户实时对话** | 同步 SSE 流式响应，无后台任务 | 始终 Healthy |

#### FastAPI 自建：最简实现

BedrockAgentCoreApp 的 `@app.async_task`、Worker Loop 等能力，在 FastAPI 中只需几行代码即可等效实现：

```python
# [FastAPI 自建]
import threading
from fastapi import FastAPI

app = FastAPI()
_active_tasks: set[int] = set()
_task_lock = threading.Lock()
_task_counter = 0

@app.get("/ping")
def ping():
    status = "HealthyBusy" if _active_tasks else "Healthy"
    return {"status": status}

def add_task() -> int:
    global _task_counter
    with _task_lock:
        _task_counter += 1
        _active_tasks.add(_task_counter)
        return _task_counter

def complete_task(task_id: int):
    with _task_lock:
        _active_tasks.discard(task_id)
```

核心逻辑就是维护一个活跃任务集合，`/ping` 根据集合是否为空返回对应状态。BedrockAgentCoreApp 的 `add_async_task` / `complete_async_task` 本质上做的也是同样的事。

> **提示**：如果 Agent 只做同步 SSE 流式响应（用户发消息 → Agent 推理 → 实时返回），不涉及异步后台任务，`/ping` 始终返回 `Healthy` 即可，无需上述任务跟踪机制。

---

## 6. Session Manager 配置

Strands Agent 通过 `session_manager` 参数实现对话持久化。AgentCore 提供了原生 Memory 服务，同时也支持自定义 fallback。

### 6.1 三级 Fallback 策略

```
优先级 1: AgentCore Memory
    └─ 条件: AGENTCORE_MEMORY_ENABLED=true + AGENTCORE_MEMORY_ID 已配置
    └─ 特点: 原生集成，支持短期+长期记忆，前端可见历史
    └─ 数据生命周期: 持久化，超越 MicroVM 生命周期

优先级 2: S3 Session Manager
    └─ 条件: S3 Bucket 环境变量已配置（如 DATA_BUCKET_NAME，名称自定义）
    └─ 路径: s3://{bucket}/sessions/{agent_type}/{session_id}
    └─ 数据生命周期: 持久化

优先级 3: File Session Manager
    └─ 条件: 始终可用（本地开发 fallback）
    └─ 路径: ./sessions/{session_id}
    └─ 数据生命周期: MicroVM 内持久，MicroVM 终止后丢失
```

> **注意**：在 AgentCore 中使用 FileSessionManager 时，由于 MicroVM 内文件系统在 Session 生命周期内持久，文件会保留到 MicroVM 终止（最长 8 小时）。但 MicroVM 终止后数据丢失，因此生产环境应使用 AgentCore Memory 或 S3。

### 6.2 Session ID 策略

多 Agent 系统中，一种推荐做法是为 session ID 添加 Agent 类型前缀，避免不同 Agent 的会话冲突（这不是 AgentCore 的要求，而是应用层的设计选择）：

```python
# 为 session ID 添加 Agent 类型前缀，避免不同 Agent 的会话冲突
# 例如：
# 订单 Agent → "order-{session_id}"
# 客服 Agent → "support-{session_id}"
prefixed_session_id = f"{agent_type}-{session_id}"

# AgentCore API 允许最长 256 字符（最短 33 字符）
# 注意：简单截断可能导致不同 session 产生相同 ID，建议使用 hash 或确保前缀+ID 总长度不超限
if len(prefixed_session_id) > 256:
    prefixed_session_id = prefixed_session_id[:256]
```

### 6.3 AgentCore Memory 的双层架构

```
短期记忆 (Short-term)
  └─ Session 粒度的多轮对话历史
  └─ 自动从历史中恢复上下文
  └─ 数据随 Session 生命周期管理

长期记忆 (Long-term)
  └─ 跨 Session 持久化
  └─ 可在多个 Agent 之间共享
  └─ 支持语义检索 (top_k, relevance_threshold)
  └─ 存储用户偏好、事实、摘要等
```

---

## 7. 工具集成方式

Agent 获取工具有两大模式：**MCP 协议**和**自定义 HTTP**。注意：工具连接（MCPClient）的生命周期与 Agent 生命周期是独立的，不应混为一谈。

### 7.1 两种工具集成模式总览

```
模式 A: MCP 协议（MCPClient 连接 MCP Server）
┌─────────────────────────────────────────────────────────┐
│  Agent (Strands)                                        │
│    ↓                                                    │
│  MCPClient (Streamable HTTP / stdio)                    │
│    ↓ MCP Protocol                                       │
│  MCP Server                                             │
│    ├── AgentCore Gateway（AWS 托管，附加 OAuth + Lambda）│
│    ├── 第三方 MCP Server（Tavily、Sentry 等）           │
│    └── 本地 MCP Server（stdio 子进程）                   │
└─────────────────────────────────────────────────────────┘
  Agent 侧统一使用 MCPClient，Server 端可以是 Gateway 或任意 MCP Server
  传输: Streamable HTTP（远程）/ stdio（本地子进程）

模式 B: 自定义 HTTP（本地 @tool 函数 + HTTP 调用）
┌─────────────────────────────────────────────────────────┐
│  Agent (Strands)                                        │
│    ↓ svc_http_request tool                              │
│  Backend API (/api/svc/*)                               │
│    ↓                                                    │
│  DynamoDB / 业务逻辑                                    │
└─────────────────────────────────────────────────────────┘
  非 MCP 协议，通过通用 HTTP 工具调后端 API，Skill MD 定义调用规范
  连接: 无持久连接，每次 HTTP 请求独立
```

### 7.2 模式 A — MCP 协议（MCPClient）

Agent 侧统一使用 Strands `MCPClient` 连接 MCP Server。`bedrock-agentcore` SDK **不会自动连接 Gateway**，需要手动创建 MCPClient。

#### 连接 AgentCore Gateway

**AgentCore Gateway 本质上是一个 AWS 托管的 MCP Server**（创建时 `protocolType: "MCP"`，端点 URL 以 `/mcp` 结尾）。Agent 通过 MCPClient + Streamable HTTP 传输连接：

**脚本/CLI 场景（短生命周期）**——`with` 块自动管理连接开关：

```python
from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamable_http_client

gateway_url = "https://{gatewayId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"
mcp_client = MCPClient(lambda: streamable_http_client(url=gateway_url))

with mcp_client:
    agent = Agent(tools=mcp_client.list_tools_sync())
    response = agent("帮我查一下最近的订单")
# with 块结束 → 连接自动关闭
```

**AgentCore Runtime 场景（长驻进程）**——MCPClient 应在 startup 创建、shutdown 关闭，不能用 `with` 块（否则退出 `with` 后连接断开，后续请求的工具调用会失败）：

```python
# [FastAPI 自建]
import os
from fastapi import FastAPI
from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamable_http_client

app = FastAPI()
gateway_url = os.environ["MCP_SERVER_URL"]
# e.g. "https://{gatewayId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"

_mcp_client: MCPClient | None = None

@app.on_event("startup")
async def startup():
    global _mcp_client
    _mcp_client = MCPClient(lambda: streamable_http_client(url=gateway_url))
    # 连接懒加载——首次工具调用时自动建立

@app.post("/invocations")
async def invocations(request: ChatRequest):
    agent = Agent(tools=[*local_tools, _mcp_client])  # 复用 MCPClient
    ...

@app.on_event("shutdown")
async def shutdown():
    if _mcp_client:
        await _mcp_client.close()  # 进程退出时关闭连接
```

如果需要传递认证 token（如 Gateway 开启了 JWT Authorizer），需要传递的 header 取决于 Gateway Target 类型：

| Target 类型 | 需要的 Header | 说明 |
|---|---|---|
| `GATEWAY_IAM_ROLE` | `Authorization`（用户 JWT） | Gateway 用自身 IAM Role 调用 Lambda |
| `OAUTH` | `Authorization` + `WorkloadAccessToken`（WAT） | Gateway 需要 WAT 从 Token Vault 获取第三方 OAuth token |

```python
import httpx
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

headers_ctx = BedrockAgentCoreContext.get_request_headers() or {}

# GATEWAY_IAM_ROLE 模式：只需转发用户 JWT
http_client = httpx.AsyncClient(headers={
    "Authorization": headers_ctx.get("Authorization", ""),
})

# OAUTH 模式：还需传递 WAT（Token Vault 按 workload+user 查找第三方 token）
# http_client = httpx.AsyncClient(headers={
#     "Authorization": headers_ctx.get("Authorization", ""),
#     "WorkloadAccessToken": BedrockAgentCoreContext.get_workload_access_token(),
# })

mcp_client = MCPClient(lambda: streamable_http_client(url=gateway_url, http_client=http_client))
```

Gateway 的完整调用链路：

```
MCPClient → Streamable HTTP POST → AgentCore Gateway（/mcp 端点）
                                        │
                                        ├── 路由到 Lambda Target（执行工具逻辑）
                                        ├── OAuth 凭证注入（Token Vault）
                                        └── 集中化工具注册（tool schema）
```

#### 连接第三方 MCP Server

连接第三方 MCP Server（如 Tavily、Sentry）方式完全一样，只是 URL 不同：

```python
# 远程 MCP Server（Streamable HTTP）
mcp_client = MCPClient(lambda: streamable_http_client(url="https://mcp.tavily.com/mcp"))

# 本地 MCP Server（stdio 子进程）
from mcp import stdio_client, StdioServerParameters
mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["some-mcp-server"])
))
```

#### Gateway vs 直连：差异仅在 Server 端

| 维度 | AgentCore Gateway | 第三方 MCP Server |
|------|-------------------|-------------------|
| Agent 侧代码 | `MCPClient` + `streamable_http_client` | **完全相同** |
| 传输协议 | Streamable HTTP | Streamable HTTP / stdio |
| 附加能力 | OAuth 凭证注入、Lambda 路由、集中化注册 | 无 |
| 工具发现 | `tools/list`（MCP 标准） | **完全相同** |
| 适用场景 | AWS 托管工具服务 | 第三方/本地 MCP Server |

Gateway 使用 Streamable HTTP 传输——每次 MCP 工具调用是独立的 HTTP POST，无持久连接。因此 Gateway 模式对 per-request 和 per-session 都完全透明，**不影响 Agent 生命周期选择**。

### 7.3 模式 B — 直接 HTTP（无状态）

```yaml
# agent_config.yaml
agent:
  tools:
    - "http_request"
    - "search_documents"
    - "current_time"
```

本地 `@tool` 函数通过 HTTP 调用后端 API，每次都是独立请求，无持久连接。包装层可在请求时注入 auth headers：

```python
# http_request_wrapper.py — 每次调用独立，无连接池
@tool
def http_request(url: str, method: str = "GET", **kwargs) -> str:
    """Call backend API with auto-injected auth headers."""
    headers = kwargs.get("headers", {})
    if "/api/svc/" in url:
        headers["X-Service-Api-Key"] = _service_api_key  # 自动注入
    response = requests.request(method, url, headers=headers, **kwargs)
    return response.text
```

同样对 Agent 生命周期选择**无影响**。

### 7.4 MCPClient 生命周期管理

MCPClient 维护与 MCP Server 的会话（无论连接的是 Gateway 还是第三方 Server），需要关注其生命周期管理。

> 注：MCPClient 的"持久"程度取决于传输协议——stdio 是真正的持久连接（子进程常驻），Streamable HTTP 则是按需 POST 请求。详见 [Section 7.6](#76-mcp-连接资源占用分析)。

以 stdio 传输为例，Strands `MCPClient` 的基本用法：

```python
from strands.tools.mcp import MCPClient
from mcp import stdio_client, StdioServerParameters

# MCPClient 管理一个持久连接
mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["some-mcp-server"])
))

# 传给 Agent，Agent 首次使用时自动连接
agent = Agent(tools=[mcp_client])
agent("第一轮")   # 连接建立，工具发现
agent("第二轮")   # 复用同一连接
```

**MCPClient 的生命周期特点：**

| 特点 | 说明 |
|------|------|
| 连接建立 | 懒加载——首次工具调用时建立 |
| 工具发现 | 连接后自动调 `list_tools` 发现可用工具 |
| 连接复用 | 同一 MCPClient 实例的多次调用复用同一连接 |
| 清理要求 | 需要显式关闭。脚本中可用 `with` 语句；长驻进程中应在 `shutdown` 事件中调用 `close()` |
| 传输方式 | stdio（本地子进程）、HTTP Streamable、SSE |

**关键认识：MCPClient 和 Agent 是独立的对象。** MCPClient 管理的是与 MCP Server 的连接，Agent 管理的是对话状态。两者的生命周期可以独立控制。

#### MCPClient 始终在 startup 建立

无论 Agent 采用哪种生命周期，MCPClient 都应在 startup 创建一次，在进程级别复用。以下三种组合按 Agent 生命周期区分：

**组合 A：startup MCP + per-request Agent（常见模式）**

```python
# [FastAPI 自建] 组合 A
_mcp_client: MCPClient | None = None

@app.on_event("startup")
async def startup():
    global _mcp_client
    _mcp_client = MCPClient(lambda: stdio_client(...))
    # 连接懒加载，首次工具调用时自动建立

@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    session_manager = _create_session_manager(request.id, request.user_id)
    agent = Agent(
        tools=[*all_tools, _mcp_client],   # 每次新建 Agent，但复用 MCP 连接
        session_manager=session_manager,
    )
    async for event in agent.stream_async(user_message):
        yield sse_event(event)
    # Agent 丢弃，但 _mcp_client 连接保持

@app.on_event("shutdown")
async def shutdown():
    if _mcp_client:
        await _mcp_client.close()
```

Agent 每次请求新建（通过 session_manager 恢复历史），MCPClient 在进程级别复用。实现简单，但每次请求都有 session_manager 反序列化开销。

**组合 B：startup MCP + per-session Agent（充分利用 MicroVM 亲和性）**

```python
# [FastAPI 自建] 组合 B
_mcp_client: MCPClient | None = None
_agent_instance: Agent | None = None

@app.on_event("startup")
async def startup():
    global _mcp_client
    _mcp_client = MCPClient(lambda: stdio_client(...))

@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    global _agent_instance
    if _agent_instance is None:
        session_manager = _create_session_manager(request.id, request.user_id)
        _agent_instance = Agent(
            tools=[*all_tools, _mcp_client],
            session_manager=session_manager,
        )
    async for event in _agent_instance.stream_async(user_message):
        yield sse_event(event)
    # Agent 和 MCP 连接都保持

@app.on_event("shutdown")
async def shutdown():
    if _mcp_client:
        await _mcp_client.close()
```

Agent 和 MCPClient 都在 MicroVM 生命周期内持久。第 2 轮起省去 session_manager 反序列化开销，对话历史直接在 Agent 内存中累积。

#### 小结

| 组合 | MCPClient | Agent | MCP 开销 | Session 恢复开销 |
|------|-----------|-------|---------|----------------|
| MCP per-request（反模式） | 每次新建 | 每次新建 | **每次请求** | 每次请求 |
| **A: startup MCP + per-request Agent** | startup 一次 | 每次新建 | 一次 | **每次请求** |
| **B: startup MCP + per-session Agent** | startup 一次 | 首次创建 | 一次 | **首次请求** |

> **反模式说明**：MCP per-request 即在每个请求的 handler 中新建 MCPClient（例如在 handler 内使用 `with MCPClient(...):` 块），每次请求都要重新建立连接 + 工具发现，开销极大。在 AgentCore 长驻进程中应避免。

三种组合的 MCP 开销都取决于 MCPClient 生命周期（应为 startup 一次），差异仅在 Agent 层面：per-session 省去了后续请求的 session_manager 反序列化。

### 7.5 MCP 连接生命周期与 MicroVM 的映射

MCPClient 应在 startup 创建，生命周期与**进程**（而非 Agent）绑定。以下以 stdio 传输为例（Streamable HTTP 无持久连接，但 MCPClient 实例同样在进程级别复用）：

```
MicroVM 生命周期（最长 8 小时）— 以 stdio 传输为例
│
├── 容器启动 (startup)
│     └── MCPClient 创建（懒加载，连接未建立）
│
├── 请求 1
│     ├── 创建 Agent（per-request 或 per-session）
│     ├── Agent 首次调用 MCP 工具 → MCPClient 建立连接 + 发现工具
│     └── 工具调用通过持久连接执行
│
├── 请求 2（同一 Session）
│     ├── 创建新 Agent（per-request）或复用（per-session）
│     └── 复用已有 MCPClient 连接（零 MCP 开销）
│
├── 请求 N ...
│     └── MCPClient 连接始终复用，与 Agent 是否重建无关
│
├── 空闲（最长 15 分钟）
│     └── MCPClient 连接保持（MicroVM 仍在运行）
│
└── MicroVM 终止 (shutdown)
      └── MCPClient.close()，进程销毁
```

**关键点：** MCPClient 在进程级别复用，Agent 在请求级别或 Session 级别——两者解耦。Per-request Agent 并不意味着 per-request MCP 连接。

### 7.6 MCP 连接资源占用分析

MCPClient 在 MicroVM 生命周期内保持连接（最长 8 小时），这会不会浪费资源？答案取决于 MCP 传输协议。

#### stdio 传输（Strands MCPClient 默认）

Client 启动 MCP Server 作为**子进程**，通过 stdin/stdout 通信。子进程在整个 MicroVM 生命周期内常驻：

```
MCPClient ←stdin/stdout→ MCP Server 子进程（常驻）
```

- 子进程从创建到 MicroVM 销毁一直存活（最长 8 小时）
- MCP 规范**没有定义空闲超时或心跳机制**——空闲期间进程静默等待
- 关闭方式：Client 关闭 stdin → SIGTERM → 等待 → SIGKILL
- 空闲时资源占用：内存（通常 10-50MB）、进程槽；CPU 接近零

**关键点：stdio 模式下 MCP Server 是本地子进程，不存在"占用远程服务器连接"的问题。** 资源消耗局限在 MicroVM 内部，而 MicroVM 本身就是为单 Session 分配的专用资源，8 小时空闲最终由 MicroVM 终止兜底。

#### Streamable HTTP 传输

每条消息是独立的 HTTP POST 请求，连接**不持久**：

```
MCPClient ──POST──→ MCP Server（远程）
          ←200/SSE──
          （连接关闭）
```

- Server 通过 `Mcp-Session-Id` header 维护逻辑会话状态
- 请求之间没有持久 TCP 连接（除非 SSE 流进行中）
- Client 可发 HTTP `DELETE` 显式终止会话
- 空闲时**零连接资源**，Server 端仅需维护 session 元数据

#### 两种传输协议的资源对比

| 维度 | stdio（本地子进程） | Streamable HTTP（远程） |
|------|---------------------|------------------------|
| 空闲时连接 | 进程常驻，stdin/stdout 打开 | 无持久连接 |
| 内存占用 | 子进程常驻（~10-50MB） | 仅 session 元数据 |
| MicroVM 8h 场景 | 子进程活 8h，空闲时低开销 | 按需连接，无空闲开销 |
| 远程 Server 压力 | N/A（本地进程） | 低（无持久连接） |
| 清理机制 | MicroVM 销毁时子进程自动终止 | Client 发 DELETE 或 Server 超时清理 |

#### 结论

- **stdio**：资源开销可接受。MCP Server 子进程与 MicroVM 同生共死，空闲时仅占少量内存，不影响外部系统。
- **Streamable HTTP**：更轻量。无持久连接，适合连接远程 MCP Server 的场景。
- **实际建议**：如果 MCP Server 是本地工具进程（如文件操作、代码执行），用 stdio 即可；如果连接第三方远程 MCP Server 且关注连接数，优先选择 Streamable HTTP 传输。

### 7.7 两种模式对比

| 维度 | MCP 协议 (A) | 自定义 HTTP (B) |
|------|-------------|----------------|
| 通信协议 | MCP（JSON-RPC 2.0） | 自定义 HTTP |
| Agent 侧 | Strands `MCPClient` | 本地 Python `@tool` 函数 |
| 工具发现 | MCP `tools/list`（自动发现） | Skill MD 或代码中定义 |
| 传输方式 | Streamable HTTP（远程）/ stdio（本地） | 独立 HTTP 请求 |
| 连接开销 | 首次连接 + 工具发现（startup 一次） | 无 |
| 推荐生命周期 | startup 建立 MCPClient，进程级复用 | 无要求 |

**MCP 模式的 Server 端变体：**

| Server 端 | 附加能力 | 适用场景 |
|-----------|---------|---------|
| **AgentCore Gateway** | OAuth 凭证注入、Lambda 路由、集中化注册 | AWS 托管工具服务 |
| **第三方 MCP Server** | 无 | Tavily、Sentry 等第三方工具 |
| **本地 MCP Server**（stdio） | 无 | 文件操作、代码执行等本地工具 |

### 7.8 工具注册表

无论哪种模式，本地工具都可以通过注册表字典将名称映射到实现，实现配置驱动的工具加载：

```python
TOOL_REGISTRY = {
    "current_time": current_time,          # strands_tools 内置
    "http_request": http_request,          # 自定义包装（自动注入 auth headers）
    "search_documents": search_documents,  # 自定义 @tool
}
```

YAML 配置引用工具名，startup 时从注册表解析为实际函数：

```yaml
agent:
  tools:
    - "current_time"
    - "http_request"
    - "search_documents"
```

MCPClient 如果启用，则作为额外的 `tools` 参数传入 Agent，与本地工具共存：

```python
agent = Agent(
    tools=[
        *all_tools,          # 本地注册表工具
        mcp_client_a,        # MCP Server A 的工具
        mcp_client_b,        # MCP Server B 的工具
    ],
    session_manager=session_manager,
)
```

---

## 8. 启动流程 (Startup)

容器启动时的典型初始化步骤：

```
容器启动 (uvicorn main:app)
    │
    ├── 1. Web 框架初始化 + 中间件注册（CORS 等）
    │
    └── @app.on_event("startup")
         │
         ├── 2. 加载配置 (S3 优先 → 本地 fallback)
         │       agent_config.yaml
         │
         ├── 3. 注入运行时变量
         │       环境变量 → system prompt 模板
         │
         ├── 4. 加载密钥 (Secrets Manager / 环境变量)
         │       → 配置工具包装层
         │
         ├── 5. 初始化 MCPClient（如需要）
         │       连接 Gateway 或第三方 MCP Server
         │
         └── 6. 解析工具列表
                 YAML tool names → 注册表查找 → all_tools[]
```

**冷启动优化**：如果 S3 配置加载失败，startup 不会阻塞。初始化可延迟到第一个 `/invocations` 请求时执行。

---

