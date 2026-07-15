# 平台 Provider 扩展指南

Review Orchestrator 以 GitHub 作为参考 provider，并已包含初步的 GitLab 实现。
数据模型、API schema、webhook 接入和 worker 操作都携带 `provider` 字段，并通过
provider adapter 路由平台行为。本文说明如何在不破坏现有 provider 的前提下，
扩展到 Azure DevOps、Bitbucket、GitCode 或其他代码托管平台。

目标形态是在外部平台行为周围建立一个小而清晰的 provider adapter 边界。Review
run 生命周期、pi-agent session 编排、workspace 准备、结果解析、finding 对账和
重试策略应继续保持 provider-agnostic。

## Platform、Provider、Registry 三层架构

平台集成拆成三个明确层次：

```text
Provider Core HTTP -> Provider 协议转换 -> Platform API/client/鉴权
                                              ^
                                              |
                                  Registry 构造与生命周期
```

- `Platform` 持有 SDK 或 HTTP client、平台原生 API、平台配置，以及
  `get_credential(target, scope)`。首版 scope 为 `webhook`、`git:read`、
  `comment:write`、`query:read`。
- `Provider` 只负责 webhook 标准化、Git checkout 解析、评论发布转换和类型化
  query 转换。它接收 Platform 注入，不读取环境变量，也不创建 client。
- `ProviderRegistry` 选择启用的插件、构造 Platform/Provider runtime、检查重复
  key、按 key 路由，并在应用关闭时统一关闭所拥有的 Platform client。

Provider Core 提供四个 Bearer 保护的入口：

- `POST /v1/webhooks/{provider}/normalize`
- `POST /v1/git/{provider}/resolve-checkout`
- `POST /v1/comments/{provider}/publish`
- `POST /v1/query/{provider}`

请求只包含目标和业务参数，不包含平台凭据。`PROVIDER_CORE_API_TOKEN` 保护该边界，
未配置时四个接口 fail closed。现有 `/api/v1/webhooks/{provider}` 继续作为
Orchestrator inbox 入口，负责持久化标准化事件。

## 当前 GitHub MVP

GitHub 支持当前覆盖：

- `POST /api/v1/webhooks/github`，使用 `X-GitHub-Delivery`、
  `X-GitHub-Event`，并在配置 webhook secret 时校验
  `X-Hub-Signature-256`。
- 标准化 `pull_request`、`issue_comment`、`pull_request_review` 和
  `pull_request_review_comment` 事件。
- 对 PR `opened`、`synchronize`、`reopened` 和 `ready_for_review` 创建
  review run。
- 对受支持的 pull request 状态和元数据变更更新 PR context。
- 当 PR comment 或 review 中提到配置的 review bot login 时，创建直接回答该消息的
  message-command AgentTask；它不会创建 ReviewRun。
- 通过 `(provider, delivery_id)` 实现 provider event 幂等。
- 对 PR context、review run、comment ref、workspace 和 review config 使用
  provider 维度隔离存储。
- 通过 `ReviewCommentRef` 跟踪 summary comment 和 line comment 引用。
- 当 finding 无法映射到 changed file 中可评论的行时，降级为 summary-only。

分层实现说明：

- 每个 Provider 负责所属平台的请求头、签名格式、事件名和 payload 映射；Platform
  负责 client 与认证设置。
- Workspace clone URL 和凭据准备统一通过 Provider checkout 合约与 Platform scope
  凭据完成。
- GitHub 声明 line-comment 能力；GitLab 不声明该能力，只发布 summary。
- Review thread resolve 和 provider-specific rate-limit backoff 尚未进入最小
  adapter 契约。

核心现在按能力协议路由。平台只实现自己支持的操作；缺失必需能力会明确失败，缺失
可选行评论能力则记录 summary-only 降级告警。

## Provider 与 Platform 边界

Provider 负责协议转换：

