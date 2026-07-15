# Agent 与任务架构重规划

状态：设计已确认并在 `feat/pi-agent` 分支实现，完成验证后合入

适用范围：Review、PR 消息命令以及后续相同形态的仓库 Agent 能力。

## 1. 结论

推荐采用“**任务控制面 + 确定性执行器 + 薄 Agent Runtime + 审计上下文账本**”架构。

核心判断：

- Bot 的可靠性来自任务调度、Workspace 准备、幂等投递和审计，不应由 Agent 自己完成；
- Agent 只接收已经准备好的 Workspace 和上下文，使用 preset 显式提供的 Tool 完成任务并提交结构化结果；
- 新增大多数能力应以一个 Capability Pack 完成，不修改公共 Worker、Runtime Server 或调度器；
- 当前继续使用 pi SDK，但收缩自研 Runtime 契约；
- 当前单 Agent 工具循环不引入 LangGraph。只有出现真实的多节点、分支、并行或人工恢复流程时再引入；
- 首期继续使用 PostgreSQL/SQLite 数据库队列，不立即增加 Redis、Kafka 或 Temporal。

目标结构：

```text
Provider / API
      │ 规范化消息
      ▼
Task Control Plane
  Task + Priority Queue + Lease + Retry + Concurrency Key
      │
      ▼
Deterministic Executor
  上下文构建 ─ Workspace 准备 ─ Agent 调用 ─ 结果校验 ─ Outbox
                       │
                       ▼
                 Thin Agent Runtime
              Prompt + Skill + Configured Tools
                       │
                       ▼
                 Structured Result
      │
      ▼
Delivery Worker / Provider Adapter
```

### 1.1 已确认的架构决策

| 主题 | 决策 |
| --- | --- |
| 总体形态 | Task 控制面 + 确定性执行器 + 独立薄 pi Runtime |
| 工作流 | 固定流水线；当前不引入 DAG、LangGraph 或 Temporal |
| Task 模型 | 使用统一 Task，Review、消息任务等使用领域扩展数据 |
| 队列 | PostgreSQL 数据库队列；SQLite 测试使用 CAS，保留 Scheduler Adapter |
| 资源控制 | user、repository、PR、comment、model 等维度使用可配置计数式资源锁；一次阶段所需资源原子获取 |
| 配置 | 只允许按领域组合 preset，例如 Agent 基础、Repository Skills 和 Task Type；使用字段级合并规则 |
| 审计 | 当前永久保存 Session 内容和 Task 元数据，以可解释为目标；精确输入重建作为后续路标 |
| Workspace | 由 Workspace Provisioner 准备；Agent 可以在任务 Workspace 中读写和执行 |
| Tool | Tool 显式配置且彼此独立；框架不推断 Tool 依赖，Skill 不隐式增加 Tool |
| Skill | 支持 npm 安装、builtin 和预构建环境；Skill 在运行环境内不做额外能力限制 |
| Runtime 环境 | 长期 Runtime 容器 + 每 Task 独立进程、Workspace 和可丢弃环境；可选一次性容器 |
| 外部副作用 | clone/fetch、Provider 投递等默认仍由确定性组件执行；Agent 可生成本地修改、命令结果和副作用请求 |
| 完成状态 | execution 与 delivery 分开；必需投递完成前 Task 保持 `awaiting_delivery` |
| 迁移 | 直接在当前功能分支完成重构后再合入，不先合入过渡平台设计 |

## 2. 目标与非目标

### 2.1 目标

1. **快速扩展**：标准能力通过配置、Schema、Skill 和测试实现，公共执行代码保持不变。
2. **上下文可审计**：可以回答一次运行到底使用了哪些消息、代码版本、Skill、Prompt、Tool、模型配置和结果。
3. **任务可调度**：支持队列、优先级、延时执行、重试、lease、并发约束和防饥饿。
4. **Agent 专注推理**：代码下载、鉴权、消息发布、重试和任务状态均由确定性组件处理。
5. **配置可解析**：Skill、Tool、Workspace、模型预设和限制最终解析为结构化 ResolvedPreset，
   并随 Task/Session 元数据永久保存。
6. **渐进迁移**：复用现有 Provider、Workspace、数据库和 pi SDK，不要求一次性重写。

### 2.2 非目标

- 不建设通用低代码 Agent 市场；
- 普通 Task 输入不能直接替换 system instructions、Tool 实现或模型 Base URL；Skill 和环境包由已解析的
  Capability/Repository/Task Type preset 安装；
- 不让 Agent 直接持有 GitHub/GitLab 凭据或发布评论；
- 不承诺模型输出可确定性重放；当前只保证 Session 与 Task 元数据可解释，精确输入重建作为路标；
- 不在首期引入多 Agent 图编排、跨服务事件总线或新的基础设施集群。

