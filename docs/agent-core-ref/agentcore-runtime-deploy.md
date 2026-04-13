# AgentCore Runtime 部署与运维：CDK、安全、可观测性、框架对比

> 涵盖 CDK 部署（L1/L2）、多 Runtime 架构、运行时数据流、安全模型（OAuth/IAM）、可观测性（OTel/CloudWatch）、BedrockAgentCoreApp vs FastAPI 对比。
>
> 姊妹篇：[核心机制](./agentcore-runtime-core.md)（容器契约、Session 模型、Agent 生命周期、工具集成、启动流程）

---

## 1. CDK 部署

> **生产环境统一使用 CDK 部署。** AgentCore Runtime 依赖 Cognito、ECR、IAM Role、Secrets Manager 等周边资源，这些都需要通过 CDK 统一编排。AgentCore 提供以下部署路径：
> - **CDK L2 Construct**（推荐）：`@aws-cdk/aws-bedrock-agentcore-alpha`，支持四种 artifact 来源——`fromAsset`（指向本地目录，CDK 自动构建镜像）、`fromEcrRepository`（已有 ECR 镜像）、`fromS3`（zip 包，无需 Docker）、`fromImageUri`（预构建镜像 URI）。
> - **CDK L1 + 自建容器**：自行编写 Dockerfile、推送 ECR、通过 `CfnRuntime` 部署，适合需要完全控制构建流程或集成到现有 IaC 管线的场景。
> - **Starter Toolkit CLI**：`agentcore configure` + `agentcore deploy`，默认 Direct Code Deploy，无需 Dockerfile。**仅适合 demo 和原型验证**，生产项目无法只部署一个 Runtime 而不管周边资源。

### 1.1 CfnRuntime 定义

AgentCore Runtime 使用 L1 CDK 构造 `CfnRuntime`：

```typescript
const cfnRuntime = new agentcore_cfn.CfnRuntime(this, 'Runtime', {
  // 运行时名称
  agentRuntimeName: 'my-agent',

  // 容器镜像（ECR ARM64）
  agentRuntimeArtifact: {
    containerConfiguration: {
      containerUri: ecrImage.imageUri,
    },
  },

  // 网络：PUBLIC 模式
  networkConfiguration: { networkMode: 'PUBLIC' },

  // 协议：HTTP
  protocolConfiguration: 'HTTP',

  // IAM 角色
  roleArn: agentCoreRole.roleArn,

  // 认证：Cognito JWT
  authorizerConfiguration: {
    customJwtAuthorizer: {
      discoveryUrl: `${cognitoIssuer}/.well-known/openid-configuration`,
      allowedClients: [cognitoClientId],
    },
  },

  // 环境变量（按需自定义）
  environmentVariables: {
    MY_API_URL: apiUrl,                        // 应用自定义
    AGENTCORE_MEMORY_ID: memoryId,             // AgentCore Memory 集成
    AGENT_OBSERVABILITY_ENABLED: 'true',       // AgentCore Observability
    // ...
  },
});
```

### 1.2 CDK L2 Construct（推荐）

`@aws-cdk/aws-bedrock-agentcore-alpha` 提供了更简洁的 L2 API，支持四种 artifact 来源：

```typescript
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as path from 'path';

// 方式 A：fromAsset — 指向本地目录，CDK 自动构建 Docker 镜像并推送 ECR
const runtime = new agentcore.Runtime(this, 'MyAgent', {
  runtimeName: 'my-agent',
  agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromAsset(
    path.join(__dirname, '../agent')   // 目录下需有 Dockerfile
  ),
});

// 方式 B：fromS3 — zip 包部署，无需 Docker
const runtime = new agentcore.Runtime(this, 'MyAgent', {
  runtimeName: 'my-agent',
  agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
    { bucketName: 'my-code-bucket', objectKey: 'agent.zip' },
    agentcore.AgentCoreRuntime.PYTHON_3_12,
    ['opentelemetry-instrument', 'main.py'],  // 启动命令
  ),
});

// 方式 C：fromEcrRepository — CI/CD 管线预构建镜像
const runtime = new agentcore.Runtime(this, 'MyAgent', {
  runtimeName: 'my-agent',
  agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
    repository, 'v1.0.0'
  ),
});

// 通用配置（适用于所有方式）
runtime.addEndpoint('production', { version: '1' });  // 固定版本端点
model.grantInvoke(runtime);                            // 授权调用 Bedrock 模型
runtime.grantInvoke(invokerFunction);                  // 授权 Lambda 调用此 Runtime
```

