# AgentCore OAuth 集成指南

> 详解 Amazon Bedrock AgentCore 的三层 OAuth 认证架构：Inbound JWT（验证调用方）、Outbound Credential Provider（代理用户访问第三方 API）、Gateway OAuth（MCP 工具透明凭证注入）。

### 快速导航

| 你想了解… | 跳转到 |
|-----------|--------|
| 三层架构全景 + WAT 如何串联 | [Section 1](#1-三层-oauth-架构与-wat-链路) |
| 如何验证调用方身份 | [Section 2](#2-inbound-jwt-authorizer--验证调用方) |
| 如何代理用户访问第三方 API | [Section 3](#3-outbound-credential-provider--代理用户访问第三方-api) |
| Gateway MCP 工具的透明凭证注入 | [Section 4](#4-gateway-oauth--mcp-工具的透明凭证注入) |
| Cognito 配置参考 | [Section 5](#5-cognito-完整配置参考) |
| 支持哪些 IdP | [Section 6](#6-支持的-idp-列表) |
| 选哪种方案 | [Section 7](#7-选择指南) |
| 安全要点 | [Section 8](#8-安全要点) |
| 端到端实战代码（CDK + 容器 + Lambda） | [附录 A](#附录-a-端到端实战示例agentcore-runtime--gateway-mcp--lambda) |

> 本文基于 2025 年中的 AgentCore API 和 SDK 编写。API 参数、SDK 导入路径可能随版本变化，请以 [官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html) 为准。

---

## 1. 三层 OAuth 架构与 WAT 链路

### 1.1 全景

```
                    调用方（Portal / CLI / API）
                         │
                    Bearer JWT Token
                         │
                         ▼
              ┌─── AgentCore 平台层 ───┐
              │                        │
              │  Inbound JWT Authorizer │  ← 层级 1: 验证调用方身份
              │  (OIDC Discovery +     │
              │   JWT 签名/claims 校验) │
              │                        │
              └────────┬───────────────┘
                       │ 验证通过
                       ▼
              ┌─── Agent 容器 ─────────┐
              │                        │
              │  /invocations 端点     │
              │  获取 user_id：        │
              │  请求体 或 JWT claims  │
              │                        │
              │  调用第三方 API 时：    │
              │  Workload Access Token  │  ← WAT: 桥梁（绑定 agent + user 身份）
              │  @requires_access_token │  ← 层级 2: Outbound Credential Provider
              │  → Token Vault 自动    │     代理用户获取 OAuth token
              │    获取/刷新 token     │
              │                        │
              └────────┬────────┬──────┘
                       │        │
              直接调用第三方 API  │ 通过 MCP 工具调用
              （层级 2 已处理）   │
                                ▼
              ┌─── AgentCore Gateway ──┐
              │                        │
              │  Inbound: 验证 Agent   │  ← 层级 3: Gateway 透明凭证注入
              │  Outbound: 从 Token    │     （与层级 2 并列，非串行）
              │  Vault 获取 OAuth      │
              │  token → 注入到下游    │     Agent 直接调 API → 用层级 2
              │  API 请求 header       │     Agent 调 MCP 工具 → 用层级 3
              │                        │
              └────────────────────────┘
```

### 1.2 Workload Access Token (WAT) — 串联三层的桥梁

WAT 是 AgentCore Identity 的核心概念——一个短期令牌，绑定了 **(workload identity + user identity)** 对。Token Vault 的完整查找索引是 **(workload, user, credential_provider_name)** 三元组——WAT 提供前两个维度，第三个维度由 Gateway Target 配置或 `@requires_access_token` 装饰器指定。三层认证通过 WAT 串联成完整链路：

```
用户                    AgentCore 平台              Agent 容器              Gateway / Token Vault
 │                         │                         │                         │
 │  Bearer JWT            │                         │                         │
 │ ──────────────────────►│                         │                         │
 │                         │                         │                         │
 │                         │  1. JWT Authorizer      │                         │
 │                         │     验证签名+claims     │                         │
 │                         │                         │                         │
 │                         │  2. GetWorkloadAccess-  │                         │
 │                         │     TokenForJWT         │                         │
 │                         │     → 创建 WAT          │                         │
 │                         │                         │                         │
 │                         │  payload + WAT header   │                         │
 │                         │ ───────────────────────►│                         │
 │                         │                         │                         │
 │                         │                         │  3. Agent 代码调用      │
 │                         │                         │     MCP 工具 / Token    │
 │                         │                         │     Vault               │
 │                         │                         │                         │
 │                         │                         │  WAT header             │
 │                         │                         │ ───────────────────────►│
 │                         │                         │                         │
 │                         │                         │                         │  4. 解析 WAT：
 │                         │                         │                         │     workload = agent-X
 │                         │                         │                         │     user = user-Y
 │                         │                         │                         │
 │                         │                         │                         │  5. Token Vault 查找：
 │                         │                         │                         │     (agent-X, user-Y)
 │                         │                         │                         │     → OAuth token
 │                         │                         │                         │
 │                         │                         │  access_token           │
 │                         │                         │ ◄────────────────────── │
 │                         │                         │                         │
 │                         │                         │  6. 调用下游 API       │
 │                         │                         │     Authorization:      │
 │                         │                         │     Bearer {token}      │
```

**WAT 传递方式**：AgentCore Runtime 自动通过 payload header `WorkloadAccessToken` 注入。Agent 代码通常不需要直接操作 WAT。

**手动获取 WAT 的两种场景**：

```python
# 场景 1：在 AgentCore Runtime 容器内（从请求上下文中提取 Runtime 已注入的 WAT）
# 方式 A：通过 BedrockAgentCoreApp 的 RequestContext
@app.entrypoint
def invoke(payload, context: RequestContext):
    wat = context.request_headers.get("WorkloadAccessToken")

# 方式 B：通过 BedrockAgentCoreContext（SDK 封装，API 可能随版本变化）
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
wat = BedrockAgentCoreContext.get_workload_access_token()

# 场景 2：在 Runtime 外部（自托管 Agent），通过 IdentityClient 手动获取
from bedrock_agentcore.services.identity import IdentityClient
identity_client = IdentityClient("us-east-1")
wat = identity_client.get_workload_access_token(
    workload_name="my-agent",
    user_token="<user-jwt>"       # 或 user_id="<user-id>"（无 JWT 时）
)
```

**底层 API**：

```
bedrock-agentcore:GetWorkloadAccessTokenForJWT
  Input:  userToken（用户的 OAuth JWT）+ workloadName（Agent 的 workload 名称标识符）
  Output: Workload Access Token（短期，绑定双重身份）
```

### 1.2.1 具体示例：一个用户请求中的 Token 链路

以用户 **张三** 通过前端 Portal 调用 **claims-agent**，Agent 代表张三通过 Gateway 调用 Jira 创建工单为例：

**第一步：用户登录后拿到的 JWT（Inbound）**

标准 Cognito access_token，三段式 `header.payload.signature`，解码后的 payload：

```json
{
  "sub": "a1b2c3d4-5678-90ab-cdef-111122223333",
  "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
  "client_id": "1234567890abcdef",
  "token_use": "access",
  "scope": "aws.cognito.signin.user.admin",
  "auth_time": 1752275688,
  "exp": 1752279288,
  "username": "zhangsan"
}
```

其中 `sub` 就是 **User Identity**——用户的唯一标识。

**第二步：Runtime 生成 WAT**

Runtime JWT Authorizer 验证 JWT 通过后，调用 `GetWorkloadAccessTokenForJWT(userToken=<张三的JWT>, workloadName="claims-agent")` 生成 WAT。

WAT 是**不透明 token**（opaque token），不像 JWT 那样可以直接解码——只有 AgentCore 内部服务（Token Vault、Gateway）能解析：

```
wat-1-AQICAHj2kP5Mv4xRz...（约 200-400 个字符的 Base64 编码字符串）
```

WAT 内部编码了双重身份（Agent 代码看不到，Token Vault 能解析）：

```
┌──────────────────────────────────────────────────────┐
│  Workload Access Token                               │
│                                                      │
│  Workload Identity: claims-agent                     │  ← workloadName（Agent 名称）
│  User Identity:     a1b2c3d4-5678-90ab-cdef-1111...  │  ← 从 JWT sub 提取
│  Issued At:         2026-03-20T10:30:00Z             │
│  Expires At:        2026-03-20T11:30:00Z             │  ← 短期有效
│  Account:           111122223333                     │
│  Region:            us-east-1                        │
└──────────────────────────────────────────────────────┘
```

**第三步：各环节的 HTTP Header**

```
用户 → AgentCore Runtime
  Authorization: Bearer eyJhbGciOiJSUzI1NiI...              ← 用户 JWT

Runtime → Agent 容器（自动注入）
  Authorization: Bearer eyJhbGciOiJSUzI1NiI...              ← 用户 JWT（透传）
  WorkloadAccessToken: wat-1-AQICAHj2kP5Mv4xRz...           ← WAT（Runtime 自动注入）

Agent 容器 → Gateway（GATEWAY_IAM_ROLE 类型）
  Authorization: Bearer eyJhbGciOiJSUzI1NiI...              ← 用户 JWT（Gateway 验证）

Agent 容器 → Gateway（OAUTH 类型，需额外传 WAT）
  Authorization: Bearer eyJhbGciOiJSUzI1NiI...              ← 用户 JWT
  WorkloadAccessToken: wat-1-AQICAHj2kP5Mv4xRz...           ← WAT（Gateway 用它查 Token Vault）

Gateway → Token Vault 查找
  WAT 解析 → (workload=claims-agent, user=a1b2c3d4...)
  WAT 解析 → (workload=claims-agent, user=a1b2c3d4...)
  + Gateway Target 配置的 credential_provider_name=atlassian-provider
  Token Vault 按三元组 (workload, user, credential_provider) 查找
  → 返回张三之前授权的 Jira OAuth token（见下方"前置条件"）

Gateway → Jira API（最终的第三方调用）
  Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6...      ← 张三的 Jira OAuth token（Atlassian JWT 格式）
                                                                （与 Cognito 无关，来自 Atlassian IdP）
```

> **前置条件：Jira token 从何而来？**
>
> Cognito JWT 只负责验证"谁在调用 Agent"（Inbound），Jira OAuth token 来自另一套完全独立的 OAuth 流程（Outbound）：
>
> 1. 管理员预先注册了 Atlassian 的 Credential Provider（`create_oauth2_credential_provider`，vendor=`AtlassianOauth2`，见 Section 3.2）
> 2. 张三**首次**使用 claims-agent 访问 Jira 时，触发 Atlassian 的 3-legged OAuth 授权——张三在浏览器中登录 Atlassian 并同意授权
> 3. Atlassian 返回的 token 存入 Token Vault，索引为 `(workload=claims-agent, user=a1b2c3d4...)`
> 4. 后续调用时，Gateway 用 WAT 解析出 `(workload, user)` + Target 配置的 `credential_provider_name`，按三元组从 Token Vault 取出缓存的 Jira token（过期则自动用 refresh_token 刷新）
>
> **两套 OAuth 流程对比**：
>
> | | Inbound（验证调用方） | Outbound（访问第三方） |
> |---|---|---|
> | **IdP** | Cognito | Atlassian |
> | **Token** | Cognito JWT（用户登录获得） | Jira OAuth token（用户授权获得） |
> | **用途** | 证明"我是张三" | 代表张三操作 Jira |
> | **存储** | 前端持有 | Token Vault 托管 |
> | **触发时机** | 每次请求都携带 | 首次授权后自动管理 |

**三种身份对照**：

| 身份 | 示例值 | 来源 | 作用 |
|------|--------|------|------|
| **User Identity** | `a1b2c3d4-5678-90ab-cdef-111122223333`（JWT `sub`） | Cognito JWT | 标识"是哪个用户" |
| **Workload Identity** | `claims-agent`（workloadName） | Agent 注册时指定 | 标识"是哪个 Agent" |
| **WAT** | `wat-1-AQICAHj2kP5Mv4xRz...`（不透明 token） | `GetWorkloadAccessTokenForJWT` 生成 | 绑定上面两者，Token Vault 用 `(workload, user, credential_provider)` 三元组查找正确的第三方 token |

> **要点**：WAT 不是给 Agent 代码消费的——Agent 代码只是透传它。真正解析 WAT 的是 Token Vault 和 Gateway。不同用户通过同一个 Agent 访问 Jira 时，Token Vault 返回的是**各自授权的 token**，而非共享凭证。

### 1.3 Gateway Target 类型与 Header 传递

Gateway Target 有两种 `credentialProviderConfiguration` 类型，决定了容器需要传递哪些 header：

| Target 类型 | 需要的 Header | 说明 |
|---|---|---|
| `GATEWAY_IAM_ROLE` | `Authorization`（用户 JWT） | Gateway 用自身 IAM Role 调用 Lambda，不需要 Token Vault |
| `OAUTH` | `Authorization`（用户 JWT）+ `WorkloadAccessToken`（WAT） | Gateway 需要 WAT 从 Token Vault 查找第三方 OAuth token（如 Jira），按 `(workload, user, credential_provider)` 三元组隔离 |

> **为什么 OAUTH 类型必须传 WAT？**
> Token Vault 的存储索引是 `(workload identity, user identity)`。WAT 编码了这个二元组——没有 WAT，Gateway 无法知道该取哪个用户、哪个 Agent 的第三方 token。

本文示例统一使用 `GATEWAY_IAM_ROLE` 类型，因此容器代码只传 `Authorization` header。

### 1.4 四种场景的认证链路

**场景 A：Agent 直接调用第三方 API（Outbound Credential Provider）**

```
用户 JWT → Runtime JWT Authorizer → WAT 注入
    → Agent 代码 @requires_access_token
    → SDK 用 WAT 调用 GetResourceOauth2Token
    → Token Vault 返回 access_token（按 workload+user+credential_provider 三元组隔离）
    → Agent 代码拿到 access_token，调用第三方 API
```

**场景 B：Agent 通过 Gateway 调用 MCP 工具**

```
用户 JWT → Runtime JWT Authorizer → 容器收到：Authorization（原始 JWT）
    → Agent 代码通过 MCPClient 调用 Gateway（转发 Authorization header）
    → Gateway JWT Authorizer 验证用户原始 JWT
    → Gateway 使用 GATEWAY_IAM_ROLE 调用 Lambda Target
    → Agent 代码完全不感知认证细节（Gateway 透明处理）
    注：若 Gateway Target 使用 OAUTH 类型，还需额外传递 WorkloadAccessToken header，
        Gateway 用 WAT 从 Token Vault 获取下游 OAuth token 并注入请求
```

**场景 C：Agent 调用 Agent（A2A via Gateway）**

```
Agent A 容器 → Gateway（Authorization: 用户 JWT）
    → Gateway JWT Authorizer 验证用户 JWT
    → Gateway 路由到 Agent B 的 Runtime
    → Agent B 收到请求 + 身份信息
```

适用场景：需要 Gateway 统一管理认证和路由，Agent B 需代表原始用户执行操作。

**场景 D：Agent 直接调用 Agent（A2A 直连，不经 Gateway）**

A2A 协议的核心设计原则是 **Agent 对等通信（peer-to-peer）**——Agent 之间不需要共享内部实现，通过标准 JSON-RPC 2.0 消息直接交互。在不需要 Gateway 凭证管理的场景下，Agent A 可以直接调用 Agent B 的 AgentCore Runtime 端点：

```
Agent A 代码
    → 发现 Agent B：获取 Agent Card（GET /.well-known/agent-card.json）
    → 认证：SigV4（IAM）或 Bearer JWT
    → 直接调用 Agent B 的 AgentCore 平台端点：
      POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{AgentB_ARN}/invocations?qualifier=DEFAULT
    → Agent B 容器（POST :9000/）处理 JSON-RPC 请求
    → Agent B 返回结果 → Agent A 继续处理
```

> **与场景 C 的区别**：场景 C 经 Gateway 路由，Gateway 负责认证和凭证注入（WAT → Token Vault → OAuth token）。场景 D 是 Agent A 直接调用 Agent B 的 Runtime 端点，不经过 Gateway，因此：
> - **无自动凭证注入**：如果 Agent B 需要访问第三方 OAuth API，需要自行通过 `@requires_access_token`（层级 2）处理
> - **无 WAT 身份链传递**：Agent B 不会自动获得原始用户身份，需要 Agent A 在请求体中显式传递
> - **认证独立**：Agent A 需要对 Agent B 的 Runtime 端点有调用权限（SigV4 需要 `bedrock-agentcore:InvokeAgentRuntime` IAM 权限；JWT 需要 Agent B 的 JWT Authorizer 允许 Agent A 的 client_id）

**场景 C vs D 选择指南**：

| 维度 | 场景 C（via Gateway） | 场景 D（直连） |
|------|----------------------|---------------|
| 认证管理 | Gateway 统一处理 | 各 Agent 独立配置 |
| 用户身份传递 | WAT 自动传递 | 需显式传递 |
| 凭证注入 | Gateway 透明注入 | Agent B 自行处理 |
| 架构复杂度 | 需部署和维护 Gateway | 无额外组件 |
| 适用场景 | 需要代用户身份、OAuth 凭证管理 | 同账号内部协作、Agent 间简单消息传递 |

### 1.5 WAT 的安全特性

| 特性 | 说明 |
|------|------|
| **双重身份绑定** | WAT = workload identity + user identity，Token Vault 按 `(workload, user, credential_provider)` 三元组隔离凭证 |
| **短期有效** | WAT 有效期短，降低泄露风险（具体时长以官方文档为准） |
| **最小权限** | Token Vault 只返回该 (agent, user) 对被授权的 scope 的 token |
| **审计追踪** | 所有 WAT 创建和使用都有审计日志 |
| **自动管理** | 在 AgentCore Runtime 内，SDK 自动创建和传递 WAT，Agent 代码无需手动管理 |

> **关键点**：Token Vault 的查找索引是 `(workload, user, credential_provider)` 三元组，实现了"谁在用哪个 Agent 访问哪个第三方服务"的三维权限控制。同一个 Agent 可以同时接入 Jira 和 Google Drive（不同 credential provider），不同用户通过同一个 Agent 访问同一个服务时，Token Vault 返回的是**该用户自己授权的 token**，而不是共享凭证。

---

## 2. Inbound JWT Authorizer — 验证调用方

> ⚠️ **关键约束**：AgentCore Runtime 的认证方式是 **SigV4 与 JWT Bearer 二选一**，不能同时启用。配置了 `customJWTAuthorizer` 后，所有 AWS SDK 调用（boto3、Java SDK 等，均使用 SigV4 签名）都不再适用，必须直接发 HTTPS 请求并携带 Bearer Token。反之，未配置 JWT Authorizer 的 Runtime 只接受 SigV4 认证。

### 2.1 工作原理

AgentCore 在请求到达容器 `/invocations` **之前**，自动验证 JWT Bearer Token：

1. 从 OIDC Discovery URL 获取 IdP 的公钥 (JWKS)
2. 验证 JWT 签名
3. 校验 claims（aud、client_id、scope、自定义 claims）
4. 验证通过 → 请求透传到容器；失败 → 返回 401

容器代码**不需要**自行验证 JWT 签名。用户身份可以从请求体获取（简单），也可以解码 JWT claims 获取更多信息（见 2.4）。

### 2.2 创建 Runtime 时配置

> **命名注意**：boto3 API 使用 `customJWTAuthorizer`（大写 JWT），CDK L1 (`CfnRuntime`) 使用 `customJwtAuthorizer`（小写 wt）。内部结构一致：`{ discoveryUrl, allowedClients, allowedAudience?, allowedScopes?, customClaims? }`。

**Python SDK**：

```python
import boto3

client = boto3.client('bedrock-agentcore-control', region_name="us-east-1")

response = client.create_agent_runtime(
    agentRuntimeName='my-agent',
    agentRuntimeArtifact={
        'containerConfiguration': {
            'containerUri': '111122223333.dkr.ecr.us-east-1.amazonaws.com/my-agent:latest'
        }
    },
    authorizerConfiguration={
        "customJWTAuthorizer": {                    # ← boto3: 大写 JWT
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/POOL_ID/.well-known/openid-configuration",
            "allowedClients": ["your-client-id"],
            # 可选：
            # "allowedAudience": ["https://my-api.example.com"],
            # "allowedScopes": ["read", "write"],
            # "customClaims": [...]
        }
    },
    networkConfiguration={"networkMode": "PUBLIC"},
    roleArn='arn:aws:iam::111122223333:role/AgentRuntimeRole',
    # 可选：生命周期配置（不设置则使用默认值，具体范围以官方 API 文档为准）
    lifecycleConfiguration={
        'idleRuntimeSessionTimeout': 300,    # 空闲超时（秒），默认值请查阅官方 API 文档
        'maxLifetime': 3600                  # 最大时长（秒），默认值请查阅官方 API 文档
    }
)
```

**AgentCore CLI**：

```bash
agentcore configure --entrypoint main.py \
  --name my_agent \
  --execution-role $ROLE_ARN \
  --authorizer-config "{
    \"customJWTAuthorizer\": {
      \"discoveryUrl\": \"$DISCOVERY_URL\",
      \"allowedClients\": [\"$CLIENT_ID\"]
    }
  }" \
  --request-header-allowlist "Authorization"

agentcore deploy
```

> `--request-header-allowlist "Authorization"` 仅在容器需要解码 JWT claims 时（方式 B）才需要。如果只从请求体获取 `user_id`（方式 A），不需要此配置。

### 2.3 配置参数详解

| 参数 | JWT Claim | 说明 | 必须 |
|------|-----------|------|------|
| `discoveryUrl` | `iss` | OIDC 发现端点，格式 `^.+/\.well-known/openid-configuration$` | 是 |
| `allowedClients` | `client_id` | 允许的应用客户端 ID 列表 | 四项至少配一项 |
| `allowedAudience` | `aud` | Token 目标受众（API 资源标识） | 四项至少配一项 |
| `allowedScopes` | `scope` | 允许的权限范围（匹配任一即可） | 四项至少配一项 |
| `customClaims` | 自定义 | claim 匹配规则 | 四项至少配一项 |

Custom Claims 匹配规则示例：

```json
{
  "customClaims": [
    {
      "inboundTokenClaimName": "Group",
      "inboundTokenClaimValueType": "STRING",
      "authorizingClaimMatchValue": {
        "claimMatchValue": { "matchValueString": "Developer" },
        "claimMatchOperator": "EQUALS"
      }
    }
  ]
}
```

`claimMatchValue` 是 union 类型：`matchValueString`（单值）或 `matchValueStringList`（数组）。`STRING_ARRAY` 类型的 claim 支持 `CONTAINS`（全部包含）和 `CONTAINS_ANY`（包含任一）操作符。

### 2.4 容器代码：获取用户身份

#### 方式 A：从请求体获取（推荐，简单直接）

AgentCore 平台层已验证 JWT，容器直接使用请求体中的 `user_id`，**完全不接触 JWT**：

```python
class ChatRequest(BaseModel):
    id: str        # session_id
    user_id: str   # 调用方传入，AgentCore 已在平台层验证过 JWT
    messages: list[ClientMessage]

@app.post("/invocations")
async def stream_agent(request: ChatRequest, http_request: Request):
    session_id = request.id
    user_id = request.user_id   # ★ 简单直接，无需 PyJWT 依赖
    ...
```

**优点**：简单，零额外依赖。**前提**：信任 AgentCore 平台层已完成认证，请求体中的 `user_id` 由受信调用方填入。

#### 方式 B：从 JWT claims 提取（需要额外身份信息时）

当容器需要 JWT 中的额外 claims（scopes、角色、自定义属性等）时，可以主动解码 JWT。需要 `--request-header-allowlist "Authorization"` 配置让 AgentCore 透传原始 JWT：

> ⚠️ **安全警告**：下方 `verify_signature=False` **仅在 AgentCore Runtime 容器内安全**（平台层已验证签名）。在任何其他环境中**必须验证 JWT 签名**，否则存在 token 伪造风险。

```python
import jwt  # PyJWT

@app.post("/invocations")
async def stream_agent(request: ChatRequest, http_request: Request):
    auth_header = http_request.headers.get("Authorization", "")
    user_id = request.user_id  # fallback

    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # ⚠️ 仅限 AgentCore Runtime 容器内使用——平台层已验证签名
        claims = jwt.decode(token, options={"verify_signature": False})

        user_id = claims.get("sub")           # 用户唯一 ID（Cognito sub）
        username = claims.get("username")      # 用户名
        client_id = claims.get("client_id")    # 应用客户端 ID
        scopes = claims.get("scope", "")       # 权限范围

        logger.info(f"Authenticated: {username} ({user_id}), scopes={scopes}")

    ...
```

JWT payload 示例（Cognito access token）：

```json
{
  "sub": "a1b2c3d4-5678-90ab-cdef-111122223333",
  "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
  "client_id": "1234567890abcdef",
  "token_use": "access",
  "scope": "aws.cognito.signin.user.admin",
  "auth_time": 1752275688,
  "exp": 1752279288,
  "username": "zhangsan"
}
```

#### 方式 C：通过 X-Amzn-Bedrock-AgentCore-Runtime-User-Id Header（SigV4 + Outbound OAuth 场景）

当使用 **SigV4 认证**（而非 JWT Bearer）但仍需要代用户获取 Outbound OAuth token 时，调用方通过此 header 指定用户 ID：

```python
# SigV4 调用方式，通过 header 传入 user_id
response = client.invoke_agent_runtime(
    agentRuntimeArn=agent_arn,
    runtimeSessionId="session-abc",
    contentType='application/json',
    accept='text/event-stream',
    body=json.dumps({"prompt": "Hello"}),
    # user_id 通过专用 header 传递
    # X-Amzn-Bedrock-AgentCore-Runtime-User-Id: "user-123"
)
```

> **前提**：调用方 IAM 角色需要额外权限 `bedrock-agentcore:InvokeAgentRuntimeForUser`。适用于后端服务通过 SigV4 调用 Agent，但 Agent 仍需代表特定终端用户获取 OAuth token 的场景。

### 2.5 调用方：携带 Bearer Token

#### 生产做法：Authorization Code Flow + PKCE

SPA 前端使用 **Authorization Code Grant + PKCE** 获取 token：

```typescript
// OIDC 登录后，使用 access_token 直调 AgentCore 端点
const escapedArn = encodeURIComponent(agentRuntimeArn);
const endpoint = `https://bedrock-agentcore.${region}.amazonaws.com`
  + `/runtimes/${escapedArn}/invocations?qualifier=DEFAULT`;

const response = await fetch(endpoint, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`,                         // Cognito JWT
    'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId,
  },
  body: JSON.stringify({ id: sessionId, user_id: userId, messages }),
});
```

完整流程：
```
用户 → Cognito Hosted UI（Authorization Code + PKCE）
     → 返回 authorization_code
     → OIDC 客户端库用 code + code_verifier 换 access_token
     → Bearer Token 直调 AgentCore HTTPS 端点
     → AgentCore JWT Authorizer 验证 → 转发到容器 /invocations
```

#### CLI 快速测试（仅限开发调试）

```bash
# ⚠️ USER_PASSWORD_AUTH 仅用于本地开发调试，切勿用于生产
# 建议通过环境变量传递凭证，避免 shell history 泄露
export TEST_USER="testuser"
export TEST_PASSWORD="<your-test-password>"

TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$TEST_USER",PASSWORD="$TEST_PASSWORD" \
  --region us-east-1 | jq -r '.AuthenticationResult.AccessToken')

ESCAPED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$AGENT_ARN', safe=''))")
curl -X POST "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: session-001" \
  -d '{"prompt": "Hello"}'
```

> **重要**：SigV4 与 JWT Bearer 二选一，详见 [Section 2 开头的关键约束](#2-inbound-jwt-authorizer--验证调用方)。

### 2.6 未认证响应（401）

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{ARN}/invocations/.well-known/oauth-protected-resource?qualifier=DEFAULT"
```

客户端可访问 `resource_metadata` URL 获取 OAuth 端点发现信息，实现自动 token 获取。

---

## 3. Outbound Credential Provider — 代理用户访问第三方 API

当 Agent 需要代表用户调用第三方 OAuth API（Google Drive、Slack、GitHub 等），Outbound Credential Provider 负责 token 的获取、安全存储和自动刷新。

### 3.1 两种 OAuth 模式

| 模式 | OAuth 2.0 Flow | 场景 | 用户交互 |
|------|---------------|------|---------|
| **User-Delegated** | Authorization Code Grant (3-legged) | 访问用户个人数据（邮件、日历、文件） | 需要用户授权同意 |
| **Machine-to-Machine** | Client Credentials Grant (2-legged) | 系统级操作（后台任务、定时处理） | 无 |

### 3.2 注册 Credential Provider

**CLI（以 Google 为例）**：

```bash
RESPONSE=$(aws bedrock-agentcore-control create-oauth2-credential-provider \
  --name "google-provider" \
  --credential-provider-vendor "GoogleOauth2" \
  --oauth2-provider-config-input '{
    "googleOauth2ProviderConfig": {
      "clientId": "your-google-client-id",
      "clientSecret": "your-google-client-secret"
    }
  }' --output json)

# ★ 必须把返回的 callbackUrl 注册到 Google OAuth 应用的 Redirect URIs
CALLBACK_URL=$(echo $RESPONSE | jq -r '.callbackUrl')
echo "Register this as Google OAuth redirect URI: $CALLBACK_URL"
```

**Cognito 作为 Outbound Provider**（Agent 访问 Cognito 保护的资源）：

```json
{
  "name": "Cognito",
  "credentialProviderVendor": "CognitoOauth2",
  "oauth2ProviderConfigInput": {
    "includedOauth2ProviderConfig": {
      "clientId": "your-client-id",
      "clientSecret": "your-client-secret",
      "authorizationEndpoint": "https://{domain}.auth.us-east-1.amazoncognito.com/oauth2/authorize",
      "tokenEndpoint": "https://{domain}.auth.us-east-1.amazoncognito.com/oauth2/token",
      "issuer": "https://cognito-idp.us-east-1.amazonaws.com/{pool-id}"
    }
  }
}
```

**API Key（简单场景）**：

```bash
aws bedrock-agentcore-control create-api-key-credential-provider \
  --name "internal-service" \
  --api-key "your-api-key"
```

支持的凭证类型：OAuth2 access token、API key、client certificate、SAML assertion、自定义 token。

### 3.3 Agent 代码中获取 Token

```python
from bedrock_agentcore.identity.auth import requires_access_token
import requests

@requires_access_token(
    provider_name="google-provider",
    scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
    auth_flow="USER_FEDERATION",                                          # 3-legged OAuth
    on_auth_url=lambda url: print("请用户在浏览器中打开:", url),             # 首次授权回调
    force_authentication=False,                                            # True = 每次强制重新授权
    callback_url='<your-oauth2-callback-url>'                                    # 可选: session binding，替换为实际回调 URL
)
async def list_google_drive_files(*, access_token: str):
    """
    access_token 由装饰器自动注入。
    Agent 代码不接触 refresh_token、client_secret，只拿到短期 access_token。
    """
    response = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    return response.json()
```

> **关于 `@tool` + `@requires_access_token` 叠加使用**：官方示例使用 `BedrockAgentCoreApp` 而非直接与 Strands `@tool` 组合。两个装饰器的叠加顺序和 async 兼容性**尚未经官方验证**，如需在 Strands Agent 中使用 Outbound OAuth，建议参考 `BedrockAgentCoreApp` 的官方示例，或等待官方适配。

### 3.4 完整 OAuth 流程（幕后）

```
1. Agent 代码调用 @requires_access_token 装饰的函数

2. 装饰器向 AgentCore Identity 请求 Workload Access Token
   ├── Agent 在 AgentCore Runtime 中运行 → Runtime 自动注入
   └── Agent 在其他环境运行 → 通过 AgentCore SDK 获取
   API: bedrock-agentcore:GetWorkloadAccessTokenForJWT

3. 用 Workload Access Token 向 Token Vault 请求目标服务的 OAuth token
   API: bedrock-agentcore:GetResourceOauth2Token

4. Token Vault 检查：
   ├── 已有有效 token
   │   └── 直接注入 access_token 到函数的 access_token 参数
   │
   ├── token 过期，有 refresh_token
   │   └── 自动刷新 → 注入新 access_token（用户无感）
   │
   └── 无任何 token（首次使用）
       └── 生成 3-legged OAuth 授权 URL
           └── 触发 on_auth_url 回调
               └── 用户在浏览器中授权
                   └── IdP 回调 AgentCore callbackUrl
                       └── Token 存储到 Vault（绑定 workload identity + user ID + credential provider）

5. 后续调用：直接从 Vault 获取缓存的 token，不再提示用户

关键安全特性：
- Agent 代码只拿到 access_token，永远接触不到 refresh_token 和 client_secret
- Token 绑定到 (agent identity + user identity + credential provider) 三元组，不同用户/Agent/服务隔离
- 所有凭证访问有完整审计日志
```

### 3.5 需要的 IAM 权限

> **SLR 变更**：较新创建的 Agent 使用 Service-Linked Role（`AWSServiceRoleForBedrockAgentCoreRuntimeIdentity`）自动管理 workload identity 权限，无需手动配置以下策略。以下 IAM 配置仅适用于旧 Agent。详见 [AgentCore Identity 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-identity.html)。

Agent Runtime Role 需要以下权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AgentCoreIdentityAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceOauth2Token"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:workload-identity-directory/default/workload-identity/agentname-*"
      ]
    },
    {
      "Sid": "CredentialProviderSecrets",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:bedrock-agentcore-identity!default/oauth2/*",
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:bedrock-access-*"
      ]
    },
    {
      "Sid": "CreateServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::*:role/aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreRuntimeIdentity",
      "Condition": {
        "StringEquals": {
          "iam:AWSServiceName": "runtime-identity.bedrock-agentcore.amazonaws.com"
        }
      }
    }
  ]
}
```

---

## 4. Gateway OAuth — MCP 工具的透明凭证注入

AgentCore Gateway 把 Inbound JWT + Outbound Credential Provider 结合到 MCP 工具层，Agent 代码和 LLM **完全不感知** OAuth 凭证。

### 4.1 工作流程

```
Agent 调用 MCP 工具（如 "create_jira_issue"）
    │
    ▼
AgentCore Gateway
    │
    ├── 1. Inbound: JWT Authorizer 验证 Agent 的身份
    │      └── discoveryUrl + allowedClients/Scopes
    │
    ├── 2. 工具路由: MCP tool_name → 目标 API / Lambda
    │
    ├── 3. Outbound: 从 Credential Provider 获取目标服务的 OAuth token
    │      └── Token Vault → access_token（过期自动刷新）
    │
    ├── 4. 凭证注入: access_token → 下游 API 请求的 Authorization header
    │
    └── 5. 执行 API 调用 → 返回 MCP tool result 给 Agent
```

### 4.2 创建 Gateway 配置

```python
import boto3

client = boto3.client('bedrock-agentcore-control', region_name="us-east-1")

response = client.create_gateway(
    name="tools-gateway",
    roleArn="arn:aws:iam::111122223333:role/GatewayRole",
    protocolType="MCP",
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration={
        "customJWTAuthorizer": {
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/POOL_ID/.well-known/openid-configuration",
            "allowedClients": ["your-client-id"]
        }
    },
)
gateway_id = response["gatewayId"]

# 创建 Target（绑定工具 schema + 凭证配置）
client.create_gateway_target(
    gatewayIdentifier=gateway_id,
    name="my-target",
    targetConfiguration={
        "mcp": {
            "lambda": {
                "lambdaArn": "arn:aws:lambda:us-east-1:111122223333:function:my-tools",
                "toolSchema": {"inlinePayload": [...]},  # MCP 工具定义
            }
        }
    },
    # 下游认证方式（三选一，见 Section 4.4）
    credentialProviderConfigurations=[
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    ],
)
```

### 4.3 Gateway vs Agent 代码中的 OAuth

| 维度 | Gateway 凭证注入 | Agent 代码 `@requires_access_token` |
|------|-----------------|-------------------------------------|
| OAuth 处理位置 | Gateway 平台层（Agent 代码零改动） | Agent 代码中（装饰器） |
| LLM 可见性 | token 对 LLM 不可见 | token 对 LLM 不可见（装饰器注入，不经过 tool input/output） |
| 适用场景 | 工具通过 Gateway 注册（MCP） | 工具在 Agent 代码中直接调用 |
| 首次授权 | Gateway 处理 OAuth 重定向 | on_auth_url 回调 |
| 预集成服务 | Salesforce, Slack, Jira, Asana, Zendesk (1-Click) | 需手动编码 |
| 自定义 API | OpenAPI / Smithy spec + Credential Provider | 任意 HTTP API |

### 4.4 credentialProviderConfigurations

| 类型 | 何时使用 | 配置方式 |
|------|---------|---------|
| `GATEWAY_IAM_ROLE` | Lambda Target 在同账号，无需额外 OAuth | `[{"credentialProviderType": "GATEWAY_IAM_ROLE"}]` |
| `OAUTH` | 下游是第三方 OAuth API（Jira、Slack 等） | `[{"credentialProviderType": "OAUTH", "credentialProvider": {"providerName": "..."}}]` |
| `API_KEY` | 下游使用 API Key 认证 | `[{"credentialProviderType": "API_KEY", "credentialProvider": {"providerName": "..."}}]` |

### 4.5 预集成服务（1-Click）

Gateway 对以下服务提供开箱即用的 OAuth 集成，无需手动配置 OAuth endpoint：

- **Salesforce** — CRM 数据读写
- **Slack** — 消息发送、频道管理
- **Jira** — Issue 创建、查询、更新
- **Asana** — 任务管理
- **Zendesk** — 工单系统

对于其他 API，提供 OpenAPI spec 或 Smithy model + Credential Provider 即可接入。

---

## 5. Cognito 完整配置参考

### 5.1 Runtime Inbound Auth（用户认证）

#### 生产配置：Authorization Code Grant + PKCE

```bash
#!/bin/bash
# setup_cognito_runtime.sh — 生产级配置

export REGION=us-east-1

# 创建 User Pool（强密码策略）
export POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "AgentUserPool" \
  --auto-verified-attributes email \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireLowercase":true,"RequireUppercase":true,"RequireNumbers":true,"RequireSymbols":true}}' \
  --region $REGION | jq -r '.UserPool.Id')

# 添加 Cognito Domain（Hosted UI 需要）
DOMAIN_PREFIX="agent-$(aws sts get-caller-identity --query Account --output text)"
aws cognito-idp create-user-pool-domain \
  --user-pool-id $POOL_ID \
  --domain "$DOMAIN_PREFIX" \
  --region $REGION

# 创建 App Client — Authorization Code Grant（生产推荐）
# 注：SPA 客户端不使用 client secret，PKCE 由客户端库自动处理
export CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $POOL_ID \
  --client-name "AgentPortalClient" \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_REFRESH_TOKEN_AUTH" \
  --allowed-o-auth-flows "code" \
  --allowed-o-auth-scopes "openid" "email" "profile" \
  --allowed-o-auth-flows-user-pool-client \
  --callback-urls "https://your-portal.example.com/" "http://localhost:8080/callback" \
  --logout-urls "https://your-portal.example.com/" \
  --supported-identity-providers "COGNITO" \
  --region $REGION | jq -r '.UserPoolClient.ClientId')

# 输出配置值
export DISCOVERY_URL="https://cognito-idp.$REGION.amazonaws.com/$POOL_ID/.well-known/openid-configuration"
echo "POOL_ID=$POOL_ID"
echo "CLIENT_ID=$CLIENT_ID"
echo "DISCOVERY_URL=$DISCOVERY_URL"
echo "COGNITO_DOMAIN=https://${DOMAIN_PREFIX}.auth.${REGION}.amazoncognito.com"
```

前端使用 `react-oidc-context`（或任何 OIDC 客户端库）即可完成 PKCE 流程，密码**永远不会**经过 JavaScript：

```
用户点击登录 → 302 到 Cognito Hosted UI → 用户输入密码 → Cognito 验证
  → 302 回 callback URL（带 authorization_code）
  → 客户端用 code + code_verifier 换取 access_token + id_token
```

#### CLI 快速测试（仅限开发调试）

> ⚠️ **前提条件**：生产配置创建的 App Client 默认**不包含** `ALLOW_USER_PASSWORD_AUTH` flow。使用下方命令前，需要先手动启用：
>
> ```bash
> aws cognito-idp update-user-pool-client \
>   --user-pool-id "$POOL_ID" \
>   --client-id "$CLIENT_ID" \
>   --explicit-auth-flows "ALLOW_REFRESH_TOKEN_AUTH" "ALLOW_USER_PASSWORD_AUTH" \
>   --region $REGION
> ```
>
> 此 flow 允许明文密码直传，**仅用于本地开发调试，切勿在生产环境启用**。测试完毕后建议移除。

```bash
export TEST_USER="testuser"
export TEST_PASSWORD="<your-test-password>"

export TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$TEST_USER",PASSWORD="$TEST_PASSWORD" \
  --region $REGION | jq -r '.AuthenticationResult.AccessToken')

# 解码查看 JWT payload（需要处理 base64url padding）
echo "$TOKEN" | cut -d '.' -f2 | tr '_-' '/+' | awk '{l=4-length($0)%4; if(l<4) for(i=0;i<l;i++) $0=$0"="; print}' | base64 -d 2>/dev/null | jq .
```

---

## 6. 支持的 IdP 列表

AgentCore Identity 内置支持 25+ OAuth 2.0 提供商（含 `CustomOauth2`），均通过标准 OIDC Discovery 端点集成。以下列表截至 2025 年中，最新列表以官方 API 文档为准。

| 类型 | 提供商（API Valid Values） |
|------|--------|
| 企业 | CognitoOauth2, OktaOauth2, MicrosoftOauth2, Auth0Oauth2, OneLoginOauth2, PingOneOauth2, CyberArkOauth2, FusionAuthOauth2 |
| 社交 | GoogleOauth2, GithubOauth2, FacebookOauth2, LinkedinOauth2, XOauth2, YandexOauth2, RedditOauth2, SpotifyOauth2, TwitchOauth2 |
| SaaS | SlackOauth2, SalesforceOauth2, HubspotOauth2, NotionOauth2, AtlassianOauth2, DropboxOauth2, ZoomOauth2 |
| 通用 | CustomOauth2（适配任意 OAuth 2.0 提供商） |

> 完整列表以 [CreateOauth2CredentialProvider API Reference](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateOauth2CredentialProvider.html) 的 Valid Values 为准。

无需为每个 IdP 编写适配代码。只需提供 Discovery URL，AgentCore 自动处理公钥获取、签名算法适配、token 格式解析。

---

## 7. 选择指南

### 7.1 场景对照

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| Agent 仅调用自有后端 API | Inbound JWT + API Key/Secrets Manager | 简单，无需 Credential Provider |
| Agent 代表用户调用第三方 OAuth API | Inbound JWT + Outbound Credential Provider | Token 自动管理 + 刷新 |
| Agent 通过 MCP 工具调用第三方服务 | Inbound JWT + Gateway OAuth | Agent 代码零改动，凭证透明注入 |
| 多 Agent 协作，Agent 间调用 | A2A via Gateway 或直连（见场景 C/D） | Agent 身份隔离 |

### 7.2 演进路径

```
起步                          第三方集成                      规模化
─────────────────────────── → ─────────────────────────── → ───────────────────────────
Inbound JWT 保护端点           注册 Credential Provider       Gateway 1-Click 集成
Agent 内部用 API Key           + @requires_access_token       Agent 代码零改动
调自有服务                     装饰器                         预集成 Salesforce/Slack/Jira
```

### 7.3 容器代码的两种架构模式

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| **本地工具模式** | 工具逻辑在容器内，FastAPI 直接暴露 `/invocations`，不涉及 MCP | 工具简单、不需要 Gateway 凭证管理 |
| **Gateway MCP 模式** | 通过 MCPClient 连接 Gateway，调用远程 MCP 工具 | 需要 Gateway 统一管理凭证注入、调用远程服务 |

两种模式可以混合使用——同一个 Agent 可以同时注册本地工具和 Gateway MCP 工具。

Gateway MCP 模式下，WAT 的获取有两种方式：

| WAT 获取方式 | 说明 |
|-------------|------|
| **手动获取** | 从请求上下文中提取 WAT，传入 `MCPClient` 的 `headers` 参数 |
| **SDK 托管（推荐）** | 使用 `BedrockAgentCoreApp` + `BedrockAgentCoreContext.get_workload_access_token()` 自动管理 |

详见[附录 A.7](#a7-agent-runtime-容器代码)。

---

## 8. 安全要点

### 8.1 凭证隔离

- **Inbound JWT**：AgentCore 平台层验证签名，容器可选择不接触 JWT（方式 A）或仅解码 claims（方式 B）
- **Outbound OAuth**：Agent 代码只接触短期 `access_token`，`refresh_token` 留在 Token Vault
- **Gateway**：OAuth token 在 Gateway 层注入下游请求 header，Agent 代码和 LLM 完全不感知

### 8.2 LLM 安全

无论哪种方案，OAuth token 都不会出现在：
- System prompt
- LLM 的 tool input/output
- 对话历史

即使 LLM 被 prompt injection，也无法泄露凭证。

### 8.3 审计

AgentCore Identity 记录所有凭证操作的审计日志：
- 谁（agent identity + user identity）
- 访问了什么（credential provider + scope）
- 什么时候（timestamp）
- 结果（成功/失败）

---

## 附录 A. 端到端实战示例：AgentCore Runtime + Gateway MCP + Lambda

> 本附录给出一个完整的端到端配置参考，覆盖 CDK 基础设施、Gateway Custom Resource Lambda、Agent Runtime 容器代码、以及 Lambda Target 工具代码。

### A.1 整体架构

```
前端用户 (Bearer JWT)
    |
    v
AgentCore Runtime (JWT Authorizer, 共用 userPoolClient)
    |
    | Runtime 转发 header 到容器：
    |   Authorization: 用户原始 JWT
    |
    v
Agent 容器 (MCPClient 调用 Gateway)
    |
    | 转发 Authorization header：
    |   Authorization → Gateway JWT Authorizer 认证
    |
    v
AgentCore Gateway (JWT Authorizer, 共用 userPoolClient)
    |
    | Gateway 使用 GATEWAY_IAM_ROLE 调用 Lambda
    |
    v
Lambda Target (执行实际业务逻辑，返回 MCP tool result)

注：若 Gateway Target 使用 OAUTH 类型（而非 GATEWAY_IAM_ROLE），
    还需传递 WorkloadAccessToken header，Gateway 用 WAT 从 Token Vault 获取下游 OAuth token。
```

### A.2 CDK：Cognito 认证

Cognito 提供一个前端客户端：用户使用 Authorization Code + PKCE 登录，Runtime 和 Gateway 共用同一个 `userPoolClient`。

```typescript
// 省略：用户组、管理员用户、Managed Login Branding 等非 OAuth 相关配置

import { Duration, Aws } from 'aws-cdk-lib';
import {
  OAuthScope, ResourceServerScope,
  UserPool, UserPoolClient,
} from 'aws-cdk-lib/aws-cognito';

export default class CognitoAuthStack extends Construct {
  public readonly userPool: UserPool;
  public readonly userPoolClient: UserPoolClient;   // 前端用户（Runtime + Gateway 共用）
  public readonly cognitoDomain: string;

  constructor(scope: Construct, id: string, props: CognitoAuthProps) {
    super(scope, id);

    this.userPool = new UserPool(this, 'userPool', {
      userPoolName: props.stackName + '-UserPool',
      autoVerify: { email: true },
      signInAliases: { email: true },
      passwordPolicy: {
        minLength: 8, requireLowercase: true, requireUppercase: true,
        requireDigits: true, requireSymbols: true,
      },
    });

    const domainPrefix = props.stackName.toLowerCase().replace(/[^a-z0-9-]/g, '-') + '-' + Aws.ACCOUNT_ID;
    this.userPool.addDomain('portalDomain', { cognitoDomain: { domainPrefix } });
    this.cognitoDomain = `https://${domainPrefix}.auth.${Aws.REGION}.amazoncognito.com`;

    // 前端客户端：Authorization Code + PKCE（Runtime + Gateway 共用）
    this.userPoolClient = new UserPoolClient(scope, 'UserPoolClient', {
      userPool: this.userPool,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [OAuthScope.EMAIL, OAuthScope.OPENID, OAuthScope.PROFILE],
        callbackUrls: [props.portalUrl, 'http://localhost:8080/callback'],
        logoutUrls: [props.portalUrl],
      },
      accessTokenValidity: Duration.minutes(720),
      refreshTokenValidity: Duration.days(30),
    });
  }
}
```

### A.3 CDK：Gateway + Lambda Construct

Gateway Construct 通过 Custom Resource Lambda 管理 Gateway 生命周期，同时创建 Lambda Target 作为 MCP 工具的执行后端。

```typescript
import { Aws, Duration, CustomResource } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';

export default class AgentCoreGatewayLambdaStack extends Construct {
  public readonly gatewayId: string;
  public readonly gatewayUrl: string;

  constructor(scope: Construct, id: string, props: AgentCoreGatewayProps) {
    super(scope, id);

    // 1. Gateway IAM Role（当前示例用 GATEWAY_IAM_ROLE 直调 Lambda，只需 Lambda Invoke 权限）
    // 如果 Target 使用 OAUTH 凭证类型，还需添加 Token Vault 和 SecretsManager 权限
    const gatewayRole = new iam.Role(this, 'AgentCoreGatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      inlinePolicies: {
        AgentCorePolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: 'InvokeLambda',
              actions: ['lambda:InvokeFunction'],
              resources: [`arn:aws:lambda:${Aws.REGION}:${Aws.ACCOUNT_ID}:function:${props.stackName}-*`],
            }),
          ],
        }),
      },
    });

    // 2. Lambda Target
    const lambdaTarget = new lambda.Function(this, 'LambdaTarget', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset(props.lambdaTargetCodePath),
      timeout: Duration.minutes(15),
      memorySize: 1024,
      environment: { LOG_LEVEL: 'INFO', ...props.lambdaEnvironment },
    });

    // 3. Custom Resource Lambda（管理 Gateway 生命周期）
    const crLambda = new lambda.Function(this, 'CustomResourceLambda', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('lambda/gateway-custom-resource'),
      timeout: Duration.minutes(15),
    });
    // crLambda 需要 bedrock-agentcore:*Gateway*, iam:PassRole 等权限（略）

    const provider = new cr.Provider(this, 'Provider', { onEventHandler: crLambda });

    const gatewayResource = new CustomResource(this, 'GatewayResource', {
      serviceToken: provider.serviceToken,
      properties: {
        gatewayName: props.gatewayName,
        gatewayRoleArn: gatewayRole.roleArn,
        discoveryUrl: props.discoveryUrl,
        clientId: props.clientId,
        lambdaArn: lambdaTarget.functionArn,
        timestamp: new Date().toISOString(),
      },
    });

    this.gatewayId = gatewayResource.getAttString('gatewayId');
    this.gatewayUrl = `https://${this.gatewayId}.gateway.bedrock-agentcore.${Aws.REGION}.amazonaws.com/mcp`;
  }
}
```

### A.4 Custom Resource Lambda：创建 Gateway 的 boto3 逻辑

Custom Resource Lambda 在 CDK 部署时被调用，通过 `bedrock-agentcore-control` API 创建 Gateway 资源。

```python
import json, time, boto3, logging
logger = logging.getLogger()
bedrock_agentcore = boto3.client("bedrock-agentcore-control")