## 3. 当前实现审计

当前实现已经具备可复用基础：

- `ProviderEventInbox` 保存 Provider 原始事件及摘要；
- `ReviewRun` 和 `AgentTask` 具备状态、阶段、超时、结果与部分 lease 字段；
- Worker 通过数据库查询领取任务；
- Workspace 支持固定提交、缓存、凭据遮蔽和 lease；
- pi Runtime 提供仓库读写、命令执行、结构化完成 Tool 和执行预算；
- Provider Adapter 负责评论发布。

主要缺口：

1. **调度只是 FIFO**：`AgentTask` 按 `created_at` 领取，没有 queue、priority、
   `available_at`、资源类别和通用并发键。
2. **状态所有权分散**：`ReviewRun`、`AgentTask` 和 pi Runtime Session 都保存执行状态，
   但彼此不是清晰的业务任务、执行尝试和 Agent Run 关系。
3. **上下文不可完整追溯**：保存了事件、命令和部分结果，但没有一次运行最终使用的有序上下文、
   Prompt、Skill 内容、Tool 版本及 Workspace 身份的统一快照。
4. **Runtime 契约超前**：Agent 语义版本、动态 Skill 覆盖、Profile、模型覆盖和交互矩阵同时存在，
   超过当前两个生产 Agent 的需要。
5. **运行层模块化不等于产品模块化**：注册一个 Agent 不需要改 Runner，但要形成一个 Provider 可用
   的 Bot 能力，仍需要修改触发、任务处理、上下文和投递代码。
6. **确定性操作和 Agent 配置耦合**：Workspace 路径、Repository Context、消息历史和 Agent
   请求参数由多个模块拼装，缺少一个稳定的 Capability 边界。

## 4. 三个可选方向

### 4.1 方向 A：薄 SDK 模块化单体

将 Agent SDK 直接接入 Python Worker，或更换成同语言 SDK；每个 Agent 只定义 instructions、
tools 和 output schema。任务、Workspace 和投递继续由当前应用负责。

优点：

- 代码最少，单个 Agent 开发最快；
- 没有 Python/Node HTTP DTO 和双层 Session；
- 调试链路短。

缺点：

- 如果继续使用 TypeScript pi SDK，就无法真正合并进程；
- Agent 执行会与 Worker 的资源、崩溃和依赖生命周期绑定；
- 未来切换 Runtime 或隔离不同执行权限更困难。

适用：Agent 数量少、吞吐低、同语言 SDK 可接受、部署隔离要求低。

### 4.2 方向 B：任务控制面 + 薄 pi Runtime（推荐）

保留 Python Orchestrator 和 Node pi Runtime 的进程边界，但重新限定职责：

- Orchestrator 是任务、上下文、Workspace、调度和投递的唯一控制面；
- Runtime 只执行一个解析完成的 Agent Run；
- Capability Pack 描述触发后的上下文、Workspace、Agent 和投递策略；
- Agent Runtime 不提供通用业务工作流，不管理 Provider，也不下载代码。

优点：

- 最大限度复用当前实现；
- 保留 pi coding agent 的仓库工具循环和隔离边界；
- 调度与审计可以独立于模型和 SDK；
- 后续替换 Runtime 时，Task/Context/Delivery 契约不变。

缺点：

- 仍有两个进程和一套 HTTP 契约；
- 必须明确 Orchestrator Task 与 Runtime Run 的状态归属；
- 比方向 A 多一些基础代码。

适用：当前仓库和未来 1～2 个阶段。

### 4.3 方向 C：工作流引擎 / Graph 原生

使用 LangGraph、Temporal 或类似工作流引擎表达 Workspace、多个 Agent、人工节点、汇总和投递。
外部消息仍通过 Provider Adapter，Agent 节点只负责推理。

优点：

- 多节点分支、并行、暂停恢复和长流程具有清晰语义；
- 工作流状态和执行历史可以成为一等对象；
- 适合多个专用 Agent 并行后汇总。

缺点：

- 当前单 Agent 循环会被包装成一张几乎只有一个认知节点的图；
- 容易与现有 Worker lease、retry、timeout 形成双状态机；
- 基础设施、调试和团队认知成本最高；
- LangGraph 本身不能替代 Provider 幂等、Workspace 安全或消息 Outbox。

适用：出现至少三个具有业务意义的节点，并需要分支、并行或跨小时恢复时。

### 4.4 决策矩阵