> L2 Construct 自动处理 IAM 角色创建、版本管理和端点配置。`fromAsset` 适合本地开发（CDK 自动构建推送），`fromS3` 适合无 Docker 环境的快速部署，`fromEcrRepository` 适合 CI/CD 管线。

### 1.3 Docker 镜像构建

```dockerfile
# 多阶段构建，ARM64
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./

# UV_PROJECT_ENVIRONMENT 指定 venv 位置
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv && \
    uv sync --frozen --no-dev --no-cache && \
    test -f /app/.venv/bin/uvicorn || (echo "ERROR: uvicorn not found!" && exit 1)

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# 安全补丁
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv ./.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/.venv/lib/python3.12/site-packages

COPY . ./

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore
EXPOSE 8080
CMD python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
```

关键点：
- **非 root 用户**：`bedrock_agentcore` (uid=1000)，AgentCore 安全要求
- **ARM64 平台**：AgentCore Runtime 要求 ARM64 兼容镜像
- **构建验证**：`test -f uvicorn` 确保依赖安装正确，避免部署后才发现问题
- **uv 包管理**：`UV_PROJECT_ENVIRONMENT` 指定 venv 位置，`--frozen` 保证可复现构建

### 1.4 部署架构总览

```
CDK Deploy
    │
    ├── Cognito UserPool
    │     ├── UserPool + Domain
    │     ├── App Client（Authorization Code + PKCE）
    │     └── Discovery URL → AgentCore JWT Authorizer
    │
    ├── ECR Repository
    │     └── Docker Image (ARM64)
    │
    ├── Secrets Manager（可选）
    │     └── API Key / 密钥
    │
    ├── AgentCore CfnRuntime
    │     ├── Container: ECR image
    │     ├── Auth: Cognito JWT Authorizer（discoveryUrl + allowedClients）
    │     ├── Network: PUBLIC / VPC
    │     └── Env: 环境变量
    │
    ├── Backend Server（API Gateway + Lambda / ECS / EC2）
    │     ├── 业务 API（数据查询、写入等）
    │     ├── Auth: API Key / Cognito JWT / IAM
    │     └── Agent 通过 HTTP 工具调用
    │
    ├── (可选) MCP Gateway
    │     ├── Auth: Cognito M2M（client_credentials）
    │     ├── Gateway Lambda
    │     └── Target Lambda
    │
    └── CfnOutput
          ├── backendApiUrl → 前端
          └── agentRuntimeArn → 前端

前端有两条调用链路：

  ┌─────────────────────────────────────────────────────────────┐
  │  前端 (SPA)                                                 │
  │  Cognito 登录 → access_token                                │
  │                                                             │
  │  链路 1: 通用业务逻辑（CRUD、列表、配置等）                   │
  │  ────► API Gateway / Backend Server                         │
  │        Auth: Cognito JWT（API Gateway Authorizer）           │
  │                                                             │
  │  链路 2: Agent 能力（Chat、智能问答等）                       │
  │  ────► AgentCore Runtime 端点（HTTPS 直连）                  │
  │        Auth: Cognito JWT（AgentCore JWT Authorizer）         │
  │        Header: Bearer Token + Session-Id                    │
  │        Response: SSE 流式                                   │
  └─────────────────────────────────────────────────────────────┘

  两条链路共用同一个 Cognito access_token，前端无需管理两套认证。
```

---

## 2. 多 Runtime 架构模式

在复杂系统中，通常会部署多个 AgentCore Runtime，每个 Runtime 专注于不同的职责。常见的分工模式：

