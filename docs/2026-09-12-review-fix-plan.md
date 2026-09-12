# 近期提交审查修复计划

> 日期：2026-09-12（UTC+8）
> 状态：S49–S51 待实施；S52 播放列表启动时序修复已在 `65bcdd9` 完成并随
> `2026.09.12.3-beta` 发布。本文仍只维护 S49–S51 计划。
> 设计基线（历史）：`beta`，`5d452ff2ef6d7f932844206caad5339e80743d43`
> 总体路线图：[项目当前状态与后续开发计划](项目当前状态与后续开发计划.md)

## 1. 目标与证据

审查覆盖 `7042678` 至 `5d452ff` 的 8 个提交。该历史基线在 Windows Python 3.13.14、
Node 24.18.1 下执行 `python -u -m unittest discover -s tests`，287 项通过，
包含 Node 用户脚本桥接测试；`git diff --check 1742b16..HEAD` 通过。
以下三项通过额外的模拟复现确认，尚未加入正式回归测试；实现时先将复现固化为测试。
现有测试通过不能代替这三项验收，也不能代替真实客户端验收。

S52 在此计划形成后独立完成：新增 mpv 播放列表首播时序等待、超时和 IPC 关闭保护，
并将对应测试纳入当前 beta 回归；S52 不作为 S49–S51 的前置依赖。

| 问题 | 引入提交 | 复现条件与结果 | 修复目标 |
| --- | --- | --- | --- |
| P1：STRM 多版本选错本地文件 | `493e271` | 条目路径为 A.strm，选中 B 的路径型 MediaSource，开启本地 STRM 推导；结果 MediaSourceId 为 B，media_path 却为 A.mkv | 选中版本、sidecar、实际媒体路径一致 |
| P2：预热失败仍播放缓存 | `7042678` | 从头播放、缓存进度为 0、首段返回 403；缓存不存在，调用方仍发送缓存路径 | 预热成功后才选择缓存，失败保留可用的原媒体地址 |
| P2：实例锁之前启动下载线程 | `d7b4b46` | GUI 与 auto_resume 开启；导入 HTTP 模块时先创建 update_db_loop 和非 daemon 的 resume_or_pause 线程，之后才申请失败的实例锁 | 第二实例在初始化下载任务前退出 |

稳定频道影响：本地 `origin/stable@e98e2c3` 包含 `7042678`，其 `utils/downloader.py`
与当前 beta 一致，因此缓存问题也影响该 stable 代码基线。S50 必须保持可独立移植，
通过验收后评估 stable 单独补丁；届时先确认最新 stable 基线并补齐回移 Stack 契约，
只移植缓存修复及对应测试。stable 补丁验证与发布不依赖 S49/S51 功能进入稳定频道。

## 2. 执行顺序与共同门禁

计划编号为 S49、S50、S51，实施前仍需检查编号、分支和 worktree 是否已被占用。

```text
beta@5d452ff → S49 STRM 版本路径 → S50 缓存预热结果 → S51 实例锁初始化顺序
                                                        ↓
                                               主线程集成回归与客户端验收
```

S52 播放列表时序修复已独立集成到当前 beta，不改变上述 S49–S51 的串行依赖。

- 每个 Stack 由一个 `luna_worker` 在独立分支、独立 worktree 中执行；本次不派发实现。
- S50 基于 S49 已审核 commit，S51 基于 S50 已审核 commit；基础 SHA 在派发时填入实际值。
- 采用串行集成。三个修复都涉及起播链路，测试导入共享配置、HTTP 模块和下载管理器；本轮不并行修改或验证。
- 主线程派发前运行 `git worktree list`，核验分支、基础 SHA、工作区状态和文件所有权。
- 所有权只覆盖各 Stack 列出的源码及测试；架构、经验库和路线图统一由主线程在集成后维护。
- 子代理不得扩展允许范围；若需要新增共享接口或修改其他文件，先报告主线程重新划界。
- 创建分支、整理 commit、推送、PR、合并和发布按任务授权与仓库门禁执行；本计划本身不授予这些操作权限。
- 回滚以每项独立 diff/commit 为单位。已集成并提交时，获授权后按 S51 → S50 → S49 的逆序 revert；未提交时仅撤销对应修复补丁，保护用户已有改动。

## 3. S49：STRM 选中版本与本地路径一致