| 维度 | A：薄 SDK 单体 | B：任务控制面 + 薄 Runtime | C：Workflow/Graph |
| --- | --- | --- | --- |
| 首个新能力速度 | 最快 | 快 | 慢 |
| 当前代码复用 | 中 | 最高 | 低 |
| Agent 进程隔离 | 弱 | 强 | 取决于部署 |
| 队列/优先级 | 需补充 | 控制面统一提供 | 引擎或外部队列提供 |
| 上下文审计 | 需补充 | 统一 Context Ledger | 可做，但需自定义内容模型 |
| 单 Agent 复杂度 | 最低 | 低 | 高 |
| 多阶段流程 | 弱 | 固定流水线 | 最强 |
| 当前推荐 | 备选 | **推荐** | 暂缓 |

## 5. 推荐架构的模块边界

### 5.1 Ingress Adapter

职责：

- 验证 Provider 签名；
- 保存原始事件及 `payload_digest`；
- 规范化为 `MessageEnvelope`；
- 根据 Capability 的 trigger rule 创建 Task；
- 生成 `dedupe_key`，不调用模型、不克隆仓库、不发布响应。

```json
{
  "source": "github",
  "event_type": "issue_comment.created",
  "tenant": "default",
  "repository": "owner/repo",
  "subject": {"type": "pull_request", "number": 42},
  "actor": {"login": "alice", "association": "MEMBER"},
  "message": {"id": "123", "text": "解释这个修改"},
  "source_ref": "provider-event:<id>",
  "dedupe_key": "github:delivery:<delivery-id>"
}
```

Provider 特有字段留在原始事件中；公共调度和 Capability 不读取未规范化 payload。

### 5.2 Task Scheduler

Scheduler 只处理执行控制，不理解 Prompt 或模型：

- queue 路由；
- priority 和 aging；
- `available_at` 延时；
- lease 领取与续约；
- retry/backoff；
- `concurrency_key` 串行化；
- deadline/cancel；
- 资源类别和 Worker 匹配。

### 5.3 Deterministic Executor

Executor 使用固定生命周期，不在首期引入任意 DAG：

```text
validate_input
  -> resolve_manifest
  -> build_context
  -> prepare_workspace (optional)
  -> invoke_agent      (optional)
  -> validate_result
  -> enqueue_delivery
  -> await_delivery
  -> complete
```

每一步都必须：

- 输入明确且可以序列化；
- 有幂等键；
- 产生 TaskEvent；
- 将错误分类为 `validation`、`retryable_infrastructure`、`agent_failure` 或
  `permanent_delivery`；
- 不把失败重试决策交给 Agent。

代码下载属于 `prepare_workspace`，消息发布属于 `Delivery Worker`，均不是 Agent Tool。

### 5.4 Session Audit Store

当前审计目标是“可解释”，不建设完整 Context Ledger，也不要求精确还原模型输入。系统永久入库：

- pi Session 中的用户、系统和 assistant 消息；
- Tool Call、参数、返回内容或明确的截断内容；
- shell 命令、stdout/stderr 和退出状态；
- Task 类型、来源、状态、阶段、优先级、资源申请和 Attempt；
- Agent、模型 preset、Skill package refs、显式 Tool 列表和执行限制；
- Workspace repository、base/head SHA、工作区 diff 和最终结果；
- Delivery Outbox 状态和 Provider 消息 ID。

Session 内容和 Task 元数据永久保存在数据库。写入前执行基础 secret redaction；超大 Tool 输出按固定
上限截断并记录原始字节数和截断状态。当前不额外复制所有仓库文件，也不保证能够重建最终组合
Prompt。

完整的有序 ContextBundle、内容寻址存储和不可变 RunManifest 作为后续路标。当出现合规、精确回放
或跨版本诊断需求时，可以在现有 TaskAttempt 和 SessionArchive 上扩展，而不改变 Agent 调用协议。

### 5.5 Thin Agent Runtime

Runtime 的唯一核心操作：

```text
run(agent_spec_ref, resolved_preset, session_input, workspace_handle) -> agent_run
get(agent_run_id) -> status/result/events
cancel(agent_run_id)
```

`agent_spec_ref` 必须指向 Runtime 已安装的可信 Capability Pack，Runtime 校验其内容 digest 与
已解析 preset 一致。HTTP 调用方不能直接提交新的 system instructions、Tool 实现或模型 Base URL；
Capability 和 Repository preset 可以声明需要安装的 Skill package refs。

Runtime 负责：

- 创建 pi SDK Session；
- 从 builtin、预构建环境或 Task 本地 npm 环境装载 Skill；
- 暴露 preset 中显式列出的 Tool，包括 Workspace 读、写和命令执行能力；
- 记录模型事件和 Tool Call；
- 执行 turn/tool/result 限制；
- 通过唯一完成 Tool 接收并校验结构化结果。

Runtime 不负责：

- 选择业务队列和优先级；
- 克隆、fetch 或清理仓库；
- 查询 GitHub/GitLab；
- 创建或更新评论；
- 拼装跨任务历史；
- 决定业务重试；
- 解析普通 Task 输入直接提供的 Skill、Provider 或 Base URL。