| 维度 | 工具密集型 Agent | 知识查询型 Agent | 流程驱动型 Agent（Skills MD） |
|------|-----------------|-----------------|-------------------------------|
| **核心能力** | 大量 Python 工具执行复杂操作 | RAG 检索 + 知识库问答 | Markdown 定义 Skill 驱动流程 |
| **工具定义** | Python `@tool` 函数 | 检索工具 + 少量辅助工具 | Markdown 文件（流程指令 + API 规范） |
| **MCP Gateway** | 需要（多工具路由） | 可选 | 可选 |
| **新增能力** | 写 Python + 部署 | 更新知识库文档 | 添加/修改 .md 文件，无需重新部署 |
| **数据访问** | 直连数据库 / Gateway | Knowledge Base / 向量数据库 | 通过 Backend API（HTTP 工具） |
| **认证模式** | Gateway OAuth | IAM（Bedrock KB） | API Key / 服务间认证 |
| **工具数量** | 多（10+） | 少（2-3） | 少量固定工具 + 动态 Skill |
| **适用场景** | 复杂业务流程（理赔、审批） | FAQ、文档问答、政策咨询 | 可配置化查询/操作流程 |

### 2.1 多 Runtime 之间如何通信

多个 Runtime 之间的协作有以下几种方式：

**方式 A：A2A 协议（Agent-to-Agent）**

