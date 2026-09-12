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
  GitCode Releases API，失败时回退 GitHub 并重新完整校验，再下载到 `.part`。
  哈希或下载失败时清理临时文件并保留旧
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

## 10. ETLP 启动和播放状态使用单实例约束

- 现象：多个 ETLP 进程或重复播放请求会让共享的 `prefetch_data`、播放器进程和
  `PlaySessionId` 回传相互交错，服务端可能短时间保留旧会话。
- 当前实现：`embyToLocalPlayer.py` 启动时先取得 `InstanceLock`；锁文件残留不作为
  活动状态判断。`start_play()` 和 HTTP 播放分派使用 playback lease，同一时刻只接纳
  一个活动播放器；第二个请求返回 HTTP 409 和 `playback_busy`。直接 `Popen` 播放器
  的 lease 保持到进程退出。`players.py` 中的 `--one-instance` 仍是 VLC 启动参数，
  不能作为 mpv 的实现依据。
- 约束：启动阶段不按进程名清理外部播放器，未知端口占用者不得被终止；播放 lease
  必须在正常退出、启动失败和异常路径释放。历史 `/playMediaFile` helper 仍是独立
  入口，未纳入这条主播放链路。
- 验证：`utils/instance_lock.py`、`utils/http_server.py`、`tests/test_instance_lock.py`、
  `tests/test_http_server_security.py` 和 `tests/test_process_cleanup.py`；覆盖跨进程
  锁竞争、重复播放、直接进程等待、异常释放和端口冲突不清理。

## 11. CD2 网关 nonce 的续连绑定要兼容本机播放器

- 现象：mpv 在重定向、Range 请求或退出阶段可能使用不同的 User-Agent 继续请求同一个
  `/cd2/` nonce；如果所有请求都要求完整的 IP+User-Agent 相同，后续请求会被网关返回
  404，即使首个请求已经成功解析并开始播放。
- 结论：nonce 首次声明后，非回环客户端仍绑定完整的 IP+规范化 User-Agent；同一回环
  IP（如 `127.0.0.1` 或 `::1`）允许 User-Agent 变化，但不同 IP 仍拒绝。缺失、过期和
  客户端绑定不匹配只记录固定的脱敏状态，不记录 nonce、User-Agent、URL、路径或 token。
- 验证：`utils/clouddrive2_gateway.py` 的 `lookup_or_claim` 和
  `tests/test_clouddrive2_gateway.py` 的回环、非回环及脱敏日志测试。

## 12. 连播章节必须随当前媒体条目传递

- 现象：播放列表请求没有显式索取 `Chapters`，旧代码又仅从浏览器缓存提取前五项
  中的片头标记，导致后端已有章节也无法传给下一集。按 ticks 的末尾零过滤还会误删
  100、200 秒等合法标记；只生成 Opening/Main 会丢失普通章节和片尾。
- 结论：在现有条目请求中索取完整 `Chapters`，按精确 ItemId 使用缓存兜底，不能
  用季集编号共享不同版本的章节。每集明确保存自己的章节列表（包括空列表），ticks
  转换为秒时保留小数。片头起止只依赖有效的 IntroStart/IntroEnd 配对。
- 播放器边界：mpv 使用实际加载路径匹配章节，在 `file-loaded` 后仅为空章节列表
  补入 Emby 数据，保留媒体已有章节；写入前再次核对路径，避免切集时标题滞后造成
  错配。CD2 地址是传输路径，不能作为章节内容的来源。
- 起播时保存真正传给 mpv 的路径。播放列表为当前集生成的新 CD2 nonce 不等于
  已在播放的地址；后来获取的同一 ItemId 章节还要登记到原起播路径，并立即尝试
  补入，兼容已经错过加载事件和 GUI 缓存路径的情况。
- 验证：`tests/test_chapters.py` 的正规化、片头和条目身份测试，以及
  `tests/test_players.py` 的章节切换、原生章节保留和路径变化测试。真实 Emby/CD2
  客户端效果仍须按本文件第 8 条单独验收。

## 13. 不落盘 HTTP 预取不能复用持久缓存任务

- 现象：下一集预取和继续观看预取只传 `save_path=NUL` 或 `/dev/null`，但
  `Downloader` 仍用缺失的 `cache_path` 拼接任务 JSON 路径，初始化会抛 TypeError。
  空设备目的路径不等于关闭任务状态、文件锁和稀疏文件操作。