- Webhook 请求头校验和签名验证。
- 原始 payload 解析和 delivery ID 提取。
- 将事件标准化为下文列出的内部事件名。
- Pull request 或 merge request metadata 提取。
- Changed file 和 diff metadata 转换。
- Line-level finding 的可评论位置映射。
- Summary、line、agent comment 请求转换。
- 平台支持时的 review thread 状态转换。
- 将平台原生异常转换为统一 capability/operation 错误。

Platform 负责原生集成：

- SDK 或 HTTP client 的构造与生命周期。
- 原生 PR/MR、change、comment、status API 调用。
- Rate limit、retry-after、permission、not-found 处理。
- 按 scope 获取、刷新和缓存静态或临时凭据。

Orchestrator core 负责共享行为：

- Provider event inbox 幂等和 coalescing。
- `PullRequestContext`、`ReviewRun`、`Finding`、`ReviewCommentRef`、
  `ReviewConfig` 和 `Workspace` 持久化。
- Review run retry、cancel、supersede、timeout 和生命周期状态。
- pi-agent session start/sync/cancel 与人工输入。
- Review result schema 校验和 fingerprint 生成。
- 对无法发布为 line comment 的 finding 执行 summary-only 降级。

## 插件与能力契约

Provider Core 合约提供稳定的 `key` 和四个操作：

```python
class Provider(Protocol):
    key: str

    async def normalize_webhook(self, headers, raw_body): ...
    async def resolve_git_checkout(self, request): ...
    async def publish_comments(self, request): ...
    async def query(self, request): ...
```

现有 Orchestrator 路径继续保留细粒度、可运行时检查的能力协议，兼容 Worker、
Workspace、Delivery 和 diagnostics：

```python
class ProviderAdapter(Protocol):
    provider: str

class WebhookCapability(ProviderAdapter, Protocol):
    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook: ...

class WorkspaceCheckoutCapability(ProviderAdapter, Protocol):
    async def get_workspace_checkout(
        self, repo_full_name: str, *, clone_url: str | None = None
    ) -> ProviderWorkspaceCheckout: ...

class PullRequestCapability(ProviderAdapter, Protocol):
    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext | None: ...

class ChangedFilesCapability(ProviderAdapter, Protocol):
    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]: ...

class ReviewSummaryCapability(ProviderAdapter, Protocol):
    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None: ...

class LineCommentsCapability(ProviderAdapter, Protocol):
    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]: ...
```

另外还有 AgentTask 评论、权限诊断和资源链接等可选协议。Registry 通过
`capability()` 和 `require_capability()` 查询能力，应用层不再用 `hasattr` 猜测。

Webhook adapter 必须为触发 review 的事件附带完整 `PullRequestSnapshot`。业务服务
只持久化归一化快照，不读取平台 Payload 路径，因此接入新平台不需要修改
`services.py`。

插件负责构造及生命周期：

```python
class ProviderPlugin(Protocol):
    provider: str
    kind: str
    display_name: str

    def build(self, context: ProviderBuildContext) -> ProviderRuntime: ...
```

API 和 Worker 都调用 `create_provider_registry(settings)`。只有
`PROVIDERS_ENABLED` 中的插件会被构造；设置为 `gitlab` 时不会创建或校验 GitHub。
`ProviderRegistry.aclose()` 统一关闭插件拥有的 Client。

外部包可以直接注册，无需修改本仓库：

```toml
[project.entry-points."review_orchestrator.providers"]
forge = "company_forge.plugin:ForgePlugin"
```

缺失必需操作时 adapter 抛出 `ProviderCapabilityError`；平台 SDK 失败统一转换为带
`provider` 和 `operation` 属性的 `ProviderOperationError`。

无论外部平台使用什么术语，内部都使用这些数据形状：