def lambda_handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    if request_type == "Create":
        return handle_create(props)
    elif request_type == "Update":
        return handle_update(event, props)
    elif request_type == "Delete":
        return handle_delete(event)

def handle_create(props):
    gateway_name = props["gatewayName"]

    # Step 1: 创建 Gateway（带 JWT Authorizer）
    # clientId 与 Runtime 共用同一个前端客户端（userPoolClient）
    # 容器转发用户原始 JWT 给 Gateway，Gateway 用 Cognito JWKS 验证
    gateway_response = bedrock_agentcore.create_gateway(
        name=gateway_name,
        roleArn=props["gatewayRoleArn"],
        protocolType="MCP",
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": props["discoveryUrl"],
                "allowedClients": [props["clientId"]],
            }
        },
    )
    gateway_id = gateway_response["gatewayId"]
    wait_for_gateway_available(gateway_id)

    # Step 2: 创建 Lambda Target（绑定 MCP 工具 schema）
    tool_schema = [
        {
            "name": "guide_claim_submission",
            "description": "Guide the user through the insurance claim submission process",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "claim_type": {"type": "string", "description": "Type of claim"},
                    "user_id": {"type": "string", "description": "User ID"},
                },
                "required": ["claim_type"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        },
        # ... 更多工具定义
    ]

    target_response = bedrock_agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="claims-target",
        description="Lambda target for claims tools",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": props["lambdaArn"],
                    "toolSchema": {"inlinePayload": tool_schema},
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )

    physical_id = f"{gateway_id}#{target_response['targetId']}"
    return {"PhysicalResourceId": physical_id, "Data": {"gatewayId": gateway_id}}