### 5.6 Delivery Outbox

Agent Result 通过验证后，Executor 在同一数据库事务写入 `DeliveryOutbox`。独立 Publisher：

- 按 `destination + idempotency_key` 去重；
- 独立使用 `queue`、`priority`、`available_at`、lease 和 retry policy 调度消息投递；
- 默认继承来源 Task 的优先级，使交互回复不会被批量 Review 消息阻塞；
- 调用 Provider Adapter；
- 保存请求正文摘要、Provider message ID、尝试次数和最终状态；
- 对限流和网络错误重试；
- 不因 Agent Session 已结束而丢失结果。

Agent 永远不获得 Provider 写权限。Task 在 Outbox 成功或进入明确的永久失败状态前保持
`awaiting_delivery`，不会提前标记完成。

## 6. 核心数据模型

### 6.1 Task

`Task` 是业务请求和调度的统一控制对象：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定任务 ID |
| `capability_id` | 使用的 Capability |
| `status/stage` | 生命周期与当前确定性步骤 |
| `queue` | `interactive`、`review`、`maintenance` 等 |
| `priority` | 0～100，数值越高越先执行 |
| `available_at` | 最早可领取时间，用于定时和 backoff |
| `deadline_at` | 任务硬截止时间 |
| `dedupe_key` | 入口幂等 |
| `concurrency_key` | 例如 `github:owner/repo:pr:42` |
| `resource_class` | `io`、`agent-standard`、`agent-heavy` |
| `input_ref/result_ref` | 不可变输入和结果引用 |
| `resolved_preset_json` | 本次实际使用的领域 preset 元数据 |
| `attempt/max_attempts` | 尝试控制 |
| `lock_owner/locked_until` | Worker lease |
| `created_at/updated_at` | 排序与审计时间 |

业务特有数据不继续扩张 Task 表：Review finding、发布记录等使用 `task_id` 关联的专用表或内容对象。

### 6.2 TaskAttempt

每次执行尝试独立记录：

- `task_id/attempt_no`；
- Workspace Handle；
- Agent Run ID；
- ResolvedPreset 元数据；
- 开始、结束和失败分类；
- token/tool/turn 使用量；
- output reference。

Task 是业务生命周期，TaskAttempt 是执行生命周期，pi Session 只是一次 Agent Run。三者不再互相
充当对方的状态源。

### 6.3 TaskEvent（路标）

当前按 D12 只永久保存 Session、Task/Attempt 元数据和 Delivery 记录，不额外建设完整的
append-only TaskEvent 流。后续需要更细粒度流程审计时可增加：

```json
{
  "task_id": "...",
  "attempt": 1,
  "sequence": 12,
  "type": "agent.tool.completed",
  "stage": "invoke_agent",
  "actor": "pi-runtime",
  "payload_ref": "content:sha256:...",
  "created_at": "..."
}
```

同一 Task 的 `sequence` 单调递增。事件用于审计和诊断，Task 当前状态仍作为高效查询投影。

### 6.4 SessionArchive

`SessionArchive` 是当前审计载体：

| 字段 | 含义 |
| --- | --- |
| `task_id/attempt_no/agent_run_id` | 与 Task、Attempt 和 Runtime Run 关联 |
| `session_json` | pi Session 的永久序列化内容 |
| `task_metadata_json` | Task 来源、preset、资源、Workspace、结果和投递元数据 |
| `workspace_diff` | Agent 在任务 Workspace 中产生的最终 diff |
| `redaction_version` | 入库时使用的 secret redaction 规则版本 |
| `created_at` | 归档时间 |

当前只能保证通过 Session 和元数据解释执行过程，不能声称精确重建 Agent 当时看到的全部输入。后续
需要可重建能力时，再增加有序 ContextBundle、内容 digest 和不可变 RunManifest。

## 7. 调度语义

### 7.1 领取顺序

生产环境使用 PostgreSQL `FOR UPDATE SKIP LOCKED`，SQLite 测试使用 compare-and-swap。逻辑顺序：

```sql
WHERE status = 'queued'
  AND available_at <= now()
  AND deadline_at > now()
  AND no_active_task_with_same_concurrency_key
ORDER BY effective_priority DESC, available_at ASC, created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED
```

首期 `effective_priority` 可以通过定时 aging 更新，避免在领取 SQL 中引入昂贵计算：等待每超过一个
aging 周期提升一级，但不超过 100。

### 7.2 推荐默认队列

| Queue | 默认优先级 | 用途 |
| --- | ---: | --- |
| `interactive` | 80 | 用户明确 @Bot 的问题 |
| `manual-review` | 60 | 用户/API 手工触发 Review |
| `webhook-review` | 40 | 自动 PR Review |
| `maintenance` | 10 | 清理、回收和非紧急任务 |

