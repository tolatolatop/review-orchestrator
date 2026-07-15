# 新平台接入与验证手册

本文用于把 GitMilk、Forgejo、Azure DevOps 或其他代码托管平台接入 Review
Orchestrator。目标是在不修改 Orchestrator、Worker、Workspace、Delivery Outbox 和
数据库模型的前提下，完成平台构造、协议转换、权限诊断和端到端验收。

## 1. 接入层级

新平台可以按两个层级接入：

| 层级 | 必需实现 | 可使用的入口 |
| --- | --- | --- |
| Provider Core | Platform、Provider 四类转换、Plugin/Factory | 四个 `/v1` 接口 |
| 完整自动评审 | Provider Core，加上 Orchestrator capability | Webhook inbox、Worker、Workspace、Delivery |

只通过 HTTP 使用标准化能力时，实现 Provider Core 即可。需要参与自动评审闭环时，
还要实现第 6 节列出的细粒度 capability。

## 2. 三层职责

```text
平台 SDK / 原生 HTTP API
          |
          v
Platform：Client、配置、凭据、原生 API、关闭
          |
          v
Provider：Webhook、Git、Comment、Query 协议转换
          ^
          |
Registry：配置选择、Factory 构造、注册、路由、统一关闭
```

边界规则：

- Platform 可以持有平台 token、SDK Client 或 `httpx.AsyncClient`。
- Provider 只持有注入的 Platform，不读取环境变量、不创建 Client、不保存 token。
- Registry/Factory 是配置对象和运行时资源的组合根。
- Provider Core 请求只包含 provider、目标和业务参数，不包含平台凭据。
- Orchestrator 不按平台名称增加 `if github`、`if gitlab` 一类分支。

公共合约位于 `src/review_orchestrator/integrations/providers.py`。

## 3. 配置与文件结构

仓库内置平台建议使用：

```text
src/review_orchestrator/integrations/{platform}.py
tests/fixtures/{platform}/webhooks/
tests/fixtures/{platform}/api/
tests/test_{platform}_provider.py
```

先定义平台专属配置，不要让 Provider 直接读取环境变量：

```python
@dataclass(frozen=True)
class GitMilkConfig:
    api_base_url: str
    api_token: str
    webhook_secret: str
    timeout: float = 30.0
```

内置平台应把字段加入 `Settings`。外部插件可以在 Plugin/Factory 中用自己的
`BaseSettings` 构造配置。必需配置缺失时应在 Factory 阶段失败。

## 4. 实现 Platform

Platform 至少实现 `key`、`get_credential()` 和 `aclose()`，并提供 Provider 所需的
平台原生 API：

```python
class GitMilkPlatform:
    key = "gitmilk"

    def __init__(self, client, config: GitMilkConfig) -> None:
        self.client = client
        self.config = config

    async def get_credential(self, target: str, scope: str) -> Credential:
        if scope == "webhook":
            return Credential(value=self.config.webhook_secret)

        if scope == "git:read":
            token = await self._create_git_token(target)
            return Credential(
                value=token.value,
                username="oauth2",
                expires_at=token.expires_at,
            )

        if scope in {"comment:write", "query:read"}:
            return Credential(value=self.config.api_token)

        raise ProviderCapabilityError(
            f"Unsupported credential scope: {scope}",
            provider=self.key,
            operation="get_credential",
        )

    async def get_pull_request(self, repository: str, number: int) -> dict:
        ...

    async def list_changes(self, repository: str, number: int) -> list[dict]:
        ...

    async def aclose(self) -> None:
        await self.client.aclose()
```

首版 scope 固定为：

- `webhook`
- `git:read`
- `comment:write`
- `query:read`

临时 token 的申请、缓存和刷新完全留在 Platform 内，不能写入数据库。

## 5. 实现 Provider Core

Provider 必须声明稳定的 `key`，并实现四个异步方法：

```python
class GitMilkProvider:
    key = "gitmilk"
    provider = "gitmilk"  # 兼容现有 capability 协议

    def __init__(self, platform: GitMilkPlatform) -> None:
        self.platform = platform

    async def normalize_webhook(self, headers, raw_body): ...
    async def resolve_git_checkout(self, request): ...
    async def publish_comments(self, request): ...
    async def query(self, request): ...
```