- 结论：不落盘预取使用独立的只读 HTTP 请求，已知大小计算首尾 Range，未知大小
  用有限尝试的 HEAD 获取；不创建缓存、任务文件或锁。成功、短读、非法响应和异常
  都关闭已有响应，失败只跳过预取，不阻断播放。
- Range 约束：验证 206 的 Content-Range 与请求一致；服务器返回 200 时仅允许
  有限读取从零开始的前缀，非零起点必须跳过，不能把文件开头当成尾部预取成功。
- 验证：`tests/test_discard_prefetch.py` 覆盖两处调用、准确范围、HEAD、错误范围、
  短读、忽略 Range、关闭响应及禁止文件/任务操作。持久缓存 Downloader 的写入
  校验不属于这项修复，不能由只读预取测试推断其断点续传已可靠。

## 14. 新增配置分区也必须标记存在差异

- 现象：`check_ini_diff()` 把新分区加入差异配置后直接 continue，遗漏
  `have_diff` 标记，导致只有新增分区时不会输出差异文件。
- 结论：新分区和已有分区的键值变化都必须触发差异输出；相同输入不应新建差异文件。
  此处只生成更新提示，不修改用户配置，也不承担旧差异文件清理或配置迁移。
- 验证：`tests/test_update_config_diff.py` 覆盖仅新增分区、新分区与已有变化同时出现、
  已有键变化、不输出未改项和相同输入。

## 15. 持久缓存 Range 响应必须在写入前验证

- 现象：旧的 `Downloader.range_download()` 在确认 HTTP 状态和范围前就打开缓存，
  服务器忽略 Range 返回 `200` 时，会把完整响应写入非零偏移；错误随后才暴露，原有缓存
  已经被破坏。首段请求也可能在错误响应确认前删除旧缓存。
- 结论：持久下载使用半开区间 `[start, end)`，请求前先检查任务锁、只读状态和取消状态，
  服务器响应必须是 `206`，`Content-Range`、总大小、可用 `Content-Length` 和
  `Content-Encoding` 必须与请求一致，随后才允许创建/替换缓存。短读从下一个未写字节
  继续，`401/403/416` 或无效范围停止，网络错误和 `5xx` 只在有限次数内重试；所有响应
  都必须关闭。
- 验证：`tests/test_downloader_range.py` 的错误响应、首段保护、短读续传、最后字节、锁和
  小文件预热测试，以及本地 HTTP 服务的 `200`、错误范围、`403`、有效 `206` 和短读场景。

## 16. 用户脚本日志和动态页面文本要使用安全快照

- 现象：直接把播放对象传给 `console.log()` 会保留可变原对象，开发者工具稍后展开时可能
  暴露认证字段；URL 查询串、userinfo 和嵌套错误文本也可能携带凭据。通知和媒体路径
  使用 `innerHTML` 拼接动态标题、路径时，特殊文本会被当成节点解析。
- 结论：日志先生成有深度和循环限制的脱敏快照，统一处理嵌套对象、数组、Error、URL、
  URL 编码键和值及认证字符串，再传给控制台；播放请求仍使用原始对象。动态标题、字幕、
  文件名和 STRM 路径使用 `textContent` 或文本节点，静态 SVG 可以保留固定 HTML。
- 验证：`tests/user_script_security.test.cjs`、Python Node 桥接测试，以及本地 Edge DOM
  检查。测试确认凭据不出现在日志，恶意标题/路径不生成元素，原节点和事件保持不变。

## 17. ETLP 关闭使用回环控制接口

- 结论：Windows 菜单和直接命令行脚本统一向 `127.0.0.1:58000/shutdown/` 发送带
  `X-ETLP-Protocol: 1` 的 JSON POST。服务端先返回固定成功 JSON，再从独立 daemon 线程
  请求 `HTTPServer.shutdown()`；`run_server()` 的 `finally` 必须调用 `server_close()`，
  由主进程的既有退出路径释放实例锁。关闭入口不负责终止外部播放器。
- 验证：`utils/http_server.py`、`utils/stop_instance.py`、
  `tests/test_http_server_security.py` 和 `tests/test_stop_instance.py`；实例锁释放路径
  继续由 `tests/test_process_cleanup.py` 覆盖。

## 不应直接沉淀的内容

- 未能由当前 ETLP 代码、测试或运行结果确认的另一项目规则。
- 单次任务的命令流水、临时 commit/hash、未决风险和本机部署拓扑。
- 另一项目的 Watch Together 房间状态机、四段版本规则、签名 trust root 或插件
  自动更新语义；这些如果未来适用，必须先做独立设计和 ADR。
