# 主线程 + Luna 子代理 + PR Stack 工作流

## 目的与适用条件

本流程用于复杂、可拆分的软件变更。主线程掌握设计、依赖、审核和集成；`luna_worker` 只实现已设计好的窄任务。简单单文件修改不必强行拆 Stack，但仍须遵守仓库检查、最小修改和验证要求。

PR Stack 是一组按依赖关系排列、可独立审核和回滚的本地分支/commit。创建、push 或合并远端 PR 必须另获用户明确授权。

## 阶段一：现状检查

主线程先读取相关 `AGENTS.md`、README、构建与测试配置，并执行：

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git worktree list
git log --oneline --decorate -10
```

记录用户已有改动、基础分支、构建工具、测试入口、共享生成物和不可并发资源。已有改动不得被覆盖、移动或混入 Stack。

## 阶段二：输出完整 Stack 计划

实现前主线程使用以下模板逐项输出，未经规划的 Stack 不得派发：

```text
Stack: S<n> - <名称>
目标: <单一可审查结果>
基础分支: <branch 或 commit>
前置依赖: <无 | S<n>>
允许修改: <精确文件或目录>
禁止修改: <精确文件或目录；至少列出业务外和共享敏感区域>
文件所有权: <本 Stack 独占的文件/目录>
实现边界: <要做与明确不做的内容>
验收标准: <可观察、可验证条件>
测试命令: <可直接运行的命令>
回滚方式: <通常为 revert 当前 Stack commit>
允许并行: <是/否及依据>
子代理: <luna_worker | 主线程>
```

主线程绘制依赖顺序：无依赖且所有权不重叠的 Stack 可从共同基础并行；有依赖的 Stack 从直接前置 Stack 的已审核 commit 创建。相同文件、共享生成文件、数据库迁移、锁文件、公共接口先后变更或同一可变测试环境均视为冲突，必须串行。

## 阶段三：建立真实隔离

写任务必须一 Stack 一分支、一 worktree、一代理线程。建议分支名为 `codex/stack-<nn>-<slug>`，worktree 放在仓库外的明确目录。主线程可用下列方式创建：

```bash
git worktree add -b codex/stack-<nn>-<slug> <worktree-path> <base-branch-or-commit>
git worktree list
git -C <worktree-path> branch --show-current
git -C <worktree-path> status --short
```

Windows PowerShell 中必须把 `<worktree-path>` 替换为已核验的绝对路径，并在涉及删除或移动前再次解析目标。不得在仓库工作区内嵌套 worktree。

桌面端可以为独立任务创建托管 worktree，但自定义 agent TOML 不会自动保证每个被 spawn 的子代理拥有独立 worktree。因此每次派发都要核验真实 `git worktree list`。不能确认隔离时，禁止并行写入：改由主线程显式创建 worktree，或串行执行且同一时刻只有一个写代理使用共享目录。

## 阶段四：派发 Luna

每个写 Stack 使用独立代理线程，并向 `luna_worker` 发送完整契约：

```text
执行 Stack: S<n> - <名称>
基础分支: <branch>
基础 commit: <sha>
worktree: <绝对路径>
分支名称: codex/stack-<nn>-<slug>
允许修改: <列表>
禁止修改: <列表>
任务边界: <实现内容和不做内容>
验收标准: <列表>
测试命令: <列表>
提交要求: 允许创建一个或多个仅包含本 Stack 的本地 commit
完成报告: worktree、分支、commit SHA、修改文件、已执行测试、未执行测试及原因、残余风险
```

主线程不得用模糊任务替代上述字段。代理报告边界不清、冲突或架构问题后，主线程先修订计划和契约再继续。

## 阶段五：主线程实证审核

Luna 返回后，主线程进入该 worktree，实际执行：

```bash
git worktree list
git branch --show-current
git status --short
git log --oneline --decorate -10
git diff --stat <基础分支>...HEAD
git diff <基础分支>...HEAD
```

若基础分支是易漂移名称，同时记录派发时的基础 commit，并按需比较 `<base-sha>...HEAD`。审核必须覆盖：

- 当前路径、分支、基础 commit 与派发契约一致；
- diff 中每个文件都属于允许范围和本 Stack 所有权；
- `git status --short` 中没有遗漏的 staged、unstaged 或 untracked 文件；
- manifest、锁文件、生成物中没有未经批准的依赖或改动；
- 没有格式噪声、无关重构、兼容性漂移或测试弱化；
- 验收标准逐条有证据；
- 主线程按风险独立复跑测试、构建、Lint、类型检查或静态检查。

不通过时，把具体文件、现象、期望和验证命令发回原代理线程，让其在原 worktree 和原分支修正。除非原线程不可恢复或任务已被重新拆分，不新建修复代理。

## 阶段六：Stack 推进与 PR 管理

一个 Stack 审核通过后，才允许基于其 HEAD 创建直接后继 Stack。并行 Stack 合流前，主线程重新确认共同基础和冲突情况。

每个远端 PR 应对应清晰 Stack，并在描述中写明前置 PR。主线程负责提交整理、push 和创建 PR；这些外部操作仅在用户明确授权后执行。合并顺序按依赖从底到顶；底层 Stack 变化后，主线程评估后继 Stack 是否需要 rebase、重测和重新审核。

单个 Stack 的默认回滚方式是 revert 其独立 commit 或 PR。不得用 `git reset --hard`、强制 push 或删除未合并 worktree 代替可审计回滚，除非用户明确授权且目标已核验。

## 阶段七：最终集成回归

所有 Stack 完成后，主线程在最终集成状态：

1. 运行完整回归验证，而非仅汇总各 Stack 的局部测试。
2. 检查 Stack 间接口、行为、配置、生成物和迁移顺序。
3. 获取最终 `git status --short`、完整 diff、分支和 commit 状态。
4. 汇总实际修改文件、测试/构建证据、分支/commit/PR 状态。
5. 明确列出未执行验证、原因和残余风险。
6. 逐条确认总体验收条件；未满足时继续修正，不宣告完成。

## 能力边界

- 项目配置可以启用多代理、限制并发，并定义 `luna_worker` 的模型、推理等级、沙箱和持久指令。
- Codex 桌面端和 Git 均支持 worktree；主线程也可以显式运行 `git worktree add`。
- agent 配置不能声明文件级写权限、自动分支、自动 worktree、PR Stack 依赖图或禁止 push 的机械策略；这些由本文件、`AGENTS.md`、派发契约和主线程实证审核执行。
- 运行时权限覆盖或组织策略可能高于 agent 文件。派发前必须检查实际权限和 worktree，不能只依赖静态配置。