def handle_update(event, props):
    """
    Update 逻辑：删除旧资源 + 创建新资源。
    ⚠️ 会导致 Gateway 短暂不可用（downtime），生产环境应改用增量更新
    （update_gateway / update_gateway_target）。
    """
    physical_id = event.get("PhysicalResourceId", "")
    try:
        handle_delete(event)
    except Exception as e:
        logger.warning(f"Cleanup during update failed (may be OK): {e}")
    return handle_create(props)

def handle_delete(event):
    """
    删除 Gateway 及关联资源。
    注意：需按 target → gateway → credential provider 的顺序删除。
    """
    physical_id = event.get("PhysicalResourceId", "")
    if "#" not in physical_id:
        logger.warning(f"Cannot parse physical_id: {physical_id}, skipping delete")
        return {"PhysicalResourceId": physical_id}

    gateway_id, target_id = physical_id.split("#", 1)
    try:
        bedrock_agentcore.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except Exception as e:
        logger.warning(f"Delete target failed: {e}")
    try:
        bedrock_agentcore.delete_gateway(gatewayIdentifier=gateway_id)
    except Exception as e:
        logger.warning(f"Delete gateway failed: {e}")
    return {"PhysicalResourceId": physical_id}

def wait_for_gateway_available(gateway_id, max_wait=300):
    elapsed = 0
    while elapsed < max_wait:
        resp = bedrock_agentcore.get_gateway(gatewayIdentifier=gateway_id)
        if resp["status"] in ["AVAILABLE", "READY"]:
            return
        time.sleep(5)
        elapsed += 5
    raise TimeoutError(f"Gateway {gateway_id} not available within {max_wait}s")
