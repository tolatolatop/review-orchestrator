# Review Orchestrator 目录架构

后端源码按职责分为五个层级。业务实现只能从这些层级目录导入其他实现；仓库根部的
同名模块仅用于兼容旧导入路径，不承载业务逻辑。

```text
src/review_orchestrator/
├── presentation/     # FastAPI 路由、应用装配、Dashboard
├── application/      # Review 生命周期用例、Worker 与 CLI
├── domain/           # 持久化实体、数据契约、结果解析、Finding 对账
├── integrations/     # GitHub/GitLab、评论发布、pi-agent、权限诊断
├── infrastructure/   # 配置、数据库、可观测性、隔离 Git Workspace
├── _compat.py        # 旧模块路径到新实现模块的别名工具
└── *.py              # 兼容入口，例如 main.py、services.py、github.py
```

## 层级职责

### `presentation`

- `main.py`：构造 FastAPI 应用，管理数据库和平台客户端生命周期。
- `api.py`：HTTP 路由、依赖注入以及领域错误到 HTTP 状态码的映射。
- `dashboard.py`、`reviews_dashboard.py`：无外部依赖的运维页面资源。

### `application`

- `services.py`：ReviewRun、AgentTask、Webhook、Session 的应用用例和查询。
- `worker.py`：异步领取任务、Workspace 准备、Agent 执行、超时与发布编排。
- `worker_cli.py`：独立 Worker 进程入口。

### `domain`

- `models.py`：ReviewRun、Finding、ProviderEvent、Workspace 等持久化实体。
- `schemas.py`：API 与应用层共享的数据契约。
- `review_results.py`：结构化结果校验、路径规范化和 fingerprint。
- `reconciliation.py`：跨 ReviewRun 的 finding 新增、延续和解决状态对账。

### `integrations`

- `providers.py`：Provider-neutral adapter 协议、错误边界与 registry。
- `github.py`、`gitlab.py`、`github_auth.py`：代码托管平台实现。
- `comments.py`：Summary、行评论和 AgentTask 评论发布。
- `pi_agent.py`：隔离 Agent Runtime 的 HTTP 客户端和契约。
- `platform_diagnostics.py`：平台权限只读诊断。

### `infrastructure`

- `config.py`：环境配置。
- `db.py`：数据库引擎、Session 和启动迁移。
- `workspaces.py`：Git Workspace、lease、cache、清理及凭据安全。
- `observability.py`：分页结构与敏感信息脱敏。

## 导入与兼容规则

1. 新代码使用上述层级路径，例如
   `review_orchestrator.application.services`。
2. 层级目录中的实现不得导入根部兼容模块，避免真实依赖关系被别名隐藏。
3. `review_orchestrator.main`、`review_orchestrator.services` 等旧路径继续可用，且与
   新路径指向同一个 Python module 对象；已有 monkeypatch 和外部入口不会失效。
4. ASGI 与 Worker 的正式入口分别是
   `review_orchestrator.presentation.main:app` 和
   `review_orchestrator.application.worker_cli:main`。

## 重构测试门禁

目录调整前建立覆盖率基线并补充公共路由、错误契约、状态机、Provider 归一化和
Workspace/Git 边界测试。持续集成应运行：

```bash
uv run ruff check .
uv run pytest --cov=review_orchestrator --cov-branch
npm --prefix pi-agent-runtime test
```

Python 分支覆盖率门禁记录在 `pyproject.toml`；任何后续拆分不得降低该基线。