| 内部形状 | 必需字段 | 说明 |
| --- | --- | --- |
| `ProviderWebhookEvent` | 事件标识、内部动作标志、可选 `pull_request` | 原始 Payload 只用于审计；核心编排使用归一化字段。 |
| `PullRequestSnapshot` | 仓库、编号、head SHA、refs、作者、状态和 URL | 不透明的平台仓库/PR ID 仍分别保存。 |
| `ProviderChangedFile` | `path`, `status`, `patch`, `commentable_lines`, `provider_position` | `commentable_lines` 是 orchestrator 面向内部的发布门禁。`provider_position` 可保存 GitLab diff position 或 Azure thread context。 |
| `ProviderCommentRef` | `provider_comment_id`, `provider_thread_id`, `comment_type`, `status` | 外部 ID 按字符串保存，不要假设它们是 GitHub 数字 ID。 |

## 事件映射

将 provider 事件标准化为一组小的内部词汇。未知 action 应作为 ignored inbox event
保存，而不是让 webhook 失败；除非 payload 无效或未通过认证。

| 内部事件 | GitHub | GitLab | Azure DevOps | Bitbucket / GitCode 指引 |
| --- | --- | --- | --- | --- |
| `pr_opened` | `pull_request.opened` | `Merge Request Hook` 中的 `open` 或 `opened` | `git.pullrequest.created` | PR 创建或打开事件。 |
| `pr_updated` | `pull_request.synchronize` | MR update 且 source SHA 改变 | `git.pullrequest.updated` 且 source commit 改变 | Source branch commit 改变。 |
| `pr_reopened` | `pull_request.reopened` | MR reopened | Pull request reactivated | 从 declined/closed 重新打开。 |
| `pr_closed` | 未 merge 的 `pull_request.closed` | MR closed | Pull request abandoned/closed | 未 merge 即关闭。 |
| `pr_merged` | `merged=true` 的 `pull_request.closed` | MR merged | Pull request completed | 如果有 merged/completed timestamp，应使用它确认。 |
| `pr_ready_for_review` | `pull_request.ready_for_review` | Draft flag 变为 false | Draft 支持因平台而异 | 不支持时保持 unmapped。 |
| `pr_converted_to_draft` | `pull_request.converted_to_draft` | Draft flag 变为 true | Draft 支持因平台而异 | 不支持时保持 unmapped。 |
| `pr_metadata_changed` | edited、labeled、assigned、unlabeled、unassigned | title、label、assignee、target branch 更新 | title、reviewer、status metadata 更新 | 默认不创建 review run。 |
| `pr_comment_context` | PR 上的 issue comment、review、review comment | MR note/discussion | PR thread/comment | 仅作为上下文，除非提到 bot。 |
| `agent_command` | PR comment/review 提到 bot 并附带指令 | MR note 提到 bot | PR thread/comment 提到 bot | 需要 provider-specific bot identity 与 actor policy 匹配。 |

默认只应对 `pr_opened`、`pr_updated`、`pr_reopened` 和
`pr_ready_for_review` 创建 review run。Metadata-only 和 comment-context 事件可以
更新 context 或创建 agent task，但不应启动完整自动 review，除非产品策略明确改变。

## 评论能力

Provider 的 comment API 差异通常比 webhook API 更大。应显式建模 capability，并在
line placement 不安全时降级为 summary-only 发布。

| 能力 | GitHub MVP | GitLab 目标 | Azure DevOps 目标 | 必需降级策略 |
| --- | --- | --- | --- | --- |
| 创建 summary comment | PR issue comment | MR note | 无文件路径的 PR thread 或 general comment | 创建后保存 `ReviewCommentRef`。 |
| 更新 summary comment | 按 ID 编辑已有 bot comment | token 允许时按 ID 更新 MR note | API 允许时更新 thread/comment | 若无法更新，使用稳定 marker 创建新的 summary。 |
| Line comment | Diff position 上的 review comment | 使用 `position` 字段的 discussion | 使用 `threadContext` 和 file/line 的 thread | 无法构造 position 时放入 summary。 |
| Multi-line comment | GitHub 支持但有限制 | 较新 GitLab API 支持 line range | Azure 支持取决于 API shape | 折叠为起始行或 summary-only。 |
| Thread resolve | Review thread API | Discussion resolve API | Thread status update | 不支持时将本地 ref 标记为 stale。 |
| Bot mention 检测 | comment/review body 中的 `@review-agent` | MR note body | Thread/comment content | Provider-specific bot login 和 identity 配置。 |