AgentCore 原生支持 [A2A 协议](./agentcore-runtime-protocols.md#4-a2a-协议)（Google 创建，Linux Foundation 托管），专为 Agent 间对等协作设计。每个 Agent 保持黑盒，通过 JSON-RPC 2.0 消息交互：

```
Agent A (Runtime 1, HTTP 协议)          Agent B (Runtime 2, A2A 协议)
  │                                       │
  │  1. 通过 MCPClient 或直接 HTTP       │
  │     调用 Agent B 的 A2A 端点         │
  │  ──────── JSON-RPC message/send ────►│
  │                                       │  2. Agent B 处理请求
  │◄──────── JSON-RPC result ─────────── │  3. 返回 artifacts
  │                                       │
  │  Agent Card 发现:                     │
  │  GET /.well-known/agent-card.json     │
```

A2A 的核心优势是 **Agent 不透明性**——Agent 之间不需要共享内部记忆、工具实现或私有逻辑，完全通过消息传递协作。适合跨框架（Strands + LangGraph）、跨组织的 Agent 协作。

**方式 B：共享数据层（间接协作）**

多个 Runtime 通过共享的数据存储（DynamoDB、S3）间接协作，不直接通信：

```
Agent A (Runtime 1)     Agent B (Runtime 2)
  │                       │
  └──► DynamoDB ◄─────────┘
       (共享状态表)
```

适合流水线式协作——Agent A 处理完写入数据库，Agent B 读取后继续处理。

**方式 C：通过编排层协调**

前端或后端 API 充当编排层，按业务逻辑依次调用不同的 Runtime：

```
编排层（Backend API / 前端）
  │
  ├── 1. POST Runtime-A/invocations → Agent A 处理
  │      ← 返回中间结果
  │
  └── 2. POST Runtime-B/invocations → Agent B 处理
         ← 返回最终结果
```

最简单的方式，不需要 Agent 之间直接通信，但编排逻辑在 Agent 外部。

### 2.2 如何选择通信方式

| 场景 | 推荐方式 | 理由 |
|------|---------|------|
| Agent 需要实时对话式协作 | A2A | 标准协议，支持流式和异步 |
| 跨框架/跨组织 Agent 互操作 | A2A | Agent Card 动态发现，框架无关 |
| 流水线式处理（A 完成后 B 继续） | 共享数据层 | 解耦，各 Agent 独立运行 |
| 简单的顺序调用 | 编排层 | 最简单，逻辑集中在编排层 |
| Agent 需要调用另一个 Agent 的工具 | MCP | 将 Agent 暴露为 MCP Server |

> 三种协议的互补关系：**MCP** 是 Agent 获取工具和数据的向下连接，**A2A** 是 Agent 与 Agent 之间的水平协作，**AG-UI** 是 Agent 与前端 UI 的向上交互。详见 [协议详解](./agentcore-runtime-protocols.md)。

---

## 3. 运行时数据流

### 3.1 典型请求链路（本地工具 + HTTP 模式）

```
用户: "帮我查一下最近的订单"
         │
    前端 (Web App)
         │ POST /invocations
         ▼
    AgentCore Runtime
    ┌────────────────────────────────────────────┐
    │  Agent 容器                                │
    │                                            │
    │  1. Agent 选择 search_orders 工具          │
    │                                            │
    │  2. @tool 函数调用 Backend API             │
    │     → 自动注入认证 headers                 │
    │                                            │
    │  3. Backend API 返回数据                   │
    │                                            │
    │  4. Agent 整合结果，流式输出               │
    └────────────────────────────────────────────┘
         │ SSE Stream
         ▼
    前端渲染对话气泡
```

### 3.2 典型请求链路（MCP Gateway 模式）

```
用户: "帮我创建一个 Jira Issue"
         │
    前端 (Web App)
         │ POST /invocations
         ▼
    AgentCore Runtime (MicroVM)
    ┌──────────────────────────────────────────┐
    │  Agent 容器                              │
    │                                          │
    │  1. Strands Agent 选择 create_issue 工具 │
    │                                          │
    │  2. MCPClient 调用 Gateway               │
    │     → Gateway 路由到 Lambda Target       │
    │     → Gateway 自动注入 OAuth token       │
    │                                          │
    │  3. Lambda 调用 Jira API                 │
    │     → 创建 Issue 成功                    │
    │                                          │
    │  4. 结果返回 Agent 上下文                │
    │                                          │
    │  5. Agent 继续推理，流式输出             │
    └──────────────────────────────────────────┘
         │ SSE Stream
         ▼
    前端渲染
```

---

## 4. 安全模型

### 4.1 认证层次总览

```
层级 1: AgentCore Identity — Inbound JWT Authorizer
  └─ 用户/调用方 → Runtime / Gateway 的 OAuth 2.0 认证
  └─ OIDC 发现 + JWT 验证，支持任意 OAuth 2.0 IdP

层级 2: AgentCore Identity — Outbound Credential Provider
  └─ Agent → 第三方服务的 OAuth 2.0 / API Key 认证
  └─ Token 安全存储、自动刷新，Agent 代码不接触明文凭证

层级 3: M2M Service API Key (Secrets Manager)
  └─ Agent → Backend API 的服务间认证
  └─ secrets.compare_digest 防时序攻击
  └─ 环境变量仅存 Secret ARN，密钥值运行时从 Secrets Manager 读取

层级 4: IAM Role
  └─ AgentCore Runtime Role 限定 AWS 资源访问范围
  └─ 最小权限原则：仅 Bedrock InvokeModel, S3, DynamoDB 等
```

### 4.2 AgentCore Identity — OAuth 认证

AgentCore Identity 提供完整的 OAuth 2.0 集成能力，分为 Inbound（验证调用方）和 Outbound（代理用户访问第三方 API）两个方向。Gateway 在此基础上实现 MCP 工具的透明凭证注入。

> **完整的代码示例、配置参考和 Cognito 设置脚本见独立文档：[agentcore-oauth-integration.md](./agentcore-oauth-integration.md)**

#### Inbound JWT Authorizer（快速概览）

创建 Runtime 时通过 `authorizerConfiguration` 配置，AgentCore 在请求到达容器前自动验证 JWT：

```python
response = client.create_agent_runtime(
    agentRuntimeName='my-agent',
    authorizerConfiguration={
        "customJWTAuthorizer": {
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/POOL_ID/.well-known/openid-configuration",
            "allowedClients": ["your-client-id"],
        }
    },
    ...
)
```

容器获取用户身份有两种方式：

```python
# 方式 A：从请求体获取，容器不接触 JWT
user_id = request.user_id   # 调用方传入，AgentCore 平台层已验证

# 方式 B（需要额外 claims 时）：解码 JWT（需 --request-header-allowlist "Authorization"）
claims = jwt.decode(auth_header[7:], options={"verify_signature": False})
user_id, scopes = claims.get("sub"), claims.get("scope")
```

> 启用 OAuth 后调用方必须直接发 HTTPS + Bearer Token，不能用 `boto3.invoke_agent_runtime()`（SigV4）。

#### Outbound Credential Provider（快速概览）

Agent 代表用户调用第三方 OAuth API 时，用 `@requires_access_token` 装饰器：

```python
from bedrock_agentcore.identity.auth import requires_access_token

@requires_access_token(
    provider_name="google-provider",
    scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
    auth_flow="USER_FEDERATION",
    on_auth_url=lambda url: print("请用户授权:", url),
)
async def list_drive_files(*, access_token: str):
    # access_token 自动注入，Agent 不接触 refresh_token / client_secret
    return requests.get("https://www.googleapis.com/drive/v3/files",
                        headers={"Authorization": f"Bearer {access_token}"}).json()
```

#### Gateway OAuth（快速概览）

Gateway 在 MCP 工具层透明注入 OAuth 凭证，Agent 代码和 LLM 完全不感知：

```
Agent 调 MCP 工具 → Gateway 验证 JWT → Token Vault 获取 access_token → 注入下游 header → 执行 API
```

预集成服务（1-Click）：Salesforce、Slack、Jira、Asana、Zendesk。

### 4.3 认证方案选择

| 场景 | 推荐方案 |
|------|---------|
| Agent 仅调用自有后端 API | Inbound JWT + API Key（Secrets Manager） |
| Agent 代表用户调用第三方 OAuth API | Inbound JWT + Outbound Credential Provider |
| Agent 通过 MCP 工具调用第三方服务 | Inbound JWT + Gateway OAuth（Agent 代码零改动） |

> **演进路径**：起步阶段可以用 API Key + Secrets Manager 实现简单的服务间认证。当需要代表用户调用 Slack、Jira 等第三方服务时，引入 Outbound Credential Provider + Gateway OAuth，无需在 Agent 代码中管理 OAuth token。

### 4.4 LLM 安全隔离

- Service API Key **不注入** system prompt，对 LLM 不可见
- 工具包装层在 Python 代码层面注入 auth headers
- AgentCore Identity：Gateway 模式下凭证完全在 Agent 代码之外；`@requires_access_token` 模式下 Agent 代码接触 access_token 但不接触 refresh_token / client_secret
- 即使 LLM 被 prompt injection，也无法泄露密钥

---

## 5. Observability

AgentCore Runtime 提供开箱即用的可观测性，基于 **OpenTelemetry（OTel）** 标准，将 Traces、Metrics、Logs 统一输出到 **CloudWatch**。

### 5.1 整体架构

```
Strands Agent（自动生成 OTel Spans + Metrics）
    │
    ▼
ADOT Sidecar（AgentCore 自动注入的 ADOT Collector）
    │
    ├──► AWS X-Ray（分布式追踪）
    ├──► CloudWatch Logs（aws/spans 日志组）
    └──► CloudWatch Metrics（bedrock-agentcore 命名空间）
```

AgentCore Runtime 会**自动注入 ADOT（AWS Distro for OpenTelemetry）Sidecar** 作为遥测收集器，应用无需自行配置 OTel Collector。Strands SDK 生成的 Spans 默认通过 UDP 发送到 `127.0.0.1:2000`（ADOT Sidecar 地址），由 Sidecar 转发到 X-Ray 和 CloudWatch。

### 5.2 环境变量配置

CDK 中配置的 Observability 相关环境变量：

```typescript
// AgentCore Runtime 环境变量
AGENT_OBSERVABILITY_ENABLED: 'true',
OTEL_RESOURCE_ATTRIBUTES: `service.name=${props.agentName}`,
OTEL_LOG_LEVEL: 'info',
```

| 环境变量 | 作用 | 示例值 |
|----------|------|--------|
| `AGENT_OBSERVABILITY_ENABLED` | 启用 AgentCore 可观测性 | `true` |
| `OTEL_RESOURCE_ATTRIBUTES` | OTel 资源属性（标识服务名） | `service.name=my-agent` |
| `OTEL_LOG_LEVEL` | OTel SDK 日志级别 | `info` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP 导出端点（ADOT Sidecar 自动配置） | `http://127.0.0.1:4318` |
| `OTEL_TRACES_SAMPLER` | 采样策略（AgentCore 默认配置 X-Ray 采样） | `xray` |

### 5.3 Strands SDK 自动生成的 Spans

Strands Agents SDK 内置了 OpenTelemetry instrumentation，**无需手动埋点**即可生成完整的 Agent 执行追踪。Span 层级关系：

```
invoke_agent {agent_name}              ← 根 Span：整个 Agent 调用
  └─ execute_event_loop_cycle           ← Agent 事件循环的每一轮
       ├─ chat                          ← 模型调用（Bedrock API）
       └─ execute_tool {tool_name}      ← 工具执行
```

每种 Span 携带的关键属性：

> 以下属性名来自 Strands SDK 源码。`gen_ai.agent.tools` 和 `gen_ai.server.time_to_first_token` 等部分属性未在 Strands 官方文档中明确列出，可能随 SDK 版本变更。

| Span | 关键属性 |
|------|----------|
| `invoke_agent` | `gen_ai.system=strands-agents`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.agent.tools`（工具列表）, `gen_ai.usage.*`（累计 token） |
| `chat` | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.server.time_to_first_token`, `gen_ai.server.request.duration` |
| `execute_tool` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.status` |

Span 还附带**事件（Events）**，记录完整的对话内容：
- `gen_ai.user.message` — 用户输入
- `gen_ai.assistant.message` — 助手回复
- `gen_ai.tool.message` — 工具输入/输出
- `gen_ai.choice` — 模型选择（含 `finish_reason`）

### 5.4 Strands SDK 自动生成的 Metrics

Strands SDK 同时生成 OTel Metrics，可通过 ADOT 导出到 CloudWatch Metrics：

**计数器（Counters）：**

| 指标名 | 含义 |
|--------|------|
| `strands.event_loop.cycle_count` | Agent 事件循环总轮数 |
| `strands.tool.call_count` | 工具调用总次数 |
| `strands.tool.success_count` | 工具成功次数 |
| `strands.tool.error_count` | 工具失败次数 |

**直方图（Histograms）：**

| 指标名 | 单位 | 含义 |
|--------|------|------|
| `strands.event_loop.latency` | ms | Agent 调用端到端延迟 |
| `strands.event_loop.cycle_duration` | s | 单轮循环耗时 |
| `strands.tool.duration` | s | 工具执行耗时 |
| `strands.event_loop.input.tokens` | count | 输入 token 数 |
| `strands.event_loop.output.tokens` | count | 输出 token 数 |
| `strands.model.time_to_first_token` | ms | TTFT（首 token 延迟） |

### 5.5 CloudWatch 中查看数据

AgentCore 将遥测数据写入以下 CloudWatch 位置：

| 数据类型 | CloudWatch 位置 | 用途 |
|----------|----------------|------|
| **Traces** | X-Ray → CloudWatch ServiceMap | 分布式追踪，可视化调用链 |
| **Spans** | CloudWatch Logs `aws/spans` | ADOT 格式的 Span 文档 |
| **Runtime Logs** | CloudWatch Logs `/aws/bedrock-agentcore/runtimes/{agent-id}` | 应用日志（stdout/stderr） |
| **Metrics** | CloudWatch Metrics `bedrock-agentcore` 命名空间 | 性能指标 |

在 X-Ray 控制台中，可以看到完整的 Agent 调用链：每一轮 `chat`（模型调用）和 `execute_tool`（工具执行）的耗时、状态和 token 消耗。

### 5.6 Evaluation：基于 Spans 的质量评估

`bedrock-agentcore` SDK 提供了 `StrandsToADOTConverter`，可以将 OTel Spans 转换为 ADOT 格式，用于 **AgentCore Evaluation API**：

```python
from bedrock_agentcore.evaluation.span_to_adot_serializer import convert_strands_to_adot

# 从 CloudWatch 或内存中获取原始 OTel Spans
raw_spans = telemetry.in_memory_exporter.get_finished_spans()

# 转换为 ADOT 文档（包含 Span 文档 + 对话日志 + 工具日志）
adot_docs = convert_strands_to_adot(raw_spans)

# 提交到 AgentCore Evaluation API 进行质量评估
```

转换后的 ADOT 文档包含三种类型：
- **Span 文档**：trace_id、span_id、耗时、状态
- **Conversation Log**：用户输入 → 助手回复的完整对话记录
- **Tool Log**：工具输入 → 工具输出的执行记录

### 5.7 应用层补充日志

除了 SDK 自动生成的遥测数据，应用层也可以通过 logging 记录额外的性能指标，写入 CloudWatch Logs：

```python
# TTFT (Time to First Token)
logger.info(f"[PERF] TTFT: {first_token_time:.3f}s")

# 工具执行时间
logger.info(f"[PERF] Tool completed: {tool_name} took {execution_time_ms}ms")

# 总耗时
logger.info(f"[PERF] Total: {total_time:.3f}s, events: {event_count}")
```

这些日志与 OTel Spans 互补：OTel 提供结构化的追踪数据（可在 X-Ray 中可视化），而应用日志提供人可读的诊断信息。

---

## 6. BedrockAgentCoreApp vs FastAPI 自建：构建方式对比

除了使用 FastAPI 自建所有端点，`bedrock-agentcore` SDK 还提供了 **`BedrockAgentCoreApp`** 封装类，自动生成 `/invocations`、`/ping` 等端点。两种方式都能在 AgentCore Runtime 上运行，但在开发体验、内置能力和灵活性上有显著差异。

### 6.1 核心差异

| 维度 | BedrockAgentCoreApp | FastAPI 自建 |
|------|--------------------|-----------------------|
| **代码量** | ~20 行即可部署 | ~200+ 行（端点、中间件、SSE 格式化） |
| **`/invocations`** | `@app.entrypoint` 自动生成 | 手动定义 `@app.post("/invocations")` |
| **`/ping`** | 自动生成，内置 `Healthy`/`HealthyBusy` 状态机 | 手动定义，返回固定 `{"status": "healthy"}` |
| **`/ws`** | `@app.websocket` 自动生成 | 需手动集成 Starlette WebSocket |
| **SSE 流式** | `yield` 即可，SDK 自动格式化为 SSE | 手动构造 `data: {...}\n\n` 格式 |
| **异步任务** | 内置 `add_async_task` / `@app.ping` / Worker Loop | 需自建任务跟踪 + 线程安全 |
| **Session 管理** | `context.session_id` 自动注入 | 从请求体手动提取 `request.id` |
| **中间件** | 支持 Starlette Middleware | 原生 FastAPI 中间件 |
| **部署方式** | CDK（`fromAsset`/`fromS3`）或 Starter Toolkit（仅 demo） | CDK（`fromAsset`/`fromEcrRepository`）+ Dockerfile |
| **本地开发** | `python my_agent.py`（`app.run()` 自动启动 uvicorn） | `uvicorn main:app --reload` |
| **框架锁定** | 绑定 `bedrock-agentcore` SDK | 标准 FastAPI，可移植到任何平台 |

### 6.2 最小代码对比

**BedrockAgentCoreApp（~15 行）**：

```python
# [BedrockAgentCoreApp]
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent

app = BedrockAgentCoreApp()
agent = Agent(system_prompt="你是一个智能助手")

@app.entrypoint
async def main(payload):
    async for event in agent.stream_async(payload.get("prompt", "")):
        if "data" in event:
            yield event["data"]

if __name__ == "__main__":
    app.run()  # 自动监听 8080，自动 /ping、/invocations、/ws
```

**FastAPI 自建（简化版，~60 行）**：

```python
# [FastAPI 自建]
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from strands import Agent

app = FastAPI()
agent_system_prompt = "你是一个智能助手"

@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    async def event_generator():
        session_manager = _create_session_manager(request.id, request.user_id)
        agent = Agent(system_prompt=agent_system_prompt, session_manager=session_manager, ...)

        yield f"data: {json.dumps({'type': 'start', 'session_id': request.id})}\n\n"
        async for event in agent.stream_async(user_message):
            if "delta" in event and "text" in event["delta"]:
                yield f"data: {json.dumps({'type': 'text-delta', 'delta': event['delta']['text']})}\n\n"
        yield f"data: {json.dumps({'type': 'finish'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/ping")