### 5.1 Webhook

Provider 从 Platform 获取 `webhook` credential，然后完成 Header 和签名处理、
Delivery ID 提取、payload 校验、内部 `pr_*` 事件映射和
`PullRequestSnapshot` 构造。

纯解析和映射应抽成共享 helper，供 Provider Core 的 `normalize_webhook()` 和
Orchestrator 兼容能力 `parse_webhook()` 复用。

### 5.2 Git checkout

```python
async def resolve_git_checkout(self, request):
    credential = await self.platform.get_credential(
        request.repository,
        "git:read",
    )
    return GitCheckoutTarget(
        remote_url=request.clone_url or self.platform.clone_url(request.repository),
        username=credential.username,
        password=credential.value or None,
        expires_at=credential.expires_at,
    )
```

### 5.3 Comment

统一支持 `summary`、`line` 和 `agent`。需要处理创建、更新、
`idempotency_key`、comment/thread ID、URL、批量部分失败和错误脱敏。

Line comment 必须使用平台真实 diff position。无法安全构造时，应返回该条失败或执行
明确的 summary-only 降级，不能伪造行位置。

### 5.4 Query

只接受：

- `pull_request.get`
- `pull_request.changes.list`
- `pull_request.comments.list`
- `pull_request.status.get`

Provider 应把平台原生字段转换为统一字段，并实现 `cursor` 和 `page_size`。

## 6. 完整自动评审 capability

平台要进入自动评审闭环时，同一个 Provider 按需要实现：

- `WebhookCapability`
- `WorkspaceCheckoutCapability`
- `PullRequestCapability`
- `ChangedFilesCapability`
- `ReviewSummaryCapability`
- `LineCommentsCapability`
- `AgentTaskCommentsCapability`
- `PlatformDiagnosticsCapability`
- `ResourceLinksCapability`

这些兼容方法应委托给同一个 Platform 和转换 helper，不应创建第二套 Client 或凭据
逻辑。

## 7. Factory、Plugin 和注册

Factory 是唯一的运行时组合点：

```python
class GitMilkProviderPlugin:
    provider = "gitmilk"
    kind = "gitmilk"
    display_name = "GitMilk"

    def build(self, context: ProviderBuildContext) -> ProviderRuntime:
        config = GitMilkConfig.from_settings(context.settings)
        client = httpx.AsyncClient(
            base_url=config.api_base_url,
            timeout=config.timeout,
        )
        platform = GitMilkPlatform(client, config)
        provider = GitMilkProvider(platform)

        return ProviderRuntime(
            provider=provider,
            descriptor=ProviderDescriptor(
                key="gitmilk",
                kind="gitmilk",
                display_name="GitMilk",
            ),
            close=platform.aclose,
        )
```

Plugin、Platform、Provider、Descriptor 的 key 必须一致。

外部包通过 entry point 注册：

```toml
[project.entry-points."review_orchestrator.providers"]
gitmilk = "company_gitmilk.plugin:GitMilkProviderPlugin"
```

部署时启用：

```env
PROVIDERS_ENABLED=github,gitlab,gitmilk
```

## 8. 复用现有平台权限诊断

可以复用 `POST /api/v1/diagnostics/platform-permissions`。该接口通过 Registry 查找
`PlatformDiagnosticsCapability`，不要求 provider 必须是 GitHub 或 GitLab。

不要混淆三个“查询”入口：

| 入口 | 用途 | 是否能证明权限 |
| --- | --- | --- |
| `GET /api/v1/providers` | 查看注册结果和 capability 清单 | 不能，只证明运行时已注册 |
| `POST /v1/query/{provider}` | 查询 PR、变更、评论和状态 | 只能证明这次具体读取成功 |
| `POST /api/v1/diagnostics/platform-permissions` | 汇总凭据、scope、角色和只读探测 | 可以，是推荐的权限验收入口 |

`/v1/query/{provider}` 仍应纳入合约验证，但它不能代替权限诊断：它不返回完整的
scope/role/check 清单，也不能区分“平台未公开写权限信息”和“明确没有写权限”。

新 Provider 需要实现：

```python
async def diagnose_permissions(self, payload):
    return await self.platform.diagnose_permissions(payload)
```

Platform 的诊断实现只执行非破坏性请求，并返回标准 checks：