优先级是调度提示，不绕过每仓库并发、资源配额和 deadline。仓库配置只能在 Capability 声明的范围内
调整，不能把普通任务提升为系统维护级或管理员级。重试保留原 queue，通过 `available_at` 延时，
并按 Capability 策略施加有限 priority penalty；它不是另一种资源队列。

### 7.3 并发控制

并发统一建模为可配置计数式资源锁。一个执行阶段声明多个 Resource Request，例如：

```text
user:alice                    1
repository:owner/repo         1
pr:owner/repo/42              1
comment:github/123            1
model:openai/gpt-5.4          1
```

资源配置的 capacity 为 1 时就是互斥锁，大于 1 时就是并发配额。Scheduler 必须原子获取当前阶段的
全部资源，不能只持有其中一部分等待其他资源。无法获取时跳过该 Task 扫描后续候选，避免队头阻塞。

- Workspace 阶段只申请 IO、repository 等资源；
- Agent 阶段申请 user、repository、PR/comment、model 等 preset 声明的资源；
- Resource Lease 使用 TTL 和 heartbeat，Worker 退出后自动回收；
- 资源维度、selector 和 capacity 通过接口与配置提供，当前实现内置上述常用字段；
- `TASK_RESOURCE_CAPACITIES` 只为首次出现的 Pool 提供默认 capacity；通过
  `PUT /api/v1/resource-pools/{key}` 创建或修改后的数据库配置保持权威，不会在领取时被默认值覆盖；
- Task 的 `resource_context` 可使用简单 key/list，也可使用 `{keys: [...], units: n}` 申请多单位资源；
- comment capacity 默认为 1，保证同一消息上的处理顺序；
- 新 head 可以通过 Task 策略取消或 supersede 旧 Review Task。

### 7.4 重试所有权

| 失败 | 所有者 | 行为 |
| --- | --- | --- |
| 输入/Schema 错误 | Executor | 永不重试 |
| Clone/fetch 网络错误 | Workspace Executor | backoff 后重试 |
| 模型临时错误 | Runtime 内短重试，Task 外长重试 | 有上限且记录新 Attempt |
| Agent 未提交合法结果 | Task Executor | 按 Capability 策略决定是否新 Attempt |
| Provider 限流/网络错误 | Delivery Worker | 不重新运行 Agent |
| Provider 权限错误 | Delivery Worker | 永久失败并告警 |

## 8. Capability、Skill、Tool 与 Workspace 配置

### 8.1 Capability Pack

建议目录：

```text
capabilities/pr-question/
├── capability.yaml
├── schemas/
│   ├── input.json
│   └── output.json
├── prompts/
│   └── task.md
├── skills/
│   └── pr-assistant/SKILL.md
└── evals/
    └── basic.yaml
```

大多数新能力只增加一个 Pack。只有需要新的确定性 Context Builder、Workspace Provider、Tool 或
Delivery Adapter 时才增加代码插件。

示例：

```yaml
apiVersion: review-orchestrator/v1alpha1
kind: Capability
metadata:
  id: pr-question
spec:
  trigger:
    messageTypes: [pull_request_comment]
    command: mention

  scheduling:
    queue: interactive
    priority: 80
    resourceClass: agent-standard
    concurrencyKey: "{provider}/{repository}/pr/{pull_request_number}"
    resources:
      - key: "user:{author_login}"
        units: 1
      - key: "repository:{repository}"
        units: 1
      - key: "pr:{repository}/{pull_request_number}"
        units: 1
      - key: "comment:{provider}/{source_comment_id}"
        units: 1
      - key: "model:{model_profile}"
        units: 1
    timeoutSeconds: 600
    maxAttempts: 2

  workspace:
    provider: git
    mode: read-write
    revision: "{head_sha}"
    baseRevision: "{base_sha}"
    cachePolicy: shared-mirror
    cleanupPolicy: lease

  context:
    builders:
      - pr-metadata
      - current-command
      - prior-bot-exchanges
    limits:
      historyTurns: 6
      historyChars: 24000

  agent:
    runtime: pi
    id: pr-assistant
    modelProfile: balanced
    prompt: prompts/task.md
    skills:
      - skills/pr-assistant/SKILL.md
    tools:
      - repository.list-files
      - repository.read-file
      - repository.search-code
      - repository.git-diff
      - workspace.write-file
      - workspace.shell
    limits:
      turns: 16
      toolCalls: 60
      resultBytes: 100000

  inputSchema: schemas/input.json
  outputSchema: schemas/output.json

  delivery:
    adapter: provider.pr-comment
    mode: replace-placeholder
```

### 8.2 配置解析规则

解析顺序固定：

