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

- 结论：更新器按安装包频道选择对应的固定 ZIP 和单条 SHA-256 sidecar；先访问
  GitHub Releases API，再下载到 `.part`。哈希或下载失败时清理临时文件并保留旧
  archive，验证成功后才原子替换。ZIP 先全量检查路径、符号链接和目标边界；live
  配置不直接被更新包覆盖。
- 成品清单：根目录只保留入口、配置、许可证、`requirements.txt` 和 Windows 启动脚本；
  `utils` 只保留 Python 模块，`user_script` 只保留 JavaScript，`third_party` 只保留
  bundled wheels；Markdown、`.proto` 源文件、替代启动入口和缓存不进入 beta 或 stable
  ZIP。打包脚本、更新器和实际 ZIP 测试必须同步维护这份清单。
- 分支门禁：`package_beta.ps1` 只能从 `beta` 运行，`package_stable.ps1` 只能从
  `stable` 运行；`main` 只同步上游，不打包或发布。
- 验证：`scripts/package_release.ps1`、两个频道 wrapper、`utils/update.py`、
  `tests/test_update.py`、`tests/test_release_info.py` 和用户脚本频道测试；另有干净
  临时工作区的 beta/stable ZIP smoke test。
- 旧版兼容：旧 beta 更新器固定请求 `releases/latest/download/etlp-remote-control-beta.zip`，
  无法自行获得新的频道更新器。stable Latest Release 暂时保留 beta ZIP 和 `.sha256` 的
  兼容副本，用于一次性自举；兼容资产必须与当前 beta Release 的 SHA256 完全一致，旧客户端
  升级后即可转入按频道 API 的新路径。

## 7. 配置比较必须使用可信基线

- 结论：使用 `ConfigParser` 语义比较 section/key/value，区分 only-local、
  only-example 和 changed；本地配置和示例配置相同只能说明这两个输入相同，不能
  证明用户没有相对上游基线的个性化改动。
- 验证：`utils/config_diff.py`、`utils/update.py`。运行比较前应明确示例配置的来源，
  必要时从可信上游 revision 提取干净基线。

## 8. 自动化验证不等于真实客户端验收

- 结论：静态检查、单元测试、实际 beta/stable ZIP smoke test 和 Emby/mpv/IINA 客户端验收
  是不同证据层级。涉及播放、字幕、CloudDrive2、控制台操作或客户端显示时，不能
  用前一层结果替代后一层。
- 验证：当前测试覆盖各模块边界；README/FUNCTIONS.md 定义了实际播放器和服务端
  支持范围，发布前仍需按任务范围执行成品或客户端验证。

## 9. .strm 指针文件不能当媒体直发，CD2 回退必须读指针重定向

- 现象：strm 内部 URL 不带媒体后缀时，`strm_local_media_path` 推导出的本地路径
  仍是 `.strm`；此时 CD2 云路径（若存在）也是同一份指针文本而不是媒体。旧回退
  链在扩展名白名单处拒绝 `.strm`，CD2 解析临时失败就硬 404，表现为时好时坏。
- 结论：`/cd2/` 回退只接受两类目标：扩展名合法且存在的本地媒体文件，或 .strm
  指针文件内容中的首个 http(s) URL（307 重定向）。`.strm` 派生路径直接跳过 CD2
  解析；派生媒体文件缺失时尝试同名 `.strm` 兄弟文件，因为小指针文件在冷挂载下
  通常比大媒体文件更早可见。指针 URL 只接受绝对 http(s) 且拒绝内嵌账号密码，
  避免把凭据带进播放器请求日志。
- 验证：`utils/http_server.py` 的 `_send_cd2_strm_fallback` 及
  `tests/test_clouddrive2_gateway.py` 的 `HttpGatewayRouteTests`、`StrmContentParseTests`。

## 10. mpv 播放器必须保持单实例

- 现象：`one_instance_mode = no` 时，即使第二个 mpv 只短暂启动后立即关闭，也可能
  让同一个 `PlaySessionId` 的 `Playing/Progress/Stopped` 回传交错。服务端先收到停止，
  随后旧式 `embyToLocalPlayer` 回传又创建 `Playing`，Emby 控制台会短时间保留旧会话，
  直到后续播放请求触发清理。
- 结论：默认开启 `one_instance_mode = yes`，不把播放器多开视为受支持场景。
  `start_play()` 的运行中保护和 mpv 的 `--one-instance` 参数共同避免多个实例争用同一
  播放回传状态。若未来需要支持多开，必须先为每个实例隔离 `PlayerManager`、远程控制
  客户端和实时回传状态。
- 验证：`utils/http_server.py`、`utils/players.py` 的单实例逻辑，以及 2026-08-12
  一次双 mpv 实际运行中服务端出现的重复 `Playing`/`Stopped` 会话序列。

## 不应直接沉淀的内容

- 未能由当前 ETLP 代码、测试或运行结果确认的另一项目规则。
- 单次任务的命令流水、临时 commit/hash、未决风险和本机部署拓扑。
- 另一项目的 Watch Together 房间状态机、四段版本规则、签名 trust root 或插件
  自动更新语义；这些如果未来适用，必须先做独立设计和 ADR。
