# 跨项目可复用经验（ETLP）

本文是从本机未跟踪的 `文档/` 目录读取另一项目 Codex 文档后，结合当前 ETLP
代码和测试整理出的摘要。它只沉淀能帮助 ETLP 维护的规则，不改变当前 beta/stable
分支的功能范围、发布方式或配置语义。

原始资料主要包括另一项目的 `AGENTS.md`、`docs/architecture.md`、
`docs/lessons-learned.md`、`docs/technical.md`、`docs/pr-stack-workflow.md` 和
`docs/versioning.md`。`文档/LOCAL_OPERATIONS.md`、`文档/.codex/` 以及其中的
部署拓扑、代理配置和本机信息不迁移到 ETLP。

## 1. 知识沉淀规则

- 只记录能由当前代码、测试、实际运行/日志、可复现结果，或官方文档与当前实现交叉确认的事实。
- 写入前先搜索去重；发现旧经验失效时更新当前结论，并保留验证依据。
- 有实质性代码修改、Bug、架构或兼容性调查时，结束前做一次 Knowledge Review：记录新增约束、隐蔽坑、被证明错误的假设和建议沉淀项。
- 只有架构变化才需要进一步评估 `architecture.md` 或 ADR；不要为了流程强行增加历史记录。
- 代理报告和一次性命令输出只是线索，最终以真实 worktree、完整 diff 和独立验证为准。

## 2. 已与当前 ETLP 交叉确认的规则

### 2.1 先解析和校验请求，再创建播放线程

HTTP handler 必须先把 Emby/Plex 请求解析成完整播放数据，再把解析后的对象传给
`start_play`。不能把原始 payload 直接交给后台线程，否则缺失 `file_path` 等字段
时，错误会延迟到线程中才暴露，且请求响应可能已经返回成功。

当前证据：`utils/http_server.py` 的 `_dispatch_post`、
`tests/test_http_server_security.py::test_emby_and_plex_threads_use_parsed_payload`。

### 2.2 外部命令必须绑定当前播放身份

播放控制不能只依赖媒体名或当前位置。ETLP 的控制设备 ID 由浏览器设备和
`play_session_id` 派生，Emby session 查找还会检查控制设备/用户；WebSocket 命令
的 `PlaySessionId` 不匹配时必须拒绝执行，并且日志只记录安全原因，不回显外部会话标识。

当前证据：`utils/emby_session_api.py`、`utils/remote_control_client.py`、
`tests/test_remote_control_client.py::test_playstate_session_mismatch_logs_safe_reason_and_does_not_execute`。

### 2.3 自然播放进度不是手动 Seek

轮询位置应先按上一位置、播放状态和经过时间估算预期位置，再用阈值判断是否是
明显跳转。不能为了消除正常播放造成的小幅误差而周期性触发 Seek；暂停、速率变化、
真实跳转和远程命令回传要分别测试。

当前 ETLP 的 `RemoteControlClient` 使用 `seek_threshold` 与 expected position，
并由 `test_normal_progress_does_not_look_like_a_seek` 覆盖正常推进和明显跳变。

### 2.4 本地 HTTP 服务必须保持窄边界

- 默认监听回环地址；绑定非回环地址时，至少要求 32 个字符的 `http_server_token`。
- 本地 POST 路由使用精确 allowlist，并要求 `X-ETLP-Protocol: 1`；非回环稀疏文件动作使用 `Authorization: Bearer`。
- JSON 请求必须是 `application/json`，有明确的 `Content-Length`，请求体不超过当前 1 MiB 上限。
- 稀疏文件只允许普通文件名、正整数大小，并确认目标仍在 cache 目录内。
- 媒体转发只接受 `file_path`、`expires`、`sig` 组成的短期 HMAC URL 和允许的媒体后缀；日志不得记录完整查询串、token、Cookie 或敏感路径。
- 这套接口是本机播放辅助接口，不应被描述或扩展成公网媒体服务或通用远程 API。

当前证据：`utils/http_server.py`、`utils/http_security.py`、
`tests/test_http_server_security.py`。

### 2.5 可选能力和慢存储必须失败可降级

- `websocket-client` 是远程控制的可选依赖。系统导入失败时才尝试加载 `third_party` 中的 wheel，并先校验固定 SHA-256；wheel 缺失、损坏或导入失败时只禁用远程控制，不启动控制线程，也不影响普通播放。
- STRM 本地预热使用 daemon 线程和配置的超时；超时或读取失败只记录日志，不改变原有路径判断，也不阻塞播放启动。
- 修改这些路径时，至少覆盖“依赖缺失/哈希不匹配/导入失败”“预热超时/读取失败”“普通播放仍可启动”三类结果。

当前证据：`utils/remote_control_client.py`、`utils/tools.py`、
`tests/test_remote_control_client.py`、`tests/test_local_path_preheat.py`、
`tests/test_dependency_bootstrap.py`。

