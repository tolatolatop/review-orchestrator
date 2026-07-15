# 目录重构测试覆盖审计

本次目录调整先建立可重复的测试基线，再补充重构保护，最后移动实现代码。

## 重构前结论

- 原测试集共 155 个用例。
- 初次覆盖率运行发现测试会读取开发机 `.env`，导致 56 个测试尝试加载部署路径
  `/run/secrets/github-app.pem`；测试基座并未与本地凭据隔离。
- 修复测试环境隔离后，155 个用例全部通过，初始分支覆盖率为 76%。
- 主要缺口集中在公共路由清单、资源不存在/Runtime 未配置错误契约、Review Session
  状态矩阵、Provider PR/MR 身份归一化，以及 Workspace/Git 失败与 lease 生命周期。

因此，原测试集不足以直接保护目录重构。

## 重构前后补充的保护

- 增加 `pytest-cov`，启用 branch coverage，并在 `pyproject.toml` 固化 80% 门禁。
- 新增测试环境隔离 fixture，测试不再读取开发机 `.env`。
- 新增完整公共路由 inventory 和旧模块路径兼容契约。
- 新增分层实现不得反向导入根部兼容模块的架构约束。
- 新增 ReviewRun 运行状态、锁、软/硬超时和 Worker 状态矩阵。
- 新增 Session start/sync/cancel、Runtime 错误分类和诊断回退矩阵。
- 新增 GitHub/GitLab 身份归一化、coalesce key 和过滤器特征测试。
- 新增 Workspace force refresh、Git cache、Git 错误分类、多 lease、过期清理、
  清理失败和动态 token 失败测试。

## 最终证据

```text
Python tests:       224 passed
Branch coverage:   80.40% (required: 80%)
pi-agent runtime:  7 passed
Ruff:              passed
Wheel build:       passed
Compose config:    passed (local and self-host)
```

覆盖率不是“所有未来行为均已证明”的同义词。真实 PostgreSQL、真实 GitHub/GitLab 和
真实 LLM 调用仍按项目既有策略不属于默认本地测试；目录重构所触及的内部模块边界、
入口、状态机和兼容路径已经纳入可重复的本地门禁。