async def ping():
    return {"status": "healthy"}

@app.get("/health")
async def health():
    return {"status": "healthy", "agent_name": "my-agent"}
```

### 6.3 架构差异

```
BedrockAgentCoreApp 架构：

app.run()
  └── Starlette ASGI App（SDK 内部创建）
        ├── GET  /ping        ← 自动生成，内置状态机
        ├── POST /invocations ← @app.entrypoint 路由
        ├── WS   /ws          ← @app.websocket 路由（可选）
        └── Worker Loop 线程  ← 隔离 handler 执行，防止阻塞 /ping

FastAPI 自建架构：

uvicorn main:app
  └── FastAPI ASGI App（开发者创建）
        ├── GET  /ping        ← 手动定义
        ├── GET  /health      ← 手动定义
        ├── POST /invocations ← 手动定义 + SSE 格式化
        └── CORS / Timing 中间件 ← 手动注册
```

### 6.4 什么时候选哪个

**选 BedrockAgentCoreApp**：
- 新项目，快速原型
- 需要异步后台任务（`HealthyBusy` 状态管理）
- 需要 WebSocket 双向通信
- 不需要复杂的自定义中间件或端点

**选 FastAPI 自建**：
- 需要完全控制请求/响应格式（自定义 SSE 事件类型）
- 需要额外端点（`/health`、`/`、自定义 API）
- 需要复杂中间件链（认证、限流、日志、计时）
- 需要可移植性（未来可能迁移到 ECS/EKS/Lambda）
- 团队熟悉 FastAPI 生态

### 6.5 迁移路径

如果未来需要从 FastAPI 迁移到 `BedrockAgentCoreApp`（例如需要异步任务管理），核心改动：

```python
# Before [FastAPI 自建]
app = FastAPI()