| Check | 验证内容 |
| --- | --- |
| `token_configured` 响应字段 | 凭据是否存在或能否成功签发 |
| `repository_read` | 能否读取目标仓库 |
| `pull_request_read` | 能否读取指定 PR/MR；未提供编号时为 `skipped` |
| `summary_comment_write` | scope/role 是否能证明普通评论写权限 |
| `line_comment_write` | scope/role 是否能证明行评论或 discussion 写权限 |

Check 状态为 `passed`、`failed`、`unknown` 或 `skipped`。如果平台的只读 API 无法
证明写权限，应返回 `unknown`，不能为了诊断而创建测试评论。

权限诊断可以验证注册、凭据、API 连通性、仓库/PR 读取、平台报告的 scope、角色和
rate limit。它不能验证公网 Webhook 投递、真实 clone/fetch、未公开的写权限、评论
diff position 或完整 Worker/Delivery 闭环。因此它是必要验证，但不能替代合约测试和
端到端测试。

## 9. 部署后验证

以下示例假设：

```bash
export BASE_URL=http://localhost:8000
export PROVIDER=gitmilk
export REPOSITORY=group/project
export PR_NUMBER=42
export PROVIDER_CORE_API_TOKEN=core-secret
```

通过 self-host Nginx 访问时，还需要发送
`X-Review-Token: $REVIEW_PROXY_TOKEN`。

### 9.1 注册和 capability

```bash
curl -sS "$BASE_URL/api/v1/providers"
```

检查目标 provider 的 key、kind 和 capability。实现权限诊断后应包含
`diagnostics`。未出现通常表示 `PROVIDERS_ENABLED`、entry point 或 Factory 失败。

### 9.2 只读权限诊断

```bash
curl -sS "$BASE_URL/api/v1/diagnostics/platform-permissions" \
  -H 'Content-Type: application/json' \
  -d "{
    \"provider\": \"$PROVIDER\",
    \"repo_full_name\": \"$REPOSITORY\",
    \"pull_request_number\": $PR_NUMBER
  }"
```

也可以使用仓库脚本：

```bash
REVIEW_ORCHESTRATOR_URL="$BASE_URL" \
uv run python scripts/check_platform_permissions.py \
  --provider "$PROVIDER" \
  --repository "$REPOSITORY" \
  --pull-request "$PR_NUMBER"
```

脚本接受任意合法 provider key。通过 Nginx 时设置 `REVIEW_PROXY_TOKEN`，脚本会自动
发送代理 Header。退出码为 `0=healthy`、`1=degraded`、`2=failed`、`3=请求或响应
合约错误`。

已注册但没有 diagnostics capability 时接口返回 `503`；未注册时返回 `404`。

### 9.3 Webhook 标准化

Header 和签名算法由平台决定，下面的 Header 需要替换：

```bash
curl -sS "$BASE_URL/v1/webhooks/$PROVIDER/normalize" \
  -H "Authorization: Bearer $PROVIDER_CORE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'X-Platform-Delivery: delivery-1' \
  -H 'X-Platform-Event: pull_request' \
  -H 'X-Platform-Signature: <fixture-signature>' \
  --data-binary @tests/fixtures/gitmilk/webhooks/pr_opened.json
```

检查 delivery ID、internal event、repository、PR number、head SHA 和 snapshot。
错误签名必须返回 `401`。

### 9.4 Checkout

```bash
curl -sS "$BASE_URL/v1/git/$PROVIDER/resolve-checkout" \
  -H "Authorization: Bearer $PROVIDER_CORE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"repository\": \"$REPOSITORY\"}"
```

检查 `remote_url`、`username` 和 `expires_at`。响应可能包含临时 password，不要写入
CI 日志。要验证凭据能否真正 clone/fetch，优先在隔离环境调用
`POST /api/v1/workspaces/prepare`；Workspace 响应不会返回平台 token。

### 9.5 Query

分别调用四种 action：

```bash
curl -sS "$BASE_URL/v1/query/$PROVIDER" \
  -H "Authorization: Bearer $PROVIDER_CORE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{
    \"action\": \"pull_request.status.get\",
    \"repository\": \"$REPOSITORY\",
    \"pull_request_number\": $PR_NUMBER,
    \"page_size\": 20
  }"
```

