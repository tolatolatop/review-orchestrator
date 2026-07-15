# pi-agent 独立 Runtime 与扩展契约

状态：已按 Agent/Task 重规划实现。

## 1. 模块边界

pi-agent Runtime 是独立 Node 模块，只负责一次 Agent Run：

```text
run(installed agent, domain preset, session input, workspace path)
  -> pi Session + explicit Tools
  -> structured result + permanent archive payload
```

Runtime 不负责 Provider 事件、Git clone/fetch、业务重试、Task 调度或评论投递。
这些能力属于 Python Orchestrator。Runtime 也不把 Session 当成业务 Task 状态源。

生产注册只包含：

| Agent | Task Type | 完成 Tool |
| --- | --- | --- |
| `code-review` | `code-review` | `submit_review` |
| `pr-assistant` | `message-command` | `submit_task_result` |

`change-summary` 仅保留为测试 fixture，用来验证 AgentDefinition/Runner 的扩展性，
不注册为生产能力，也没有 Catalog API。

## 2. 启动契约与 preset

`POST /v1/sessions` 的可配置选择量只有：

```json
{
  "agent_id": "pr-assistant",
  "task_type": "message-command",
  "repository_skills": ["builtin:pr-assistant"],
  "workspace_path": "/workspaces/.../repo",
  "input": {},
  "idempotency_key": "agent-task:<id>:attempt:1"
}
```

请求不能提交 `agent_version`、`profile`、`model`、`base_url` 或任意 `skills`
覆盖。模型及 Base URL 是 Runtime 部署配置；Task Type 只能映射到 Agent 内已经安装
的命名 preset。

解析使用字段级所有权：

1. Agent definition 提供 system instructions、默认 Skill、Tool、模型规则、预算和结果 Schema；
2. Repository Skills 只改变 Skill refs；
3. Task Type 选择 Agent 内部命名 preset，并对该 preset 拥有的 Tool、预算等字段取最终值；
4. Skill 不增加 Tool，Tool 之间的依赖也不由框架推断。

Runtime 返回并持久化 `resolved_preset`，包含：

- Agent ID、发布版本和 definition digest；
- 三层组合来源；
- 模型、thinking level；
- Skill refs 与内容 digest；
- 显式 Tool 列表；
- turn/tool/result 限制；
- Execution Environment 类型和模板。

同一 Runtime 不根据请求动态路由多个语义版本。升级通过发布新的 Runtime 完成，
审计依赖 definition digest 和 Runtime release。

## 3. Skill 来源

Skill ref 支持三种形式：

- `builtin:<name>` 或兼容的裸 `<name>`：读取只读 `PI_AGENT_SKILLS_ROOT`；
- `prebuilt:<name>`：读取克隆模板的 `skills/<name>/SKILL.md`；
- `npm:<package>`：在 Task overlay 中执行正常的 `npm install`；package 可用
  `piAgentSkill` 指定 `SKILL.md` 路径，也可写成 `npm:<package>::<path>`。

Skill 安装脚本和后续命令拥有 Task 环境内的完整执行能力。框架不建设 Skill 沙箱，
但安装只修改 Task overlay，不修改 builtin 或 prebuilt 模板。Skill 名称、来源、安装结果
和内容 digest 随 Session/Task 元数据保存。

## 4. Tool 能力

两个生产 Agent 均显式获得：

- `repository.list-files`；
- `repository.read-file`；
- `repository.search-code`；
- `repository.git-diff`；
- `workspace.write-file`；
- `workspace.shell`；
- 与结果 Schema 一一对应的唯一完成 Tool。

Agent 可以修改 Workspace、安装依赖、执行构建和测试。路径型 Tool 拒绝 `..`、绝对路径
和 symlink escape。Shell 可以在 Workspace 内使用任意命令，但没有 Provider/Git/Runtime
凭据，也不负责发布评论或 push。

Tool Registry 是独立的 `id -> factory` 映射。注册 Tool 不声明依赖，加载 Skill 也不会
隐式注册 Tool；组合正确性由 Agent/Capability 作者和测试负责。

## 5. Execution Environment

默认是轻量级 B：长期 Controller 容器 + 每 Task 独立 Workspace、子进程和可丢弃 overlay。

`TaskOverlayExecutionEnvironmentProvider`：

1. 克隆 `PI_AGENT_ENVIRONMENT_TEMPLATE_ROOT`；
2. 在 overlay 中安装 npm Skill/package；
3. 把 overlay 的 `node_modules/.bin` 加入 Task PATH；
4. Agent Run 结束后删除整个 overlay。

Compose 把数据库/Git cache 与 Workspace 拆成不同 volume。Runtime 只挂载 Workspace，
不挂载 Orchestrator 数据和 secrets。Controller 持有模型密钥并使用 root UID；shell/npm
子进程从 20000～60000 的运行中 UID 池分配不同 UID。运行前 Workspace 临时交给该 Task
UID，结束后恢复给 Orchestrator
UID 1000。子进程环境只包含 PATH、HOME、LANG 和 npm cache，不继承 Runtime Token、
模型 Key 或 Provider/Git 凭据。

需要更强隔离时，可实现另一个 `ExecutionEnvironmentProvider`，用同一契约启动一次性
OCI 容器；这不会改变 Task、SessionArchive 或 Delivery Outbox。

## 6. Session 与审计

Runtime 导出：

- Session header 和全部 entries；
- active branch；
- pi 构建的 Session context；
- token/turn 等 Session stats；
- Tool events、结构化结果和执行环境元数据。

Orchestrator 对数据和 Workspace diff 脱敏后永久写入 `SessionArchive`，并关联
`TaskAttempt`。当前目标是可解释，不承诺从归档精确重建模型调用。

Runtime 不再提供 human-input、steer 或 follow-up 路由。控制面只保留 Session 查询、
同步和取消。

## 7. 新 Agent 的扩展步骤

新增 Agent 需要：

1. 实现 `AgentDefinition`：输入/输出 Schema、instructions、prompt builder、Task Type
   preset、显式 Tool 和预算；
2. 提供 builtin/prebuilt/npm Skill；
3. 在 `agents/index.ts` 安装一个生产 definition；
4. 在 Orchestrator 增加确定性 trigger/context/delivery 接线；
5. 增加输入输出、preset、Tool allowlist、prompt snapshot 和 pi faux-provider eval。

如果新能力只复用已有 Agent、Workspace、Context、Tool 和 Delivery，它应优先成为新的
Task Type/Repository Skill 组合，而不是复制 Runner 或 Scheduler。