| 字段 | 计划 |
| --- | --- |
| 目标 | 根据选中的 MediaSourceId 确认 sidecar，再推导本地媒体路径 |
| 基础分支 | `beta@5d452ff2ef6d7f932844206caad5339e80743d43`；若 beta 前进，先复核差异 |
| 前置依赖 | 无 |
| 计划分支 | `codex/stack-49-strm-selected-version` |
| 允许修改 / 文件所有权 | `utils/data_parser.py`、`tests/test_clouddrive2_gateway.py`、`tests/test_strm_media_path.py`，由 S49 独占 |
| 禁止修改 | 其余文件，尤其播放器适配、CD2 gateway 实现、用户脚本、配置、更新器和发布脚本 |
| 实现边界 | 修正版本到 sidecar 的定位及主播放/列表一致性；保留现有后缀优先级、路径映射配置和单版本行为 |
| 回滚方式 | 独立撤销 S49 补丁或获授权后 revert S49 commit |
| 允许并行 / 子代理 | 否；一个 `luna_worker` |

实现要点：

1. 先确定选中源，再使用 MediaSourceId 对应的条目路径定位 sidecar；该查找同时支持 HTTP 源和路径型源。
2. 可用条目映射优先，不能用未选中版本的主条目路径直接推导本地文件。单版本仍使用其原始 sidecar。
3. 多版本缺少可靠映射时，停止该项本地推导，使用包含选中 MediaSourceId 的既有服务端流地址；不能把源文件路径当成已确认的 sidecar，也不能改回 A 版本。
4. 保持首集与播放列表路径选择一致；不调整版本偏好规则、公共字段或配置含义。

验收标准：

- 手动选 B、自动偏好选 B 时，源 ID、sidecar、本地路径和 CD2 登记路径均属于 B。
- `Container=strm` 和通过 `.strm` 后缀识别的条目均覆盖；HTTP 源与路径型源均覆盖。
- 缺少 B 映射时返回选中 B 的服务端地址，且不登记错误的本地/CD2 路径。
- 单版本、`Film.mkv.strm`、命名查询参数、纯 pickcode 和未知扩展名维持现有正确行为。
- 首次播放和连播都验证实际路径，不能只断言 MediaSourceId。

测试命令（在 S49 worktree 根目录执行）：

```powershell
python -u -m unittest discover -s tests -p test_strm_media_path.py
python -u -m unittest discover -s tests -p test_clouddrive2_gateway.py
python -u -m unittest discover -s tests -p test_chapters.py
git diff --check
```

## 4. S50：消费缓存预热的成功与失败结果

| 字段 | 计划 |
| --- | --- |
| 目标 | 只有预热成功才将从头播放的媒体地址替换为缓存文件 |
| 基础分支 | `codex/stack-49-strm-selected-version` 的已审核 commit，派发时记录 SHA |
| 前置依赖 | S49 审核通过；作为串行集成基线 |
| 计划分支 | `codex/stack-50-cache-preheat-result` |
| 允许修改 / 文件所有权 | `utils/downloader.py`、`tests/test_downloader_range.py`，由 S50 独占 |
| 禁止修改 | 其余文件，尤其 HTTP 协议、播放器、用户脚本、缓存持久化格式、配置和依赖 |
| 实现边界 | 处理 download_fist_last 的返回结果及其调用方；保留 Range 校验、任务锁、只读保护和有限重试 |
| 回滚方式 | 独立撤销 S50 补丁或获授权后 revert S50 commit |
| 允许并行 / 子代理 | 否；一个 `luna_worker` |

实现要点：

1. `download_play()` 在修改 `media_path` 前保存原媒体地址；首尾预热成功且缓存文件存在，才分派缓存播放。
2. 失败时沿用现有网络播放回退，避免发送不存在或预热不完整的缓存；暂停、取消与只读状态仍遵循原约定。
3. 检查全部 `download_fist_last()` 调用方。恢复下载时消费失败结果，结束本次失败准备流程，避免紧接着启动另一轮下载而抵消有限重试；保留任务状态供后续明确恢复操作使用。
4. 不提高 progress，不删除有效旧缓存，不改变正常已缓存续播和完整下载逻辑；网络播放回退只分派一次。

验收标准：

- 首段 403、无效 Range、首段/尾段短读重试耗尽，都不会分派失败缓存。
- 首段成功但尾段失败，仍使用原媒体地址；进度不伪装为预热成功。
- 首尾成功时选择缓存；已完成缓存和已有进度的正常续播保持可用。
- `play=False` 不触发播放，暂停/取消/只读场景不新增写入。
- 测试通过 DownloadManager 调用真实 Downloader 方法，并检查最终分派地址、文件状态和请求次数，不能仅 mock 预热返回值。

测试命令（在 S50 worktree 根目录执行）：

