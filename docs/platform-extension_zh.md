# 平台 Provider 扩展指南

Review Orchestrator 以 GitHub 作为参考 provider，并已包含初步的 GitLab 实现。
数据模型、API schema、webhook 接入和 worker 操作都携带 `provider` 字段，并通过
provider adapter 路由平台行为。本文说明如何在不破坏现有 provider 的前提下，
扩展到 Azure DevOps、Bitbucket、GitCode 或其他代码托管平台。

目标形态是在外部平台行为周围建立一个小而清晰的 provider adapter 边界。Review
run 生命周期、OpenHands session 编排、workspace 准备、结果解析、finding 对账和
重试策略应继续保持 provider-agnostic。

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
- 当 PR comment 或 review 中提到配置的 review bot login 时，创建
  mention-trigger agent task。
- 通过 `(provider, delivery_id)` 实现 provider event 幂等。
- 对 PR context、review run、comment ref、workspace 和 review config 使用
  provider 维度隔离存储。
- 通过 `ReviewCommentRef` 跟踪 summary comment 和 line comment 引用。
- 当 finding 无法映射到 changed file 中可评论的行时，降级为 summary-only。

代码中仍然可见的 provider-specific 集成点：

- 每个 adapter 仍负责所属平台的请求头、签名格式、事件名、payload 路径、client
  和认证设置。
- Workspace clone URL 和凭据准备仍包含 worker adapter 操作之外的 provider-aware
  行为。
- GitHub 支持 line comment；GitLab 当前返回 summary-only 统计。
- Review thread resolve 和 provider-specific rate-limit backoff 尚未进入最小
  adapter 契约。

不要在第二个平台证明需求之前就重命名或泛化所有 GitHub 代码。应该添加能让下一个
provider 复用内部契约的最小 adapter surface。

## Provider 边界

Provider adapter 负责外部平台相关能力：

- Webhook 请求头校验和签名验证。
- 原始 payload 解析和 delivery ID 提取。
- 将事件标准化为下文列出的内部事件名。
- Pull request 或 merge request metadata 提取。
- Changed file 和 diff metadata 获取。
- Line-level finding 的可评论位置映射。
- Summary comment 创建和更新。
- Line comment 或 review thread 创建和更新。
- 平台支持时的 review thread resolve 或 stale comment 处理。
- Rate limit、retry-after、permission、not-found 错误映射。
- 基于 provider-specific secret 查找 token 并构造 API client。

Orchestrator core 负责共享行为：

- Provider event inbox 幂等和 coalescing。
- `PullRequestContext`、`ReviewRun`、`Finding`、`ReviewCommentRef`、
  `ReviewConfig` 和 `Workspace` 持久化。
- Review run retry、cancel、supersede、timeout 和生命周期状态。
- OpenHands session start/sync/cancel。
- Review result schema 校验和 fingerprint 生成。
- 对无法发布为 line comment 的 finding 执行 summary-only 降级。

## Adapter 契约

新 review-triggering provider 的最小 adapter 契约是：

```python
class ProviderAdapter(Protocol):
    provider: str

    def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        settings: Settings,
    ) -> ParsedProviderWebhook: ...

    async def get_pull_request_context(
        self,
        task: AgentTask,
    ) -> PullRequestContext | None: ...

    async def list_changed_files(
        self,
        review_run: ReviewRun,
    ) -> list[ChangedFile]: ...

    async def publish_summary_comment(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        status_text: str,
        finding_stats: dict[str, int] | None = None,
    ) -> ReviewCommentRef | None: ...

    async def publish_line_comments(
        self,
        session: AsyncSession,
        review_run: ReviewRun,
        *,
        changed_files: list[ChangedFile],
    ) -> dict[str, int]: ...
```

`parse_webhook` 由 API registry 用于 webhook 接入。Worker 侧也通过同一个
adapter 对象完成 PR context hydration、changed-file lookup、summary
发布和 line comment 发布。暂不支持 line comment 的 provider 应让
`publish_line_comments` 返回全零统计，并保持该 provider 的
`ReviewConfig.line_comments_enabled` 关闭。

Worker 启动时应只构造一次持有 client 的 registry，并在 task 处理、review 处理和
timeout 扫描中复用。操作未配置时，adapter 抛出 `ProviderCapabilityError`；平台
client 或 SDK 失败时，adapter 将其转换成带有 `provider` 和 `operation` 属性的
`ProviderOperationError`。Worker 只处理共享的 `ProviderError` 边界，不应导入
平台专属的 client error。

无论外部平台使用什么术语，内部都使用这些数据形状：

| 内部形状 | 必需字段 | 说明 |
| --- | --- | --- |
| `ProviderWebhookEvent` | `provider`, `delivery_id`, `provider_event`, `provider_action`, `internal_event`, `repository`, `pull_request_number`, `head_sha`, `status`, `raw_payload` | `pull_request_number` 也用于 GitLab merge request 和 Azure pull request。优先存储稳定的人类可见 MR/PR 编号。 |
| `ProviderPullRequestContext` | `provider`, `repo_full_name`, `pull_request_number`, `base_sha`, `head_sha`, `base_ref`, `head_ref`, `author_login`, `html_url`, `status`, `is_fork` | 当平台存在与 PR/MR number 不同的不透明 ID 时，保存在 `provider_pr_id`。 |
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
| `agent_mention` | PR comment/review 提到 bot | MR note 提到 bot | PR thread/comment 提到 bot | 需要 provider-specific bot identity 匹配。 |

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
- 添加 adapter module，覆盖 webhook parsing、signature validation、event
  normalization 和 PR context extraction。
- 将 adapter 注册到 `/api/v1/webhooks/{provider}`。
- 添加 provider webhook fixtures 和 normalizer tests。
- 添加 service tests，证明 inbox idempotency 和 review-run creation。
- 添加 changed-file fixtures 和 commentability mapping tests。
- 启用 publishing 前添加 summary comment contract tests。
- 在 provider diff-position mapping 可靠前保持 line comments disabled。
- 添加 provider deployment notes、必需 webhook events 和 token permissions。
- 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`。
- 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`。

## 风险与后续工作

当前代码缺少具体的 adapter registry 和 provider-neutral webhook event type。对
GitHub MVP 来说这是可接受的，但第一个非 GitHub provider 应在加入大量
provider-specific 分支前先引入 registry。

Provider comment publishing 也尚未成为完整 adapter。不要把 finding reconciliation
耦合到任何单一 provider 的 diff-position 模型。稳定的内部契约是
`commentable_lines`，并携带 provider-specific metadata。

不要要求所有 provider 第一天就支持同一组功能。新 provider 的最小安全基线是
webhook ingest、PR context、review-run creation、workspace preparation、result
parsing 和 summary comment publishing。Line comments、thread resolve、branch policy
status 和高级 rate-limit handling 可以在 adapter 具备 contract coverage 后继续推进。
