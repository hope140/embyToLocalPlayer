# 2026-10-31 播放维护与上游适配

本次在 `beta@54b2aba` 基础上修复三项已知问题，再适配上游多版本兼容及独立小补丁。
改动已形成串行本地提交；已发布 beta/stable 程序包沿用原版本。

## 后续状态（2026-11-12）

维护者确认此前已完成 ETLP 使用验收，只是尚未发布；下文“尚未进行”是 2026-10-31 的历史记录，不再作为当前整体未验收结论。本次未增加产品改动，已对 `a3231f3` 新构建测试包补充双运行时回归与隔离启动检查。具体包身份、证据边界及发布状态见 [发布记录](2026-11-12-beta-release.md)。

## Stack 与审核

| Stack | 基础 | 目标 / 提交 |
| --- | --- | --- |
| S49 | `54b2aba` | 选中 STRM 版本与本地路径一致；`1fc8e95` |
| S50 | S49 `1fc8e95` | 消费首尾预热结果；`9ad0ac7` |
| S51 | S50 `9ad0ac7` | 持锁后初始化业务；`3119fd5` |
| S53 | S51 `3119fd5` | Emby 替代版本补全；`90ccb58` |
| S54 | S53 `90ccb58` | 请求标识与 Windows PATH 补丁；`e024c5e` |

各 Stack 使用独立分支、worktree 和 `luna_worker`，实现与验证串行进行。
主线程检查真实工作区、完整 diff、未跟踪文件与验收证据，独立复跑相关测试后整理本地提交。
文件所有权按下表独占，表外文件禁止由对应执行代理修改；知识文档统一由主线程维护。
未引入依赖、配置格式或播放器适配代码改动。

| Stack | 允许修改 / 文件所有权 | 实现边界与验收 |
| --- | --- | --- |
| S49 | `utils/data_parser.py`、`tests/test_clouddrive2_gateway.py`、`tests/test_strm_media_path.py` | HTTP/路径型 STRM、手动/自动版本选择、主播放/列表；缺可靠 sidecar 时回退选中源流，保留后缀规则 |
| S50 | `utils/downloader.py`、`tests/test_downloader_range.py` | 首尾都成功才派发缓存；失败有限重试且不马上重启整段下载，保留原媒体地址、只读/暂停/取消与旧缓存 |
| S51 | 入口、进程清理/锁/关闭测试 | 锁外仅标准库和锁模块；锁失败禁止业务导入，导入/端口失败释放锁，正常关闭后重启 |
| S53 | 解析器、Emby API、多版本/章节/CD2 测试 | 显式条目优先去重、精确源与章节身份、季列表和显式列表边界、缺字段与不可靠 sidecar 回退；Jellyfin 保持原路径 |
| S54 | 下载器、Emby API/Thin、Windows 启动器、下载与多版本测试 | 保留 Range 响应校验、重试、原有启动器引号；系统 PATH 在菜单循环外补充 |

S49–S51 的详细原始契约及修复前复现见 [审查修复计划](2026-09-12-review-fix-plan.md)。
各项可按独立提交回滚；已经集成的提交在获授权后按 S54→S53→S51→S50→S49 顺序 revert。

## 上游来源与适配边界

- [b4f9527](https://github.com/kjtsune/embyToLocalPlayer/commit/b4f9527519774ea6e9d91ebbe04ca892f41cb98b)：请求替代媒体源并补齐缺少独立条目的版本；保留本仓库章节与 STRM 安全约束。
- [6406161](https://github.com/kjtsune/embyToLocalPlayer/commit/640616141b165209811b283b01285c84b74e24eb)：独立日志限量与请求标识片段。
- 错误条目日志限制为前 10 项并附总数，已随 S53 的数据检查适配完成；S54 不再修改解析器。
- 按名称构造的 STRM 路径只作为版本提示，不作为已验证的本地 sidecar；无法区分时保留精确源单项播放。
- 替代 ItemId 不继承默认版本章节；显式播放列表不自动增加另一版本。
- 上游 mpv 片头缓存涉及倍速记忆、Plex/IINA 和章节优先级，继续待后续设计。

## 验证与限制

最终在本地 `beta` 执行 `python -u -m unittest discover -s tests`，332 项通过；
包含 Node 用户脚本桥接、真实入口子进程及发布/打包 dry-run 检查。`git diff --check` 通过。
分项命令均使用 `python -u -m unittest discover -s tests -p <模块名>`：

- S49：`test_strm_media_path.py`、`test_clouddrive2_gateway.py`、`test_chapters.py`。
- S50：`test_downloader_range.py`、`test_discard_prefetch.py`、`test_http_server_security.py`。
- S51：`test_process_cleanup.py`、`test_instance_lock.py`、`test_stop_instance.py`、`test_dependency_bootstrap.py`。
- S53/S54：`test_emby_multi_version.py`、章节/CD2/STRM/播放器、下载及启动器相关模块。

真实 Emby/mpv/IINA/CD2 播放及实际 Python 3.9 x86 成品验收尚未进行；打包测试使用运行时夹具。
稳定分支仍保留此前发布代码。其下载器和下载测试的修复前 blob 与 S50 基础相同，具备独立回移条件；回移及稳定版验收仍需单独执行。

## Knowledge Review

已更新现有架构、单实例、章节、缓存和 STRM 经验条目，补充多版本列表身份约束。
本次维护恢复既有边界，未改变公开协议、持久化格式或长期架构取舍，无需新增 ADR。
