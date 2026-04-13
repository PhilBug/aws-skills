# AgentCore Runtime 协议详解：HTTP、MCP、A2A、AG-UI

> AgentCore Runtime 支持四种通信协议，每种协议针对不同的交互模式。本文基于 AWS 官方文档，详解每种协议的容器契约、端点规范、请求/响应格式和适用场景。

---

## 1. 协议概览

AgentCore Runtime 在创建时通过 `protocolConfiguration` 指定协议，**一个 Runtime 只能选一种协议**：

| 协议 | 定位 | 端口 | 主端点 | 通信模式 | 创建者 |
|------|------|------|--------|----------|--------|
| **HTTP** | 通用 Agent 交互 | 8080 | `POST /invocations` | JSON / SSE / WebSocket | — |
| **MCP** | 工具与数据服务 | 8000 | `POST /mcp` | JSON-RPC 2.0 | Anthropic |
| **A2A** | Agent 间协作 | 9000 | `POST /` | JSON-RPC 2.0 | Google |
| **AG-UI** | Agent 到前端 UI | 8080 | `POST /invocations` | SSE 事件流 | CopilotKit 主导 |

四种协议共享相同的 AgentCore 基础设施：MicroVM Session 隔离、OAuth/SigV4 认证、自动扩缩容。区别在于**容器内部的通信契约**。

> **所有协议都需要实现 `GET /ping` 健康检查端点**（返回 `{"status": "Healthy"}` 或 `{"status": "HealthyBusy"}`）。这是 AgentCore Runtime 的通用要求，不属于任何特定协议的规范。

```
调用方 → AgentCore 平台（认证 + 路由） → MicroVM 内的容器
                                           │
                                           ├── HTTP:  POST :8080/invocations
                                           ├── MCP:   POST :8000/mcp
                                           ├── A2A:   POST :9000/
                                           └── AG-UI: POST :8080/invocations
```

> 无论选择哪种协议，外部调用方使用的 AgentCore 平台端点**始终相同**：
> ```
> POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urlEncodedArn}/invocations?qualifier=DEFAULT
> ```
> AgentCore 平台层接收请求后，根据 `protocolConfiguration` 将 payload 透传到容器的对应端点。

---

## 2. HTTP 协议

**定位**：最通用的协议，适合自定义 Agent 应用。请求/响应格式完全由应用自行定义，AgentCore 只做透传。

### 2.1 容器契约

| 规格 | 要求 |
|------|------|
| 监听地址 | `0.0.0.0:8080` |
| 平台 | ARM64 容器 |
| 必须端点 | `POST /invocations`、`GET /ping` |
| 可选端点 | `GET /ws`（WebSocket） |

### 2.2 端点详解

#### `POST /invocations` — 主交互端点

AgentCore 的 `InvokeAgentRuntime` API 将调用方的 payload 作为字节流**原样透传**到此端点。请求体格式由应用自行定义。

**响应支持两种模式：**

**模式 A：JSON（非流式）**
```
Content-Type: application/json

{"response": "查询结果...", "status": "success"}
```
适用：简单问答、确定性计算、状态查询。

**模式 B：SSE（流式）**
```
Content-Type: text/event-stream

data: {"event": "partial response 1"}
data: {"event": "partial response 2"}
data: {"event": "final response"}
```
适用：实时对话、渐进式内容生成、长时间运行的操作。

#### `GET /ws` — WebSocket（可选）

与 `/invocations` 共用 8080 端口。通过标准 HTTP Upgrade 建立 WebSocket 连接：

```
GET /ws HTTP/1.1
Connection: Upgrade
Upgrade: websocket
Sec-WebSocket-Version: 13
X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: session-uuid
```

支持文本（JSON / 纯文本）和二进制消息。适用于需要双向实时通信的场景。

#### `GET /ping` — 健康检查

```json
{"status": "Healthy", "time_of_last_update": 1640995200}
```

| status 值 | 含义 |
|-----------|------|
| `Healthy` | 就绪，可接受新请求 |
| `HealthyBusy` | 运行中但有后台异步任务（Session 保持活跃，不会因空闲超时被回收） |

### 2.3 适用场景