1. 部署中安装的 Capability Pack；
2. Capability 声明的命名 preset；
3. Repository Policy 中被 Capability 明确允许的有限覆盖；
4. Task 输入只填充模板变量，不改变 Skill、Tool、Provider 或 Base URL；
5. 按字段级规则解析为 ResolvedPreset，并随 Task/Session 元数据永久保存。

禁止“任意请求覆盖任意字段”。需要新增可变项时，先在 Capability Schema 中声明范围，避免当前
Profile、Skill override 和模型策略形成组合爆炸。

不同字段使用不同合并规则：

- `systemInstructions`：Agent 基础内容固定，其他 preset 只能追加领域内容；
- `skills`：Agent Skills → Repository Skills → Task Type Skills，稳定排序并允许重复项去重；
- `model/limits`：Task Type > Repository Policy > Agent 默认；
- `tools`：Agent 与 Task Type 显式组合，Repository Skill 不隐式增加 Tool；
- `workspace`：Task Type > Repository Policy > 系统默认；
- Task 输入只提供模板数据，不参与配置优先级。

Tool 之间的依赖和兼容性由 Capability 作者决定，框架不做依赖解析。

### 8.3 Skill 规则

- Skill 可以是 builtin 内容、npm package 或预构建环境中的内容；
- Skill 可以描述如何使用 Tool，但不能授予 Tool；
- 加载顺序由 Agent、Repository、Task Type 的字段级合并规则确定；
- npm Skill 的 package、版本、安装命令和结果随 Session/Task 元数据保存；
- Skill 的安装脚本和运行内容在 Task 执行环境内不做额外能力限制；
- Skill 不会隐式注册或增加 Tool，Tool 仍由 preset 显式列出。

### 8.4 Tool 规则

每个 Tool 注册项包含：

- 稳定 ID；
- 输入/输出 Schema；
- 实现版本或 digest；
- timeout、最大输出和并发限制；
- 审计策略与敏感字段；
- 是否确定性/是否有外部副作用。

Tool 默认彼此独立，框架不构建 Tool 依赖图，也不根据 Skill 自动推导 Tool。preset 可以显式提供
Workspace 读取、写入、shell、构建和测试等完整能力。clone/fetch、Provider 评论发布等默认仍由
确定性组件负责，避免凭据和业务幂等进入 Agent 控制流。

### 8.5 Workspace 规则

Workspace Provisioner 接收 `WorkspaceSpec`，返回固定 revision 的可写任务 `WorkspaceHandle`：

```json
{
  "workspace_id": "...",
  "root": "/workspaces/.../repo",
  "provider": "git",
  "repository": "owner/repo",
  "base_sha": "...",
  "head_sha": "...",
  "tree_sha": "...",
  "mode": "read-write",
  "manifest_digest": "...",
  "lease_expires_at": "..."
}
```

Runtime 只接受 Handle 映射出的任务路径；Agent 可以在其中修改文件、执行命令并生成 diff，但 clone
凭据、Provider 凭据和 Orchestrator 服务凭据不进入 Task 环境。

### 8.6 Execution Environment

默认使用轻量级 B 模式：长期运行 Runtime 容器，每个 Task 获得独立进程组、Workspace 和可丢弃的
环境目录。环境由 `ExecutionEnvironmentProvider` 创建，Agent Run 结束后整体删除。

环境来源分为三层：

1. **builtin**：Runtime 镜像内已经安装的 Node、pi SDK、常用构建工具和内置 Skill；
2. **prebuilt template**：按稳定 ID 和镜像 digest 标识的预构建环境，可通过 copy-on-write、reflink
   或只读基础目录加 Task overlay 快速克隆；
3. **task overlay**：Task 根据 ResolvedPreset 使用 npm 安装额外 Skill/package，写入 Task 本地目录，
   复用只读 npm cache，不修改 builtin 或 template。

```yaml
executionEnvironment:
  mode: prebuilt-clone
  template: node-review-v1
  image: review-agent-runtime@sha256:<digest>
  npmCache: shared-read-only
  skills:
    - package: "@example/security-review-skill@2.1.0"
  cleanup: always
```

对于强隔离或依赖冲突任务，可以把 `mode` 切换为 `ephemeral-container`，从相同预构建 OCI image
启动一次性容器。两种模式使用相同的 WorkspaceHandle、SessionArchive 和 Agent Run 接口，因此不影响
上层 Task 生命周期。

Agent 具有 shell 能力时可以继续在 task overlay 中安装依赖；安装命令、stdout/stderr 和最终环境
元数据进入 SessionArchive。框架不限制 Skill 的执行能力，但任何安装都不能修改共享模板。