只有满足以下条件时，line comment 才可发布：

- 仓库 review config 启用了 line comments。
- Provider adapter 声明支持 line comments。
- Finding path 存在于 provider changed-file map。
- Finding line 能映射到 provider-commentable line 或 diff position。
- Provider token 有创建 comment 的权限。

否则应将 finding 保留为 summary-only，并记录类似
`file_not_changed`、`line_not_commentable`、`provider_line_comments_disabled`
或 `provider_permission_denied` 的原因。

## Fingerprint

Finding fingerprint 必须跨 provider adapter 保持稳定，并且独立于 provider 生成的
comment ID。Orchestrator 应继续从标准化 review context 生成 fingerprint：

- provider name
- repository full name 或稳定 repository key
- pull request 或 merge request number
- base 和 head commit SHA
- 标准化 file path
- severity
- 标准化 finding message

Adapter 必须在 result parsing 前将路径标准化为 repository-relative POSIX-style
path。不要把 provider diff position、thread ID 或 comment ID 放进 fingerprint，
因为平台重算 diff 或重建 comment 时这些值可能变化。

## 认证与配置

Provider credential 应按 provider 和部署环境隔离。

| Provider | 典型 secret | Webhook signature | API base URL |
| --- | --- | --- | --- |
| GitHub | `GITHUB_APP_ID`、`GITHUB_PRIVATE_KEY_PATH`、installation token、`GITHUB_WEBHOOK_SECRET` | `X-Hub-Signature-256` HMAC SHA-256 | `GITHUB_API_BASE_URL` |
| GitLab | bot/project/group token、app secret、`GITLAB_WEBHOOK_SECRET` | `X-Gitlab-Token` shared secret | `GITLAB_API_BASE_URL` |
| Azure DevOps | PAT 或 OAuth app credentials，按需配置 webhook secret | Service hook basic auth 或 configured secret | `AZURE_DEVOPS_ORG_URL` |
| Bitbucket | app password/OAuth consumer，按需配置 webhook secret | Workspace/webhook secret 支持因平台而异 | `BITBUCKET_API_BASE_URL` |
| GitCode | platform token/app credentials，按需配置 webhook secret | 按 GitCode webhook 文档使用 HMAC/shared secret | `GITCODE_API_BASE_URL` |

Provider settings 应在 `Settings` 和 `.env.example` 中清晰分组。不要复用 GitHub
变量来承载 GitLab 或 Azure 配置。多 provider 部署应使用独立 webhook URL：

```text
/api/v1/webhooks/github
/api/v1/webhooks/gitlab
/api/v1/webhooks/azure-devops
```

Secret 应在 adapter 边界解析为短生命周期 API client 或 token ref。不要在
review-run、workspace、comment-ref 或 event-inbox 行中持久化原始 token。

## 数据模型与路由

当前表已经在关键位置包含 `provider`。应继续把它作为所有 provider-specific 记录的
顶层分区：

- `ProviderEventInbox`：按 `(provider, delivery_id)` 唯一。
- `PullRequestContext`：按 `(provider, repo_full_name, pull_request_number)` 唯一。
- `ReviewRun`：按 provider、repo、PR number、head SHA 和 attempt 唯一。
- `ReviewCommentRef`：按 provider、repo、PR number 和 provider comment ID 唯一。
- `ReviewConfig`：按 provider 和 repo 唯一。
- `Workspace`：按 provider、repository、PR number 和 head SHA 唯一。