- 自定义 Agent 应用（本项目采用此协议）
- 需要完全控制请求/响应格式
- 使用 Strands Agents、LangGraph 等框架构建的 Agent
- 需要 WebSocket 双向通信

---

## 3. MCP 协议

**定位**：将 Agent 的工具和数据能力以标准化方式暴露出去。MCP（Model Context Protocol）由 Anthropic 创建，是连接 AI 应用与外部系统（工具、数据源、工作流）的开放标准。

### 3.1 MCP 协议核心概念

MCP 采用 **Client-Server 架构**：

```
MCP Host（AI 应用，如 Claude Code）
  ├── MCP Client 1 ──→ MCP Server A（文件系统）
  ├── MCP Client 2 ──→ MCP Server B（数据库）
  └── MCP Client 3 ──→ MCP Server C（Sentry）
```

MCP Server 暴露三种核心原语（Primitives）：

| 原语 | 用途 | 发现方法 |
|------|------|----------|
| **Tools** | 可执行的函数（文件操作、API 调用、数据库查询） | `tools/list` → `tools/call` |
| **Resources** | 上下文数据源（文件内容、数据库记录、API 响应） | `resources/list` → `resources/read` |
| **Prompts** | 交互模板（system prompt、few-shot 示例） | `prompts/list` → `prompts/get` |

> **注**：三种原语是 MCP 协议标准定义。在 AgentCore 托管场景中，**Tools** 是最常用的原语；Resources 和 Prompts 的支持程度取决于具体的 MCP Server 实现。

通信基于 **JSON-RPC 2.0**，传输层支持两种机制：
- **Stdio**：本地进程间通信（标准输入/输出）
- **Streamable HTTP**：远程通信，HTTP POST + 可选 SSE 流

### 3.2 AgentCore 中的 MCP 容器契约

| 规格 | 要求 |
|------|------|
| 监听地址 | `0.0.0.0:8000` |
| 平台 | ARM64 容器 |
| 主端点 | `POST /mcp` |
| 健康检查 | `GET /ping` |
| 传输方式 | Streamable HTTP |
| 默认模式 | 无状态（`stateless_http=True`） |

> **注意端口差异**：MCP 用 **8000**，HTTP/AG-UI 用 8080，A2A 用 9000。

### 3.3 端点详解

#### `POST /mcp` — MCP RPC 消息处理

AgentCore 将 `InvokeAgentRuntime` 的 payload 作为标准 MCP JSON-RPC 消息**直接透传**到此端点。

**请求示例（工具发现）：**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list"
}
```

**响应示例：**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "add_numbers",
        "description": "Add two numbers together",
        "inputSchema": {
          "type": "object",
          "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"}
          },
          "required": ["a", "b"]
        }
      }
    ]
  }
}
```