```

### A.5 CDK：AgentCore Runtime

Runtime 通过 CDK L1 `CfnRuntime` 创建，关键配置：JWT Authorizer 和 `MCP_SERVER_URL` 环境变量。

> 实际部署还需要 CloudWatch Logs、X-Ray tracing、DynamoDB 等额外 IAM 权限，此处仅展示 OAuth 相关的核心配置。

```typescript
import { Aws, aws_bedrockagentcore as agentcore_cfn } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';

export default class AgentCoreRuntimeCdkStack extends Construct {
  public readonly agentRuntimeArn: string;

  constructor(scope: Construct, id: string, props: AgentCoreRuntimeProps) {
    super(scope, id);

    const agentCoreRole = new iam.Role(this, 'AgentCoreRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      inlinePolicies: {
        AgentCorePolicy: new iam.PolicyDocument({
          statements: [
            // ECR 拉取镜像
            new iam.PolicyStatement({
              actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
              resources: [`arn:aws:ecr:${Aws.REGION}:${Aws.ACCOUNT_ID}:repository/*`],
            }),
            new iam.PolicyStatement({
              actions: ['ecr:GetAuthorizationToken'],
              resources: ['*'],
            }),
            // Bedrock 模型调用
            new iam.PolicyStatement({
              actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
              resources: ['arn:aws:bedrock:*::foundation-model/*'],
            }),
            // Token Vault（Outbound OAuth / Gateway WAT 传递）
            new iam.PolicyStatement({
              sid: 'GetAgentAccessToken',
              actions: [
                'bedrock-agentcore:GetWorkloadAccessToken*',
                'bedrock-agentcore:CreateWorkloadIdentity',
                'bedrock-agentcore:GetResourceOauth2Token',
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*`,
                `arn:aws:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:token-vault/default`,
                `arn:aws:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:token-vault/default/oauth2credentialprovider/*`,
              ],
            }),
            // SecretsManager
            new iam.PolicyStatement({
              actions: ['secretsmanager:GetSecretValue'],
              resources: [
                `arn:aws:secretsmanager:${Aws.REGION}:${Aws.ACCOUNT_ID}:secret:bedrock-agentcore-identity!default/oauth2/*`,
              ],
            }),
          ],
        }),
      },
    });

    const cfnRuntime = new agentcore_cfn.CfnRuntime(this, 'Runtime', {
      agentRuntimeName: props.agentName,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: props.containerUri,
        },
      },
      networkConfiguration: { networkMode: 'PUBLIC' },
      protocolConfiguration: { protocol: 'HTTP' },
      roleArn: agentCoreRole.roleArn,
      authorizerConfiguration: {
        customJwtAuthorizer: {                      // ← CDK L1: 小写 wt
          discoveryUrl: props.discoveryUrl,
          allowedClients: [props.clientId],
        },
      },
      environmentVariables: {
        MCP_SERVER_URL: props.mcpServerUrl,
        OAUTH_PROVIDER_NAME: props.oauthProviderName || 'oauth',
        AWS_REGION: Aws.REGION,
      },
    });

    this.agentRuntimeArn = cfnRuntime.attrAgentRuntimeArn;
  }
}
```

### A.6 CDK：组装层

在顶层 Stack 中将 Cognito、Gateway、Runtime 串联起来。Runtime 和 Gateway **共用同一个 Cognito App Client**（`userPoolClient`）——容器将用户原始 JWT 转发给 Gateway 认证，无需额外的 M2M 客户端。

```typescript
// 1. Cognito 认证
const auth = new CognitoAuthStack(this, 'Auth', { stackName, adminEmail, portalUrl });

const discoveryUrl = `https://cognito-idp.${Aws.REGION}.amazonaws.com/${auth.userPool.userPoolId}/.well-known/openid-configuration`;
const clientId = auth.userPoolClient.userPoolClientId;  // Runtime + Gateway 共用

// 2. Gateway + Lambda（MCP 工具后端）
const claimsGateway = new AgentCoreGatewayLambdaStack(this, 'ClaimsGateway', {
  stackName,
  gatewayName: `${stackName}-claims-gateway`,
  discoveryUrl,
  clientId,                                             // 与 Runtime 同一个客户端
  lambdaTargetCodePath: 'lambda/claims-tools',
});

// 3. AgentCore Runtime（连接 Gateway）
const claimsRuntime = new AgentCoreRuntimeCdkStack(this, 'ClaimsRuntime', {
  agentName: `${stackName}-claims-agent`,
  containerUri: claimsImage.imageUri,
  discoveryUrl,
  clientId,                                             // 与 Gateway 同一个客户端
  mcpServerUrl: claimsGateway.gatewayUrl,
});
```

### A.7 Agent Runtime 容器代码

#### 本地工具模式：FastAPI + 本地工具（不经过 Gateway）

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from strands import Agent
from strands.models import BedrockModel

app = FastAPI()

TOOL_REGISTRY = {
    "guide_claim_submission": guide_claim_submission,
    "get_claim_checklist": get_claim_checklist,
}

@app.post("/invocations")
async def stream_agent(request: ChatRequest, http_request: Request):
    session_id = request.id
    user_id = request.user_id

    agent = Agent(
        system_prompt=agent_system_prompt,
        model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        tools=list(TOOL_REGISTRY.values()),
    )

    async def event_generator():
        async for event in agent.stream_async(user_message):
            if "delta" in event and "text" in event["delta"]:
                yield f"data: {json.dumps({'type': 'text-delta', 'delta': event['delta']['text']})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

#### Gateway MCP 模式

Agent 容器通过 `MCP_SERVER_URL` 环境变量连接 Gateway。需要传递的 header 取决于 Gateway Target 类型（详见 1.3 节）：

| Target 类型 | 需要的 Header |
|---|---|
| `GATEWAY_IAM_ROLE`（本示例） | `Authorization`（用户 JWT） |
| `OAUTH` | `Authorization` + `WorkloadAccessToken`（WAT） |

**手动获取：FastAPI + MCPClient**

```python
import os
from fastapi import Request
from strands import Agent
from strands.models import BedrockModel
from strands_mcp import MCPClient

@app.post("/invocations")
async def handle(http_request: Request):
    # Runtime 转发的用户原始 Cognito JWT（需配置 request-header-allowlist）
    user_jwt = http_request.headers.get("Authorization")

    mcp_client = MCPClient(
        transport="streamable_http",
        url=os.environ["MCP_SERVER_URL"],
        headers={
            "Authorization": user_jwt,  # Gateway JWT Authorizer 认证
        },
    )

    with mcp_client:
        agent = Agent(
            system_prompt="你是保险理赔助手...",
            model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
            tools=[mcp_client],
        )
        result = agent("帮我提交一个医疗理赔")
```

**SDK 托管：BedrockAgentCoreApp（推荐）**

```python
import os
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from strands import Agent
from strands_mcp import MCPClient

app = BedrockAgentCoreApp()

@app.entrypoint
async def handle_request(request):
    # SDK 自动从 Runtime 注入的 header 中提取并存入上下文
    headers = BedrockAgentCoreContext.get_request_headers() or {}

    mcp_client = MCPClient(
        transport="streamable_http",
        url=os.environ["MCP_SERVER_URL"],
        headers={
            "Authorization": headers.get("Authorization", ""),  # 用户原始 JWT
        },
    )

    with mcp_client:
        agent = Agent(
            system_prompt="...",
            model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
            tools=[mcp_client],
        )
        return agent.stream(request.prompt)

app.run()
```

### A.8 Lambda Target 代码

Lambda 接收 Gateway 转发的 MCP 工具调用请求：

```python
import json, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    event 结构:
    {
        "tool_name": "guide_claim_submission",
        "tool_input": { "claim_type": "medical", "user_id": "user-123" }
    }
    """
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    handlers = {
        "guide_claim_submission": handle_guide_claim,
        "classify_document": handle_classify_document,
    }

    handler = handlers.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}
    return handler(tool_input)

def handle_guide_claim(input_data):
    claim_type = input_data.get("claim_type", "")
    return {
        "message": f"开始{claim_type}理赔引导流程...",
        "steps": ["上传证明材料", "填写理赔信息", "提交审核"],
    }

def handle_classify_document(input_data):
    document_key = input_data.get("document_key", "")
    return {"message": f"文档 {document_key} 已分类", "category": "medical_report"}
```

### A.9 端到端认证链路

```
前端用户
  | Bearer JWT (Cognito access_token, Authorization Code + PKCE)
  v
AgentCore Runtime (JWT Authorizer, allowedClients=[userPoolClient])
  | 验证用户 JWT 签名 + claims
  | 转发到容器：Authorization header（原始 JWT）
  v
Agent 容器 (/invocations)
  | MCPClient 连接 Gateway，转发 Authorization header：
  |   Authorization: Bearer {用户原始 JWT} → Gateway JWT Authorizer 认证
  v
AgentCore Gateway (JWT Authorizer, allowedClients=[userPoolClient]，与 Runtime 共用)
  | JWT Authorizer 验证用户原始 JWT（Cognito JWKS 验签 + client_id 校验）
  | 确定目标 Lambda + credentialProviderConfiguration
  |
  |--- GATEWAY_IAM_ROLE（本示例）: 直接用 Gateway 自身 IAM Role 调 Lambda
  |--- OAUTH: 需额外传递 WorkloadAccessToken header，
  |           Gateway 用 WAT 从 Token Vault 获取目标服务 OAuth token 注入下游请求
  v
Lambda Target
  | 执行工具逻辑
  | 返回 MCP tool result → Gateway → Agent → 用户
```

### A.10 CDK 属性命名速查

| 位置 | JWT Authorizer 属性名 | 说明 |
|------|----------------------|------|
| CDK L1 `CfnRuntime` | `customJwtAuthorizer`（小写 wt） | TypeScript 驼峰命名 |
| boto3 API（create_agent_runtime） | `customJWTAuthorizer`（大写 JWT） | API 原始命名 |
| boto3 API（create_gateway） | `customJWTAuthorizer`（大写 JWT） | API 原始命名 |

两者的内部结构一致：`{ discoveryUrl, allowedClients, allowedAudience?, allowedScopes?, customClaims? }`。