@app.post("/invocations")
async def stream_agent(request: ChatRequest):
    ...

@app.get("/ping")
async def ping():
    return {"status": "healthy"}

# After [BedrockAgentCoreApp]
from bedrock_agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
async def main(payload, context):
    # payload = 原来的 request body
    # context.session_id = 原来的 request.id
    ...
    yield chunk  # SDK 自动格式化为 SSE

@app.ping
def custom_ping():
    if has_background_tasks():
        return PingStatus.HEALTHY_BUSY
    return PingStatus.HEALTHY
```

主要改动点：
- `@app.post("/invocations")` → `@app.entrypoint`
- `ChatRequest` 解析 → `payload` dict + `context.session_id`
- 手动 SSE 格式化 → `yield` 原始数据
- 手动 `/ping` → `@app.ping` 或自动状态机
- `StreamingResponse` → SDK 自动处理

---

## 附录：典型项目文件结构

> **生产环境统一使用 CDK 部署。** AgentCore Runtime 不是孤立的服务——它依赖 Cognito、ECR、IAM Role、Secrets Manager、DynamoDB 等周边资源，这些都需要通过 CDK 统一编排。Starter Toolkit CLI（`agentcore configure` + `agentcore deploy`）适合快速 demo 和原型验证，但生产项目无法只部署一个 Runtime。

**BedrockAgentCoreApp（CDK 部署）：**

```
my-agent/
├── main.py                    # BedrockAgentCoreApp 入口（@app.entrypoint + app.run()）
├── tools/
│   └── custom_tools.py        # 自定义 @tool 函数
├── requirements.txt           # 依赖列表（CDK L2 fromS3 时使用）
├── Dockerfile                 # ARM64 容器构建（CDK L2 fromAsset 时使用）
├── pyproject.toml             # uv 项目配置（可选，替代 requirements.txt）
└── uv.lock                    # 锁文件（可选）
```

> BedrockAgentCoreApp 的 CDK 部署与 FastAPI 完全一样——CDK 不关心应用内部用了哪个框架，只看到一个容器或 zip 包。`fromS3` 无需 Dockerfile（启动命令 `['python', 'main.py']`），`fromAsset` 需要 Dockerfile（`CMD python3 main.py`）。

**FastAPI 自建（CDK 部署）：**

```
my-agent/
├── main.py                    # FastAPI 主入口（/invocations, /ping）
├── config.py                  # pydantic-settings 配置
├── schema.py                  # 请求/响应模型定义
├── config_manager.py          # S3 + 本地配置加载
├── tools/
│   ├── http_request.py        # HTTP 工具包装（auto auth）
│   └── custom_tools.py        # 自定义 @tool 函数
├── configs/
│   └── agent_config.yaml      # Agent 配置（system prompt, tools）
├── Dockerfile                 # ARM64 容器构建
├── pyproject.toml             # uv 项目配置
└── uv.lock                    # 锁文件
```

**CDK 侧（两种构建方式共用）：**
```
infrastructure/
├── lib/agentcore/
│   ├── runtime-construct.ts   # CfnRuntime (L1) 或 Runtime (L2) 定义
│   └── gateway-construct.ts   # MCP Gateway（可选）
├── lib/auth/
│   └── cognito-construct.ts   # Cognito UserPool + App Client
├── lib/backend/
│   └── api-construct.ts       # Backend API（API Gateway + Lambda / ECS）
└── lib/main-stack.ts          # 整体部署编排
```