当平台同时存在人类可见 PR/MR number 和不透明 ID 时，把 number 存入
`pull_request_number`，把不透明值存入 `provider_pr_id`。当平台 repository 名称不是
全局唯一时，将 `repo_full_name` 设为稳定的 provider-scoped full path，并在
`provider_repo_id` 中保存不透明 repository ID。

## Provider-specific 说明

### GitHub

GitHub 应保持为 MVP 的参考实现。保留现有 duplicate delivery ID、PR synchronize
superseding、draft/ready action、mention-trigger task 和可选 webhook signature 校验
行为。

GitHub MVP 必需能力：

- Webhook ingest 和 normalization。
- PR context 持久化。
- Review-run 创建。
- 基于 clone URL 和 base/head SHA 准备 workspace。
- Result parsing 和 finding reconciliation。
- Summary comment ref tracking。

GitHub 后续增强：

- 用于 changed files 和 comment publishing 的具体 GitHub API client。
- Review thread 生命周期管理。
- Rate limit backoff 和 retry 分类。

### GitLab

GitLab 使用 merge request 术语，但应标准化到相同的内部 PR contract。先为 opened、
updated、merged、closed 和 reopened 事件准备 Merge Request Hook payload fixture。

GitLab-specific 实现点：

- 一致地使用 project path 或 project ID 作为 repository key。
- 使用 MR IID 作为 `pull_request_number`；如有需要，把全局 MR ID 存入
  `provider_pr_id`。
- 将 source/target branch SHA 映射到 `head_sha` 和 `base_sha`。
- 基于 GitLab discussion `position` 字段构造 line comment，而不是 GitHub-style
  diff position。
- 增加 thread resolve 时，把 unresolved discussion 当作 provider thread。
- 使用 `X-Gitlab-Token` 或已配置的 GitLab secret 机制进行 webhook 校验。

GitLab MVP 必需能力：

- MR opened/updated/reopened/merged/closed 的 webhook ingest。
- PR context 提取和 review-run 创建。
- 足以判断 summary-only 与 line-commentable finding 的 changed-file map。
- Summary comment 创建和更新。

后续增强：

- 完整 discussion resolve 支持。
- Self-managed GitLab API base URL 校验。
- Group-level token 与 project-level token policy 检查。

### Azure DevOps

Azure DevOps service hook 的事件名称和 resource shape 与 GitHub 差异较大。应将 pull
request created/updated/completed 标准化为相同的内部词汇。

Azure-specific 实现点：

- 使用 organization、project、repository name 或 ID 以及 PR ID 构建稳定
  repository key。
- 当 Azure PR ID 是人类可见 ID 时存入 `pull_request_number`；如还存在额外不透明值，
  则存入 `provider_pr_id`。
- 当 merge commit 或 completion metadata 表示成功时，将 completed pull request
  映射为 `pr_merged`；abandoned/closed without completion 映射为 `pr_closed`。
- 将 comment 建模为 PR thread。File/line comment 需要 Azure thread context 字段，
  不是 GitHub review comment position。
- Azure 权限通常按 organization/project/repository scope 限制；应将 permission
  failure 映射为 provider error，让发布降级而不是让 result collection 失败。

Azure MVP 必需能力：

- PR created、updated 和 completed 的 service hook ingest。
- 基于部署策略的 signature 或 shared-secret 校验。
- PR context 提取和 review-run 创建。
- Summary comment 创建/更新，或 summary append fallback。

后续增强：

- 已解决 finding 的 thread status update。
- Branch policy/check status 集成。
- Organization-level rate limit 和 permission 诊断。

### Bitbucket 和 GitCode

这两个 provider 应在 GitLab 或 Azure 验证 adapter 边界后再作为后续 adapter 接入。
它们仍应复用相同的内部契约：