### 2.6 打包、更新和配置保护必须按成品验证

- beta/stable 打包先清理旧 staging，再生成对应频道的可运行 ZIP 和单独的 `.sha256`
  sidecar；发布包不得带入 `__pycache__`、`.pyc/.pyo` 或运行时生成的 wheel cache。
- 更新器先查询 GitHub Releases API，再严格解析只对应当前频道固定包名的 checksum，
  然后下载到 `.part` 文件；哈希不匹配或下载异常时删除临时文件、保留旧包，验证成功
  后才原子替换。不能用 GitHub 的单一 Latest 下载入口让 beta/stable 互相串包。
- ZIP 解压要先验证全部成员，拒绝 zip-slip、绝对路径、符号链接和越界目标；实时配置不能被更新包直接覆盖，示例配置应单独输出。
- 配置比较按 `ConfigParser` 的 section/key/value 语义进行，忽略注释、空行、键顺序和选项键大小写；section 名称仍按 `ConfigParser` 的匹配语义处理。如果要比较当前配置，应先选定可信的示例或上游基线，不能把“两个文件相同”误判为没有本地改动。
- beta/stable 更新包采用同一份最小运行清单：根目录只保留入口、配置、许可证、
  `requirements.txt` 和 Windows 启动脚本；`utils` 只保留 Python 模块，`user_script` 只
  保留 JavaScript，`third_party` 只保留 bundled wheels。`scripts/package_release.ps1`、
  两个频道 wrapper、`utils/update.py`、实际 ZIP 和测试必须保持同一份契约；用户脚本的
  update/download/homepage/support URL按频道规范化，不能和另一个频道混用。

当前证据：`scripts/package_release.ps1`、两个频道 wrapper、`utils/update.py`、
`utils/config_diff.py`、`tests/test_update.py`、`tests/test_package_beta.py`。

### 2.7 验证要区分层级

静态检查和单元测试只能证明对应代码路径；实际 beta/stable 成品还要检查 staging、ZIP
布局、sidecar 和干净环境启动。涉及播放、字幕、CloudDrive2、Emby 控制台或
mpv/IINA 外观/行为时，真实客户端验收仍是独立层级，不能用“测试通过”替代。

## 3. 只保留为参考、暂不并入 ETLP 的内容

- Watch Together 的房间、参与者、Barrier/Watching 状态机、房间 gate 和插件嵌入页主题变量不属于 ETLP 当前能力，不能移植为本项目承诺。
- 另一项目的四段程序集版本号、RSA manifest、trust root/bootstrap 和正式插件自动更新流程不适用于当前 ETLP。ETLP 当前证据是按频道生成的 ZIP + SHA-256 sidecar；若将来引入签名更新，应单独设计信任引导、密钥轮换和失败策略，私钥不得进入仓库或文档。
- 另一项目的服务器地址、部署/回滚步骤、签名密钥位置、代理 TOML 和本机路径只属于其 `LOCAL_OPERATIONS.md` 或 `.codex/`，不复制、不提交。
- Emby 服务端短暂 `PlaybackStopped`、旧 session 与当前播放并存等现象值得在 ETLP 的真实客户端链路中单独复现；在没有当前 ETLP 日志和回归测试前，不直接套用另一项目的 2 秒确认窗口或停止副作用规则。

## 4. 维护时的最小检查顺序

1. 读取适用的 `AGENTS.md`、README、当前模块文档和测试入口，先确认分支与用户已有改动。
2. 修改前写清楚输入、输出、身份边界、失败时的降级行为和不属于本任务的范围。
3. 先运行最相关的单元/静态检查，再对实际包或真实客户端做相应级别的验证。
4. 完成后核对 `git status --short`、完整 diff、未跟踪文件和残余风险；不要把另一项目目录或机器本地资料混入变更。

## 5. 证据索引

| 主题 | 当前 ETLP 证据 |
| --- | --- |
| HTTP 安全、路由和解析 | `utils/http_server.py`、`utils/http_security.py`、`tests/test_http_server_security.py` |
| Emby session 与远程控制 | `utils/emby_session_api.py`、`utils/remote_control_client.py`、`tests/test_remote_control_client.py` |
| 依赖加载和 STRM 预热 | `utils/dependency_bootstrap.py`、`utils/tools.py`、`tests/test_dependency_bootstrap.py`、`tests/test_local_path_preheat.py` |
| 打包、更新和配置比较 | `scripts/package_release.ps1`、`scripts/package_beta.ps1`、`scripts/package_stable.ps1`、`utils/update.py`、`utils/config_diff.py`、`tests/test_update.py` |
| 协作、隔离和审核 | `AGENTS.md`、`docs/pr-stack-workflow.md` |
