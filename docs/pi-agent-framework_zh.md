# pi-agent Agent 框架

`pi-agent-runtime` 是一个按 Agent Definition 执行任务的 runtime。Review 和 PR
问答是内置 Agent，不再是 Runner 中的固定执行分支。旧的 `kind=review` 和
`kind=instruction` 只在 HTTP 输入边界映射到对应 Agent。

## Agent 模型

每个 Agent 完整声明：

- 稳定的 `id` 和语义化 `version`；
- TypeBox 输入和输出 Schema；
- 标题、任务 Prompt 和 System Prompt；
- 主 Skill、辅助 Skills 及是否允许调用方覆盖；
- Tool Registry 中允许使用的工具；
- 模型覆盖策略和允许的 thinking level；
- 人工输入、steer 和 follow-up 交互策略；
- 最大 turn、工具调用和结果字节数；
- 唯一的结构化结果提交工具。

核心契约位于：

```text
pi-agent-runtime/src/
├── agent/
│   ├── types.ts       # Agent Definition 和运行时状态契约
│   ├── registry.ts    # Agent 注册、版本解析、输入输出校验、Profile 解析
│   ├── runner.ts      # 通用 pi SDK 执行循环
│   ├── skills.ts      # 多 Skill 加载、顺序组合和 SHA-256 摘要
│   └── limits.ts      # turn/tool-call 预算
├── agents/
│   ├── code-review.ts
│   ├── pr-assistant.ts
│   └── change-summary.ts
└── tools/
    ├── registry.ts
    ├── repository.ts
    ├── interaction.ts
    └── completion.ts
```

Runner 只依赖 `AgentDefinition`，不判断 Agent ID、Review 类型或结果工具名称。

## 内置 Agent

| Agent | 版本 | Profile | 结果工具 | 说明 |
| --- | --- | --- | --- | --- |
| `code-review` | `1.0.0` | `default`, `fast`, `strict` | `submit_review` | 结构化 PR 缺陷审查。`strict` 自动组合 `security-analysis` Skill。 |
| `pr-assistant` | `1.0.0` | `default`, `concise`, `thorough`, `strict` | `submit_task_result` | 使用只读证据回答 PR 命令。 |
| `change-summary` | `1.0.0` | `default`, `concise`, `deep` | `submit_change_summary` | 面向开发、Review 或发布角色解释变更，用于验证非 Review Agent 的扩展能力。 |

`GET /v1/agents` 返回注册 Agent 的版本、Profile、Schema、Skill、Tool、模型策略、
交互策略和执行限制。

## 通用启动协议

新调用方应使用 `agent_id + agent_version + input`：

```json
{
  "agent_id": "change-summary",
  "agent_version": "1.0.0",
  "workspace_path": "/var/lib/review-orchestrator/workspaces/.../repo",
  "profile": "deep",
  "skills": ["change-summary"],
  "input": {
    "repository_context": {
      "provider": "github",
      "repo_full_name": "example/repo",
      "pr_number": 42,
      "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "audience": "release-manager",
    "focus": "database compatibility"
  }
}
```

Runtime 在创建 Session 前完成 Agent 版本解析、输入 Schema、Profile、Skill 名称和
模型策略校验。Python `PiAgentClient.start_agent_session()` 暴露同一个通用协议；
`start_session()` 和 `start_instruction_session()` 是现有业务的类型化包装。

## Profile 解析

Profile 是实际执行配置，而非元数据字符串。解析顺序是：

1. Runtime 部署默认模型形成基础配置；
2. 应用合法的 Session 模型和 Skill 覆盖；
3. 应用命名 Profile 的模型、Skill、Tool 和预算覆盖；
4. 最后使用 Agent 固有 allowlist 校验结果。

未知 Profile、非法 thinking level、越过模型策略或重复 Tool 会在启动前失败。

## Skill 组合

Agent 区分一个主 Skill 和零到多个辅助 Skill。加载顺序固定为主 Skill在前，辅助
Skill 按定义顺序追加。Runtime 校验目录名与 `SKILL.md` frontmatter 的 `name`
一致，将所有 Skill 内容组合进 System Prompt，并在 Session 中记录每个文件的
SHA-256 摘要。

Skill 只能提供行为指导，不能自行扩大 Agent 的 Tool 权限或替换完成协议。

## Tool 与完成协议

Agent Definition 使用注册 ID 选择工具，例如：

```ts
tools: [
  "repository.list-files",
  "repository.read-file",
  "repository.search-code",
  "repository.git-diff",
]
```

Tool Registry 将 ID 解析为 pi SDK 工具，并拒绝未知 ID 或重复的运行时工具名称。
结果提交工具由 `AgentResultDefinition` 通用创建，执行时依次校验：

1. 只能提交一次；
2. 结果符合 Agent 输出 Schema；
3. JSON 字节数没有超过 Agent/Profile 限制；
4. 通过 Agent 可选的语义校验，例如引用文件和行号；
5. 校验成功后才把 Session 标记为 `completed`。

Agent 正常结束但没有调用完成工具时统一进入 `missing_result`。

## 交互和执行预算

Agent 独立声明是否允许：

- `request_human_input`；
- 运行中的 `steer`；
- 当前工作完成后的 `follow_up`。

Runner 对每个 `turn_start` 和 `tool_execution_start` 计数。超过 Profile 解析后的预算
会中止 SDK Session，并以 `execution_limit_exceeded` 失败。状态统一使用
`starting`、`preparing`、`running`、`waiting_for_input`、
`validating_result`、`completed`、`failed` 和 `cancelled`。

## 新增 Agent

新增 Agent 只需要：

1. 在 `src/agents/` 新增一个实现 `AgentDefinition` 的模块；
2. 在 `src/agents/index.ts` 注册该定义；
3. 在 `skills/<name>/SKILL.md` 添加主 Skill，必要时添加辅助 Skill；
4. 添加输入/输出、Prompt、Profile、Tool allowlist 和 pi faux-provider eval 测试。

不需要修改 `AgentRunner`、HTTP Session 路由、完成工具实现或现有 Agent。

## 测试与评测

```bash
npm --prefix pi-agent-runtime test
```

测试覆盖：

- Agent 注册、版本选择和重复注册；
- 通用启动请求与输入 Schema；
- 输出 Schema 和结果大小限制；
- Prompt 快照；
- Tool allowlist；
- 主/辅助 Skill 顺序、frontmatter 和摘要；
- Profile 对模型、Skill 和预算的真实影响；
- turn/tool-call 预算；
- 未调用完成工具；
- 人工输入暂停和恢复；
- `code-review`、`pr-assistant`、`change-summary` 的 pi SDK faux-provider eval。