- 将 provider pull request 事件标准化为内部 `pr_*` 事件。
- 将不透明 provider ID 与用户可见 PR number 分开存储。
- 在启用 line comment 前构建 provider-specific commentability map。
- 如果 line comment 依赖脆弱的 diff-position mapping，先从 summary comment 开始。

GitCode 可能根据目标 API surface 更接近 GitHub 或 GitLab，但仍应有独立的 provider
adapter、settings、fixtures 和 contract tests。除非明确验证 API contract，否则不要
把 GitCode 流量指向 GitHub adapter。

## 测试策略

每个 provider 都必须先具备 local-only 测试，再添加真实集成测试。默认
`uv run pytest` 不应依赖网络访问或 provider credential。

必需的 provider 测试层：

| 层级 | 目的 | Fixture 位置 |
| --- | --- | --- |
| Normalizer unit tests | 请求头、签名、事件名、action mapping、缺失字段、无效 payload | `tests/fixtures/{provider}/webhooks/*.json` |
| Adapter contract tests | Changed files、commentability map、summary comment upsert、line comment fallback、rate limit error mapping | `tests/fixtures/{provider}/api/*.json` |
| Service tests | Inbox idempotency、PR context 持久化、review-run 创建、superseding | 使用现有 service/API test 风格和 fake adapters |
| E2E/BDD tests | 从 webhook 到 review run 再到 result reconciliation 的闭环路径 | 扩展 `tests/e2e` helper 以支持 provider 参数化 |

Fixture 规则：

- 保持 raw provider payload 尽量接近真实 webhook/API response。
- 用确定性的测试值替换 secret、clone URL、commit SHA 和 ID。
- 每个 provider 至少包含一个 unsupported 或 ignored action fixture。
- 至少包含一个 finding line 无法映射到 commentable diff line 的 fixture。
- Provider fixture 目录保持独立；不要修改 GitHub fixture 来代表其他 provider。

Contract tests 应断言每个 adapter 对等价事件返回相同内部形状。还应断言 graceful
degradation：

- unsupported provider action 返回 ignored event；
- 只有配置 secret 时，缺失 signature 才失败；
- duplicate delivery ID 保持幂等；
- unpublishable finding 变为 summary-only；
- provider 不支持 summary comment update 时，降级为 create。

## 接入 Checklist

添加 provider 时使用此 checklist：

- 添加 provider settings 和 `.env.example` 条目。
- 添加封装原生 API、scope 凭据和 `aclose()` 的 Platform 对象。
- 添加实现四类 Provider Core 转换的 Provider 对象。
- 添加返回 `ProviderRuntime(provider=..., close=platform.aclose)` 的 factory，
  并注册 plugin key。
- 添加 provider webhook fixtures 和 normalizer tests。
- 添加 checkout、comment、query、凭据刷新和生命周期合约测试。
- 添加 service tests，证明 inbox idempotency 和 review-run creation。
- 添加 changed-file fixtures 和 commentability mapping tests。
- 启用 publishing 前添加 summary comment contract tests。
- 在 provider diff-position mapping 可靠前保持 line comments disabled。
- 添加 provider deployment notes、必需 webhook events 和 token permissions。
- 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`。
- 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`。

## 风险与后续工作

Provider Core 已有类型化 comment 合约；为保持兼容，旧 Orchestrator publishing
能力仍携带数据库 session 和 domain model。应继续把这些调用封装在
Provider/Platform runtime 内，并在逐步收敛旧能力时避免把平台凭据放入
Orchestrator 请求。

不要把 finding reconciliation 耦合到任何单一 provider 的 diff-position 模型。
稳定的内部契约是 `commentable_lines`，并携带 provider-specific metadata。

不要要求所有 provider 第一天就支持同一组功能。新 provider 的最小安全基线是
webhook ingest、PR context、review-run creation、workspace preparation、result
parsing 和 summary comment publishing。Line comments、thread resolve、branch policy
status 和高级 rate-limit handling 可以在 adapter 具备 contract coverage 后继续推进。
