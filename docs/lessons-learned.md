# 维护经验

本文只收录已由当前 ETLP 代码、测试或可复现运行结果确认，且对后续维护具有复用
价值的经验。写入前搜索去重；发现失效时更新当前事实并保留验证依据。一次性任务
日志、未复现推测、机器拓扑和敏感信息不写入这里。

## 1. 先解析请求，再创建播放线程

- 现象：把原始 Emby/Plex payload 直接交给后台线程时，`start_play()` 可能在异步
  路径中才因缺失 `file_path` 抛出 `KeyError`。
- 结论：HTTP handler 必须先完成协议校验和数据解析，再将解析后的对象传给播放线程。
- 验证：`tests/test_http_server_security.py::test_emby_and_plex_threads_use_parsed_payload`。

## 2. 控制命令必须绑定当前播放身份

- 现象：仅凭媒体名、位置或相同的播放器设备不足以证明命令属于当前播放。
- 结论：远程控制使用由浏览器设备和 `play_session_id` 派生的控制身份；session 查找
  校验控制设备、用户和播放 session，WebSocket 命令校验 `PlaySessionId`，状态回传
  再携带当前 Item/MediaSource。校验失败时拒绝执行并只记录安全诊断标签。
- 验证：`utils/emby_session_api.py`、`utils/remote_control_client.py`，以及
  `test_playstate_session_mismatch_logs_safe_reason_and_does_not_execute`。

## 3. 自然播放进度不是手动 Seek

- 现象：轮询位置会因播放时间自然前进，直接比较相邻位置会误报跳转。
- 结论：按上一位置、暂停状态和经过时间计算 expected position，再以阈值判断明显
  跳变；不要用周期性 Seek 消除小幅误差。
- 验证：`RemoteControlClient` 的 `seek_threshold` 逻辑和
  `test_normal_progress_does_not_look_like_a_seek`。

## 4. 本地 HTTP 接口保持窄边界

- 结论：默认回环监听；非回环需要至少 32 字符 token；POST 使用精确路由和
  `X-ETLP-Protocol: 1`；请求体有 1 MiB 上限；稀疏文件限制在 cache 目录内；媒体
  URL 使用短期 HMAC，并避免把查询串、token 或敏感路径写进普通日志。
- 验证：`utils/http_server.py`、`utils/http_security.py` 和
  `tests/test_http_server_security.py`。

## 5. 可选依赖和慢存储只能降级

- 结论：内置 WebSocket wheel 只有在系统导入不可用时才加载，并先校验 SHA-256；缺失、
  损坏或导入失败只关闭远程控制，不启动控制线程。STRM 预热在 daemon 线程中执行，
  超时或读取失败只记录日志，不改变路径决定和播放启动。
- 验证：`utils/remote_control_client.py`、`utils/tools.py`、
  `test_dependency_bootstrap.py`、`test_local_path_preheat.py` 和远程控制相关测试。

## 6. 更新必须先验证，成功后再替换

- 结论：beta 更新器只接受固定资产名的单条 SHA-256 sidecar；下载写入 `.part`，
  哈希或下载失败时清理临时文件并保留旧 archive，验证成功后才原子替换。ZIP 先
  全量检查路径、符号链接和目标边界；live 配置不直接被更新包覆盖。
- 成品清单：根目录只保留入口、配置、许可证、`requirements.txt` 和 Windows 启动脚本；
  `utils` 只保留 Python 模块，`user_script` 只保留 JavaScript，`third_party` 只保留
  bundled wheels；Markdown、`.proto` 源文件、替代启动入口和缓存不进入 beta ZIP。
  打包脚本、更新器和实际 ZIP 测试必须同步维护这份清单。
- 验证：`scripts/package_beta.ps1`、`utils/update.py`、`tests/test_update.py` 和
  `tests/test_package_beta.py`。

## 7. 配置比较必须使用可信基线

- 结论：使用 `ConfigParser` 语义比较 section/key/value，区分 only-local、
  only-example 和 changed；本地配置和示例配置相同只能说明这两个输入相同，不能
  证明用户没有相对上游基线的个性化改动。
- 验证：`utils/config_diff.py`、`utils/update.py`。运行比较前应明确示例配置的来源，
  必要时从可信上游 revision 提取干净基线。

## 8. 自动化验证不等于真实客户端验收

- 结论：静态检查、单元测试、实际 beta ZIP smoke test 和 Emby/mpv/IINA 客户端验收
  是不同证据层级。涉及播放、字幕、CloudDrive2、控制台操作或客户端显示时，不能
  用前一层结果替代后一层。
- 验证：当前测试覆盖各模块边界；README/FUNCTIONS.md 定义了实际播放器和服务端
  支持范围，发布前仍需按任务范围执行成品或客户端验证。

## 9. CloudDrive2 直链必须在本地 gateway 内消费

- 结论：`get_direct_url` 默认关闭；启用后只把 CloudDrive2 的 `directUrl` 留在进程内，
  由本地 gateway 转发 `Range` 和必要请求头，不通过 `Location` 暴露。直链代理失败先
  回退 `downloadUrlPath`，再回退原挂载文件；旧 protobuf 不支持请求字段时保持普通 URL
  模式。直链能力是否被具体云盘实现，仍需真实 CD2 响应和播放器验收确认。
- 验证：`utils/clouddrive2_client.py`、`utils/clouddrive2_gateway.py`、
  `utils/http_server.py`、`tests/test_clouddrive2_client.py` 和
  `tests/test_clouddrive2_gateway.py`。

## 不应直接沉淀的内容

- 未能由当前 ETLP 代码、测试或运行结果确认的另一项目规则。
- 单次任务的命令流水、临时 commit/hash、未决风险和本机部署拓扑。
- 另一项目的 Watch Together 房间状态机、四段版本规则、签名 trust root 或插件
  自动更新语义；这些如果未来适用，必须先做独立设计和 ADR。