验证统一字段和分页 cursor。未知 action 应被拒绝。

### 9.6 Comment

Comment 验证会修改平台数据，只能在沙箱仓库执行：

```bash
curl -sS "$BASE_URL/v1/comments/$PROVIDER/publish" \
  -H "Authorization: Bearer $PROVIDER_CORE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{
    \"repository\": \"$REPOSITORY\",
    \"pull_request_number\": $PR_NUMBER,
    \"kind\": \"summary\",
    \"body\": \"Provider integration verification\",
    \"idempotency_key\": \"provider-verification-1\"
  }"
```

重复执行后应返回同一 comment ID，且不产生重复评论。然后分别验证 update、line、
agent 和批量部分失败。

### 9.7 完整 Orchestrator 闭环

完整接入还要验证：

1. 向 `/api/v1/webhooks/{provider}` 发送真实格式 fixture。
2. Provider Event Inbox 中的 delivery 保持幂等。
3. 正确创建或更新 `PullRequestContext` 和 ReviewRun。
4. Worker 通过 Provider 准备 Workspace 并查询 changed files。
5. Delivery 发布 summary、line 或 agent comment。
6. 重复 delivery 不产生重复平台评论。

## 10. 自动化测试要求

### Platform

- 四个 scope 的 credential 行为。
- 静态凭据、临时 Git token、过期刷新和并发缓存。
- API 错误到安全异常的转换。
- Registry 调用 `aclose()` 且幂等。

### Provider

- 正确和错误 Webhook 签名、忽略事件、非法 payload。
- Checkout URL、username、password、expires_at。
- 四种 Query action 和分页。
- Comment create、update、idempotency、line position、部分失败。
- 错误不包含 token、Authorization Header 或上游响应正文。

### HTTP 和 Registry

- Provider Core token 未配置返回 `503`。
- Bearer 缺失或错误返回 `401`。
- 请求携带平台 token 字段返回 `422`，且不回显其值。
- 未注册 provider 返回 `404`，重复 key 启动失败。
- `/api/v1/providers` capability 正确。
- 权限诊断覆盖 healthy、degraded、failed 和 `unknown` 写权限。

### 回归命令

```bash
uv run ruff check src tests scripts
uv run pytest -q

cd pi-agent-runtime
npm ci
npm test

cd ..
docker compose -f docker-compose.yaml config --quiet
docker compose -f docker-compose.self_host.yaml config --quiet
```

默认测试不得依赖真实平台或网络；真实平台验证应使用显式启用的沙箱测试。

## 11. 验收矩阵

| 项目 | 自动化验证 | 部署后验证 | 通过标准 |
| --- | --- | --- | --- |
| 注册 | Registry/Plugin 单测 | `/api/v1/providers` | key 唯一、capability 正确 |
| 凭据 | Platform 单测 | 权限诊断 | 不泄漏、按 scope 获取 |
| Webhook | Fixture/签名单测 | normalize + inbox | 映射正确、delivery 幂等 |
| Git | Checkout 合约测试 | Workspace prepare | clone/fetch 成功、响应无 token |
| Query | 四 action 合约测试 | `/v1/query` | 字段统一、分页正确 |
| Comment | Fake Client 合约测试 | 沙箱评论 | 创建、更新、幂等、line 正确 |
| 生命周期 | Registry 单测 | 应用关闭 | Client 只关闭一次 |
| 安全 | 错误/日志测试 | 失败请求抽查 | token 不出现在请求、日志和异常中 |
| 完整评审 | E2E fixture | 沙箱 PR/MR | ReviewRun 到 Delivery 闭环成功 |

## 12. 常见问题

- 权限诊断 `503`：Provider 已注册，但没有 diagnostics capability，或其配置不可用。
- 权限诊断 `degraded`：逐项查看 check；`unknown` 不等同于无权限，应在沙箱做最小
  写入验证。
- Query 成功但 Workspace 失败：`query:read` 和 `git:read` 可能使用不同 scope，
  检查 clone URL、自托管证书和临时 token 过期时间。
- 评论重复：检查 idempotency marker 查询、comment/thread ID 和 update API 映射。
- 错误包含敏感信息：平台 Client 不应把上游正文、Authorization Header 或含凭据的
  URL 写入异常。