**工具调用：**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "add_numbers",
    "arguments": {"a": 3, "b": 5}
  }
}
```

响应 Content-Type 支持 `application/json` 或 `text/event-stream`。

### 3.4 Session 管理

- AgentCore 平台自动添加 `Mcp-Session-Id` header 实现 Session 隔离
- 默认无状态模式：Server 不应拒绝平台生成的 Session ID
- 有状态模式（`stateless_http=False`）：支持 elicitation（多轮交互）和 sampling（请求 LLM 生成内容）

### 3.5 代码示例

```python
# my_mcp_server.py
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together"""
    return a + b

@mcp.tool()
def greet_user(name: str) -> str:
    """Greet a user by name"""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

**部署：**
```bash
agentcore configure -e my_mcp_server.py --protocol MCP
agentcore deploy
```

**远程调用（Python MCP Client）：**
```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')
mcp_url = f"https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
headers = {
    "authorization": f"Bearer {bearer_token}",
    "Content-Type": "application/json"
}

async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (
    read_stream, write_stream, _
):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(tools)
```

也可以用 **MCP Inspector**（`npx @modelcontextprotocol/inspector`）可视化测试。

### 3.6 与 AgentCore Gateway 的关系

AgentCore Gateway 可以将 MCP Server 注册为工具，为 Agent 提供透明的 OAuth 凭证注入：

```
Agent → AgentCore Gateway → MCP Server（AgentCore Runtime 托管）
                │
                ├── Inbound: 验证 Agent 身份（JWT）
                ├── 工具路由: MCP tool_name → 目标 MCP Server
                └── Outbound: 从 Token Vault 获取 OAuth token → 注入到请求
```

### 3.7 适用场景

- 将工具和数据能力标准化暴露给 AI 应用
- 需要被多个不同的 AI 客户端（Claude、ChatGPT、VS Code 等）调用
- 通过 AgentCore Gateway 提供带 OAuth 凭证注入的工具服务
- 已有 MCP Server，希望托管到 AgentCore 获得 Session 隔离和自动扩缩容

---

## 4. A2A 协议

**定位**：Agent 间协作通信。A2A（Agent-to-Agent）由 Google 创建并捐赠给 Linux Foundation，是让不同框架、不同组织的 Agent 以**对等方式协作**的开放标准。

### 4.1 A2A 协议核心概念

**Agent 不透明性（Agent Opacity）**：A2A 的核心设计原则是 Agent 之间**不需要共享内部记忆、私有逻辑或工具实现**。协作通过消息传递完成，每个 Agent 保持黑盒。

```
Agent A（理赔处理）              Agent B（保单校验）
  │                                │
  │  不知道 B 的内部实现              │  不知道 A 的内部实现
  │  只通过 A2A 消息交互              │  只通过 A2A 消息交互
  │                                │
  └──── JSON-RPC 2.0 / HTTP ───────┘
```

**通信方式：**
- JSON-RPC 2.0 over HTTP(S)
- 支持同步请求/响应、SSE 流式、异步推送通知
- 数据类型：文本、文件、结构化 JSON

**Agent Card（Agent 名片）**：每个 A2A Agent 在 `/.well-known/agent-card.json` 暴露自身元数据，用于**动态发现**：

```json
{
  "name": "Calculator Agent",
  "description": "A calculator agent for arithmetic operations",
  "version": "1.0.0",
  "url": "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{arn}/invocations/",
  "protocolVersion": "0.3.0",
  "preferredTransport": "JSONRPC",
  "capabilities": {"streaming": true},
  "defaultInputModes": ["text"],
  "defaultOutputModes": ["text"],
  "skills": [
    {
      "id": "arithmetic",
      "name": "Arithmetic",
      "description": "Basic arithmetic operations"
    }
  ]
}
```

### 4.2 AgentCore 中的 A2A 容器契约

| 规格 | 要求 |
|------|------|
| 监听地址 | `0.0.0.0:9000` |
| 平台 | ARM64 容器 |
| 主端点 | `POST /`（根路径） |
| Agent Card | `GET /.well-known/agent-card.json` |
| 健康检查 | `GET /ping` |

> **注意**：A2A 的主端点是根路径 `/`，不是 `/invocations`。端口是 **9000**。

### 4.3 端点详解

#### `POST /` — JSON-RPC 2.0 消息处理

AgentCore 将 `InvokeAgentRuntime` 的 payload 作为 JSON-RPC 消息**直接透传**。

**请求示例（发送消息）：**
```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "what is 101 * 11?"}],
      "messageId": "12345678-1234-1234-1234-123456789012"
    }
  }
}
```

**响应示例：**
```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "result": {
    "artifacts": [
      {
        "artifactId": "unique-artifact-id",
        "name": "agent_response",
        "parts": [{"kind": "text", "text": "101 * 11 = 1111"}]
      }
    ]
  }
}
```

#### `GET /.well-known/agent-card.json` — Agent 发现

返回 Agent Card JSON，描述 Agent 的身份、能力、技能和认证要求。调用方通过此端点了解 Agent 能做什么、如何交互。

### 4.4 错误处理

A2A 错误以 **JSON-RPC 2.0 error 响应**返回，**HTTP 状态码始终为 200**（协议合规性要求）：

| JSON-RPC Code | 含义 | 对应 HTTP 语义 |
|---------------|------|---------------|
| -32501 | 资源不存在 | 404 |
| -32052 | 请求校验失败 | 400 |
| -32053 | 请求频率超限 | 429 |
| -32054 | 资源冲突 | 409 |
| -32055 | 运行时客户端错误 | 424 |

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "error": {"code": -32052, "message": "Validation error - Invalid request data"}
}
```

### 4.5 代码示例

```python
# my_a2a_server.py
import os, logging, uvicorn
from strands import Agent
from strands.multiagent.a2a import A2AServer
from strands_tools.calculator import calculator
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)

runtime_url = os.environ.get('AGENTCORE_RUNTIME_URL', 'http://127.0.0.1:9000/')

strands_agent = Agent(
    name="Calculator Agent",
    description="A calculator agent that can perform basic arithmetic operations.",
    tools=[calculator],
    callback_handler=None
)

a2a_server = A2AServer(
    agent=strands_agent,
    http_url=runtime_url,
    serve_at_root=True  # AgentCore A2A 契约要求主端点在 / 根路径
)

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "healthy"}

app.mount("/", a2a_server.to_fastapi_app())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
```

**部署：**
```bash
pip install strands-agents[a2a] bedrock-agentcore strands-agents-tools
agentcore configure -e my_a2a_server.py --protocol A2A
agentcore deploy
```

**远程获取 Agent Card：**
```bash
ESCAPED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$AGENT_ARN', safe=''))")

curl "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations/.well-known/agent-card.json" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $(uuidgen)"
```

### 4.6 适用场景

- 多 Agent 协作系统（不同 Agent 各司其职，通过消息协作）
- 跨框架 Agent 互操作（Strands Agent 与 LangGraph Agent 协作）
- 跨组织 Agent 通信（Agent Card 实现动态发现）
- 需要 Agent 保持黑盒、不暴露内部实现

---

## 5. AG-UI 协议

**定位**：Agent 到前端 UI 的标准化通信。AG-UI（Agent-User Interaction Protocol）由 CopilotKit 团队主导开发，与 LangGraph、CrewAI、AWS Strands、Google ADK、Microsoft Agent Framework、Pydantic AI 等多个框架团队协作，定义了 Agent 后端与前端应用之间的**事件流协议**。

### 5.1 AG-UI 解决什么问题

传统 request/response 架构对 Agent 应用力不从心，因为 Agent 具有以下特性：
- **长时间运行**：多轮对话中持续流式输出中间结果
- **非确定性**：Agent 行为不可预测，需要动态控制 UI
- **混合 I/O**：同时产生结构化数据（工具调用、状态）和非结构化内容（文本、语音）
- **人机交互**：需要暂停/审批/编辑等 human-in-the-loop 能力

AG-UI 通过定义 **16+ 种事件类型**，让前端能精确感知 Agent 的每一步行为。

### 5.2 AgentCore 中的 AG-UI 容器契约

| 规格 | 要求 |
|------|------|
| 监听地址 | `0.0.0.0:8080` |
| 平台 | ARM64 容器 |
| 主端点 | `POST /invocations`（SSE 事件流） |
| 可选端点 | `GET /ws`（WebSocket） |
| 健康检查 | `GET /ping` |

> AG-UI 与 HTTP 协议使用**相同的端口（8080）和端点路径（`/invocations`）**，区别在于请求/响应格式遵循 AG-UI 事件规范。

### 5.3 端点详解

#### `POST /invocations` — AG-UI 事件流

**请求格式（`RunAgentInput`）：**
```json
{
  "threadId": "thread-123",
  "runId": "run-456",
  "messages": [{"id": "msg-1", "role": "user", "content": "Hello, agent!"}],
  "tools": [],
  "context": [],
  "state": {},
  "forwardedProps": {}
}
```

| 字段 | 含义 |
|------|------|
| `threadId` | 对话线程 ID |
| `runId` | 本次执行的唯一 ID |
| `messages` | 消息历史 |
| `tools` | 前端注册的工具（前端 tool call 场景） |
| `context` | 上下文信息 |
| `state` | 共享状态（支持前后端状态同步） |
| `forwardedProps` | 透传属性 |

**响应格式（SSE 事件流）：**
```
Content-Type: text/event-stream

data: {"type":"RUN_STARTED","threadId":"thread-123","runId":"run-456"}
data: {"type":"TEXT_MESSAGE_START","messageId":"msg-789","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-789","delta":"Processing your request"}
data: {"type":"TOOL_CALL_START","toolCallId":"tool-001","toolCallName":"search","parentMessageId":"msg-789"}
data: {"type":"TOOL_CALL_RESULT","messageId":"msg-789","toolCallId":"tool-001","content":"Search completed"}
data: {"type":"TEXT_MESSAGE_END","messageId":"msg-789"}
data: {"type":"RUN_FINISHED","threadId":"thread-123","runId":"run-456"}
```

### 5.4 AG-UI 事件类型

| 事件类型 | 含义 |
|----------|------|
| `RUN_STARTED` | Agent 执行开始 |
| `TEXT_MESSAGE_START` | 文本消息流开始 |
| `TEXT_MESSAGE_CONTENT` | 文本增量内容（delta） |
| `TEXT_MESSAGE_END` | 文本消息流结束 |
| `TOOL_CALL_START` | 工具调用开始 |
| `TOOL_CALL_RESULT` | 工具执行结果 |
| `RUN_FINISHED` | Agent 执行完成 |
| `RUN_ERROR` | 执行出错 |

错误以 SSE 事件返回（HTTP 状态码仍为 200）：
```
data: {"type":"RUN_ERROR","code":"AGENT_ERROR","message":"Agent execution failed"}
```

### 5.5 代码示例

```python
# my_agui_server.py
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
# 注意：AG-UI 生态尚在快速演进，以下包名和 API 可能随版本更新变化，
# 请以 ag-ui.com 官方文档和 PyPI 最新版本为准。
from ag_ui_strands import StrandsAgent
from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from strands import Agent
from strands.models.bedrock import BedrockModel

model = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-west-2",
)

strands_agent = Agent(
    model=model,
    system_prompt="You are a helpful assistant.",
)

agui_agent = StrandsAgent(
    agent=strands_agent,
    name="my_agent",
    description="A helpful assistant",
)

app = FastAPI()

@app.post("/invocations")
async def invocations(input_data: dict, request: Request):
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    async def event_generator():
        run_input = RunAgentInput(**input_data)
        async for event in agui_agent.run(run_input):
            yield encoder.encode(event)

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type()
    )

@app.get("/ping")
async def ping():
    return JSONResponse({"status": "Healthy"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**部署：**
```bash
pip install fastapi uvicorn ag-ui-strands
agentcore configure -e my_agui_server.py --protocol AGUI
agentcore deploy
```

### 5.6 与 CopilotKit 集成

AG-UI 天然支持 [CopilotKit](https://docs.copilotkit.ai/)，前端可以使用 CopilotKit 的 React 组件直接对接 AG-UI 后端：

```
React 前端（CopilotKit 组件）
  ↕ AG-UI 事件流
AgentCore Runtime（AG-UI 协议）
  ↕ LLM 调用
Bedrock / OpenAI / ...
```

AG-UI 支持的高级 UI 能力包括：streaming chat、generative UI、共享状态同步、thinking steps 可视化、human-in-the-loop 中断、sub-agent 组合等。

### 5.7 适用场景

- 构建丰富的 Agent UI 应用（聊天、协作编辑、审批流程）
- 需要前端精确感知 Agent 的每一步行为（工具调用、思考过程）
- 使用 CopilotKit 等前端框架
- 需要 human-in-the-loop 交互（暂停、审批、编辑）
- 需要前后端状态同步

---

## 6. 四种协议对比

| 维度 | HTTP | MCP | A2A | AG-UI |
|------|------|-----|-----|-------|
| **容器端口** | 8080 | 8000 | 9000 | 8080 |
| **主端点** | `/invocations` | `/mcp` | `/` | `/invocations` |
| **消息格式** | 应用自定义 | JSON-RPC 2.0 | JSON-RPC 2.0 | RunAgentInput + SSE 事件 |
| **通信模式** | JSON / SSE / WebSocket | JSON-RPC（Streamable HTTP） | JSON-RPC / SSE / 异步推送 | SSE 事件流 / WebSocket |
| **发现机制** | 无 | `tools/list`（MCP 原语） | Agent Card（`/.well-known/agent-card.json`） | 无 |
| **交互对象** | 人 → Agent | AI 应用 → 工具/数据 | Agent → Agent | Agent → 前端 UI |
| **请求体格式** | 完全自定义 | 标准 MCP RPC | 标准 A2A RPC | AG-UI RunAgentInput |
| **事件类型** | 自定义 | MCP 标准 | A2A 标准 | 16+ 种 AG-UI 事件 |
| **客户端/框架** | 任意 | MCP 客户端：Claude、ChatGPT、VS Code、Cursor 等 | Agent 框架：Strands、LangGraph、CrewAI 等 | 前端框架：CopilotKit；Agent 框架：Strands、LangGraph、CrewAI 等 |
| **协议来源** | — | Anthropic | Google（Linux Foundation） | CopilotKit + LangGraph + CrewAI |

### 三种协议的互补关系

AG-UI 官方文档明确定义了三种协议的互补定位：

```
                    MCP
              Agent ↔ 工具/数据
                 │
    ┌────────────┼────────────┐
    │            │            │
   A2A         Agent        AG-UI
Agent ↔ Agent    │     Agent ↔ 前端 UI
                 │
              用户/系统
```

- **MCP**：Agent 获取工具和数据的**向下**连接
- **A2A**：Agent 与 Agent 之间的**水平**协作
- **AG-UI**：Agent 与前端 UI 的**向上**交互

三者可以在同一个系统中并存：一个 Agent 通过 MCP 访问工具，通过 A2A 与其他 Agent 协作，通过 AG-UI 与用户前端交互。

---

## 7. 如何选择协议

```
你要构建什么？
  │
  ├── 自定义 Agent 应用，需要完全控制格式
  │   └── HTTP
  │
  ├── 工具/数据服务，要被多种 AI 客户端调用
  │   └── MCP
  │
  ├── 多 Agent 协作系统
  │   └── A2A
  │
  └── Agent 驱动的前端 UI 应用
      └── AG-UI
```

**实际考量：**

| 场景 | 推荐协议 | 理由 |
|------|----------|------|
| Strands/LangGraph Agent + 自定义前端 | HTTP | 最灵活，格式自由 |
| 已有 MCP Server，需要云端托管 | MCP | 原生兼容 |
| 构建工具服务给 Claude/ChatGPT 用 | MCP | 标准协议，广泛支持 |
| 多个专业 Agent 各司其职、协作完成复杂任务 | A2A | Agent 间标准通信 |
| 使用 CopilotKit 构建 AI 协作应用 | AG-UI | 原生事件协议 |
| 简单聊天机器人 | HTTP | 最简单直接 |

> **典型选择**：如果你的 Agent 直接面向前端用户、需要完全自定义请求/响应格式、使用 Strands Agents 或 LangGraph 等框架 + FastAPI，HTTP 是最自然的选择。

---

## 8. 认证：所有协议通用

无论选择哪种协议，AgentCore 都支持相同的认证方式：

**OAuth 2.0 Bearer Token：**
```
Authorization: Bearer <jwt-token>
X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session-id>
```

未认证请求返回 401：
```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{ESCAPED_ARN}/invocations/.well-known/oauth-protected-resource?qualifier={QUALIFIER}"
```

客户端可通过 `resource_metadata` URL 发现 OAuth 端点，实现自动 token 获取。

**SigV4 签名认证：**
- 返回 HTTP 403（不是 401）
- 不包含 `WWW-Authenticate` header
- 通过 `boto3.client('bedrock-agentcore').invoke_agent_runtime()` 调用

> **注意**：OAuth 和 SigV4 二选一，不能混用。启用 OAuth 后不能用 boto3 SDK 调用（SigV4），必须直接发 HTTPS 请求携带 Bearer Token。

---

*本文基于 AWS 官方文档（AgentCore Runtime Protocol Contracts）、MCP 官方文档（modelcontextprotocol.io）、A2A 官方文档（Google/A2A）和 AG-UI 官方文档（ag-ui.com）整理。协议版本：MCP 2025-06-18、A2A 0.3.0。*