这里不建设细粒度权限策略，但长期 Runtime 容器仍提供固定的基础隔离：Controller 和 Task 子进程
使用不同 OS 身份；Task 只能写 Workspace/overlay；子进程环境不继承 Runtime Token、Provider/Git
凭据或模型 API Key；不挂载 Docker socket 和宿主机敏感目录。模型请求由 Controller 发起。Task 默认
允许出站网络以支持 npm、构建和测试，但不能凭空获得外部系统身份。

Runtime 在默认 system instructions 中说明 Workspace 范围、凭据边界和外部副作用提交方式，帮助
Agent 正确使用完整能力；这些提示只表达操作约定，不作为安全隔离机制。

## 9. AgentSpec 收缩

当前 AgentDefinition 应收缩为接近以下契约：

```ts
interface AgentSpec {
  id: string;
  inputSchema: TSchema;
  outputSchema: TSchema;
  systemInstructions: string;
  buildTaskPrompt(input: JsonObject, context: ContextManifest): string;
  skillRefs: string[];
  toolIds: string[];
  limits: {
    maxTurns: number;
    maxToolCalls: number;
    maxResultBytes: number;
  };
  validateOutput?(output: JsonObject, workspace: WorkspaceHandle): Promise<void>;
}
```

不再把以下内容作为首期 Agent 运行时维度：

- 同一 Runtime 内多语义版本自动选择；
- 普通 Task 请求直接覆盖 Skill；
- Agent 级 Provider/Base URL override；
- 未被产品使用的 human-input/steer/follow-up 策略矩阵；
- 为发现而发现的完整 Agent Catalog API。

审计使用 `agent_spec_digest + runtime_release`，不依赖 Runtime 动态保留多个语义版本。

## 10. 一次任务的完整执行序列

```text
1. Ingress 验签并持久化 ProviderEventInbox
2. 规则匹配 Capability，事务内创建 Task
3. Scheduler 按 queue/priority/concurrency_key 领取
4. Executor 按字段级规则解析 ResolvedPreset 并写入 Task 元数据
5. Context Builder 构造本次 Session 初始输入
6. Environment/Workspace Provisioner 准备 Task overlay 和固定 revision WorkspaceHandle
7. Executor 调用 Runtime，并关联 TaskAttempt 与 Agent Run
8. Runtime 执行 Tool loop，记录消息、命令和 Tool Call，提交结构化结果
9. Executor 独立校验结果和引用，永久写入 SessionArchive
10. 同一事务保存 execution result 并写 DeliveryOutbox
11. Publisher 幂等创建或更新 Provider 消息
12. 更新 delivery status 和 Task 总状态，清理 Task 环境并释放 lease
```

Runtime 崩溃只影响当前 Attempt；Task 控制面根据失败分类创建下一 Attempt。Provider 发布失败只重试
Outbox，不重新执行 Agent。

## 11. 扩展开发路径

### 11.1 新增配置型 Agent 能力

开发者只需要：

1. 新建 Capability Pack；
2. 定义输入/输出 Schema；
3. 选择现有 Context Builder、Workspace 类型和 Tool；
4. 编写 Prompt 与 Skill；
5. 添加 Prompt snapshot、Schema、Tool allowlist 和 faux-provider eval；
6. CI 校验后随应用发布。

公共 Scheduler、Executor、Runtime Server 和 Provider Adapter 不修改。

### 11.2 新增确定性能力

- 新消息来源：实现 Ingress Adapter，输出统一 `MessageEnvelope`；
- 新代码来源：实现 Workspace Provider，输出统一 `WorkspaceHandle`；
- 新上下文：实现纯 Context Builder，输出 ContextItem；
- 新 Agent 操作：实现 Tool，并注册 Schema、执行和审计元数据；
- 新投递渠道：实现 Delivery Adapter，消费 Outbox。

这些扩展都不进入 Agent Prompt 控制流。

### 11.3 何时升级为 Graph

同时满足以下任一条件再评估 LangGraph/Workflow Engine：

- 有三个以上需要独立重试和观测的认知节点；
- 安全、性能、测试等 Agent 需要并行执行并汇总；
- 业务要求人工审批后从中间状态恢复；
- 流程分支由结构化状态决定，而不是 Agent 内部 Tool loop；
- 单个 Task 需要跨小时或跨天等待外部事件。

Graph 节点仍复用 Task、Context、Workspace、Runtime 和 Outbox 契约，避免推倒重来。

## 12. 渐进迁移计划

### 阶段 0：冻结平台化扩张

- 不继续增加 Agent 动态版本、任意覆盖或交互策略；
- 将本文作为后续实现边界；
- 记录当前 Review 和 message command 的行为基线。

验收：现有测试保持通过，新设计不改变外部 Bot 行为。

### 阶段 1：补齐 Task 调度控制面

- 为可执行任务增加 `queue`、`priority`、`available_at`、`concurrency_key`、
  `resource_class` 和统一 lease 语义；