```powershell
python -u -m unittest discover -s tests -p test_downloader_range.py
python -u -m unittest discover -s tests -p test_discard_prefetch.py
python -u -m unittest discover -s tests -p test_http_server_security.py
git diff --check
```

## 5. S51：取得实例锁后初始化服务

| 字段 | 计划 |
| --- | --- |
| 目标 | 第二实例拿不到锁时，在创建下载管理器、恢复下载和启动服务之前退出 |
| 基础分支 | `codex/stack-50-cache-preheat-result` 的已审核 commit，派发时记录 SHA |
| 前置依赖 | S50 审核通过；作为串行集成基线 |
| 计划分支 | `codex/stack-51-lock-before-service-init` |
| 允许修改 / 文件所有权 | `embyToLocalPlayer.py`、`tests/test_process_cleanup.py`、`tests/test_instance_lock.py`、`tests/test_stop_instance.py`，由 S51 独占 |
| 禁止修改 | 其余文件，尤其锁文件协议、下载器业务、HTTP 路由、配置语义、播放器与发布脚本 |
| 实现边界 | 在入口延后有副作用的初始化，锁外仅保留标准库及 InstanceLock 等必要依赖；配置加载、bundled dependencies、服务导入和后台任务放入成功持锁的 try/finally 范围 |
| 回滚方式 | 独立撤销 S51 补丁或获授权后 revert S51 commit |
| 允许并行 / 子代理 | 否；一个 `luna_worker` |

实现要点：

1. 首先申请现有 OS 锁；失败使用不依赖服务模块初始化的简短错误输出，并返回非零退出码。
2. 持锁后准备 bundled dependencies，再导入依赖它们的业务模块；保留原有可选依赖降级行为。
3. 配置/导入错误、端口冲突、正常 shutdown 都由同一 `finally` 释放锁。
4. 使用入口延迟初始化解决当前问题；全项目配置副作用整理继续留在中期路线图。

验收标准：

- GUI、auto_resume 开启且锁已被其他进程占用时，第二实例限时退出，没有创建下载管理器或启动恢复任务。
- 用独立子进程验证真实入口导入顺序；不能在导入入口完成后才 mock 锁，掩盖导入副作用。
- 正常首实例能启动自动恢复和 HTTP 服务；覆盖持锁后的导入失败、端口冲突和正常关闭后的重新启动。
- 锁竞争与残留锁文件测试通过；未知端口占用者和外部播放器不被终止。

测试命令（在 S51 worktree 根目录执行）：

```powershell
python -u -m unittest discover -s tests -p test_process_cleanup.py
python -u -m unittest discover -s tests -p test_instance_lock.py
python -u -m unittest discover -s tests -p test_stop_instance.py
python -u -m unittest discover -s tests -p test_dependency_bootstrap.py
git diff --check
```

## 6. 主线程集成、验收与文档

每项完成后按 [PR Stack 工作流](pr-stack-workflow.md) 检查真实 worktree 的分支、状态、
完整 diff、未跟踪文件和测试结果，原代理修正问题后复审。全部通过后执行：

```powershell
python -u -m unittest discover -s tests
git diff --check
```

Windows 多进程测试使用上述模块入口或有正确 main guard 的文件运行器，避免从标准输入
启动测试导致 spawn 无法重建主模块；新用例应增加基线测试数，不能靠删除或跳过原测试通过。

集成后的客户端验收包括：A/B 多版本主播放与切集、HTTP/路径型 STRM、CD2 成功与失败回退、
缓存预热失败后网络起播、GUI 自动恢复时重复启动、端口冲突、关闭后重启、字幕/章节和最终进度。
成品检查继续覆盖现有 Python 3.9 x86 包内运行时，不用开发机 Python 3.13 的结果代替。

主线程维护文档所有权：`docs/architecture.md`、`docs/lessons-learned.md`、本计划及总体路线图。
核实证据并搜索去重后，修正“取得锁前没有后台副作用”的过强描述，补充选中版本路径一致性和
预热结果调用方约束。当前属于既有约束的修复，预计无需新增 ADR；如果实现改变公共接口、
缓存格式或生命周期模型，暂停相关 Stack，先重新设计并评估 ADR。

S52 只增加播放器内部的启动门禁和对应测试，不改变公共接口、配置语义、缓存格式或生命周期模型，
因此无需新增 ADR。

发布门槛、后续功能顺序和待决策项见总体路线图。S52 已完成代码修复、自动化验收、成品打包和
beta 发布；S49–S51 仍按本文分别记录代码修复、自动化验收、成品验收和发布状态。