- 抽取一个 Scheduler claim 实现，Review 与 AgentTask 不再各写一套领取逻辑；
- 增加 priority、FIFO、aging、并发键、lease 过期和取消测试；
- 先用数据库队列，保留未来 Scheduler Adapter 接口。

验收：交互命令可以稳定优先于自动 Review，同一 PR 消息保持顺序，无任务永久饥饿。

### 阶段 2：确定性步骤与 Outbox

- 将 Workspace 准备、Agent 调用和 Provider 发布拆为明确步骤；
- 引入 DeliveryOutbox，发布失败不再触发 Agent 重跑；
- 统一错误分类和 retry ownership；
- Agent Runtime 请求只接收已准备 Workspace Handle。

验收：模拟 Runtime、Git 和 Provider 分别失败时，只重试对应步骤，且无重复评论。

### 阶段 3：SessionArchive 与可解释审计

- 增加 TaskAttempt、TaskEvent 和永久 SessionArchive；
- 保存 ResolvedPreset、Skill package refs、显式 Tool、Workspace SHA 和模型元数据；
- Tool Call 和 shell 记录输入、输出或截断内容、退出状态和时间；
- 入库前执行 secret redaction，并提供按 Task 查询的审计视图；
- 只把 ContextBundle、完整内容 digest 和 RunManifest 留作后续扩展点。

验收：任意已完成任务都能查看永久 Session、Task 元数据、commit/diff、Skill packages、Tool Call、
模型配置和投递证据；敏感值不进入 SessionArchive。

### 阶段 4：Runtime 瘦身与 Capability Pack

- 将 AgentDefinition 收缩为 AgentSpec；
- 移除未被产品使用的动态版本、普通请求直接覆盖 Skill/model 的能力和交互端点；
- `code-review` 与 `pr-assistant` 迁移为两个 Capability Pack；
- `change-summary` 降为测试 fixture，除非它有真实入口；
- 自动校验 Pack 内 Schema、Skill、Tool 和 Prompt snapshot。

验收：新增一个使用现有组件的测试 Capability 不修改 Scheduler、Executor、Runtime Server；生产
Runtime 的配置组合维度明显减少。

### 阶段 5：按指标决定扩容方式

只有出现数据库 claim 争用、队列延迟或复杂流程需求时，才选择：

- 将 Scheduler Adapter 切换到专用 Broker；或
- 为复杂 Capability 增加 LangGraph/Temporal Workflow Executor。

Task Envelope、ResolvedPreset、SessionArchive、Workspace Handle 和 Outbox 契约保持不变。

## 13. 保留、调整与删除清单

### 保留

- ProviderEventInbox 与 Provider Adapter；
- Workspace cache、固定 SHA、凭据隔离和 lease；
- pi SDK Session 和仓库读写、命令执行 Tool；
- 输入/输出 Schema、唯一完成 Tool、执行预算；
- Python/Node 进程隔离；
- Provider 评论幂等 marker。

### 调整

- ReviewRun/AgentTask 的执行字段逐步迁到 Task/TaskAttempt 控制面；
- 两套 claim 逻辑统一为 Scheduler；
- Runtime Session 降为 Agent Run，不再作为业务状态源；
- Profile 收缩为 Capability 内命名 preset；
- Skill 和 Tool 选择在 Task 开始前解析并冻结；
- 评论发布改为 Outbox。

### 删除或暂缓

- 无真实流量的生产示例 Agent；
- 任意调用方 Skill、Provider、Model Base URL 覆盖；
- 当前未使用的实时 human input、steer、follow-up 通道；
- Runtime 内多版本自动路由；
- 在单 Agent 循环外再包一层 LangGraph。

## 14. 架构验收标准

最终设计和实现必须同时满足：

1. 新增配置型 Capability 不修改公共 Scheduler、Executor 和 Runtime Server；
2. 代码下载和 Provider 消息发布不出现在 Agent Tool allowlist；
3. 调度支持 queue、priority、available time、lease、并发键和防饥饿；
4. 发布失败不会重新运行 Agent；
5. 每次 Agent Run 的 Session 内容和 Task 元数据经过脱敏后永久入库；
6. 能从审计记录解释代码 SHA/diff、Session 消息、Skill package、显式 Tool、模型配置、Tool Call
   和最终投递；当前不宣称完整输入可重建；
7. Skill 不隐式增加 Tool，普通 Task 输入不能直接覆盖 system instructions、Tool 实现或 Base URL；
8. Workspace 只由 Provisioner 创建，Runtime 只接受受信 Handle；
9. 同一 PR 的消息任务严格有序，不同 PR 可以并行；
10. 单 Agent 能力不依赖 LangGraph，未来 Graph 节点仍能复用相同基础契约。

这组标准比代码行数更适合作为“是否快速、可扩展且不过度平台化”的判断依据。
