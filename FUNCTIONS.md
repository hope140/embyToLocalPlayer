# embyToLocalPlayer 功能详情

本文面向产品/策划和后续维护人员，用来回答三个问题：

1. 当前项目已经具备哪些功能；
2. 每项功能支持哪些服务器、播放器和使用方式；
3. 后续想修改某项功能时，应先看哪些配置和模块。

本文以当前 `watch_together` 测试分支的代码、`README.md` 和 `embyToLocalPlayer_config.ini` 为准。该分支是同步观看测试通道；它不是安装教程，也不替代 README 中的 FAQ。

## 1. 状态说明

| 标记 | 含义 |
| --- | --- |
| **[稳定]** | 默认能力，或项目明确支持的常用功能。仍需以支持矩阵中的平台、播放器限制为准。 |
| **[条件支持]** | 功能已经实现，但依赖指定服务器、播放器、操作系统、播放模式或额外配置。 |
| **[实验/无支持]** | README 将其归为隐藏功能、配置较复杂、兼容性有限，或明确不提供问题支持。 |

文中的“稳定”表示项目当前明确支持，不表示所有媒体、系统和播放器组合都能得到完全一致的结果。

配置项还分为两类：

- **常规配置**：已经出现在 `embyToLocalPlayer_config.ini` 中，可按注释修改。
- **隐藏配置**：代码能够读取，但默认配置模板没有完整展示，通常只在 README 的隐藏功能中出现。修改前应先备份配置。

## 2. 项目整体流程

```mermaid
flowchart LR
    A["Emby / Jellyfin / Plex 网页"] --> B["油猴脚本拦截播放请求"]
    B --> C["本地 HTTP 服务 :58000"]
    C --> D["解析媒体、版本、字幕和播放列表"]
    D --> E{"播放方式"}
    E -->|网络模式| F["服务器视频流 / STRM 直连"]
    E -->|读盘模式| G["路径转换后的本地或挂载文件"]
    F --> H["本地播放器"]
    G --> H
    H --> I["播放进度与暂停状态"]
    I --> J["回传 Emby / Jellyfin / Plex"]
    I --> K["可选同步 Bangumi / Trakt"]
```

主要组成部分：

| 组成 | 作用 | 主要位置 |
| --- | --- | --- |
| 浏览器端油猴脚本 | 拦截网页播放、读取媒体信息、提供模式菜单和页面增强 | `user_script/embyToLocalPlayer.user.js` |
| Python 入口 | 读取配置、清理环境、启动后台任务和本地服务 | `embyToLocalPlayer.py` |
| 本地 HTTP 服务 | 接收油猴请求、分派播放/下载/打开文件夹等操作 | `utils/http_server.py` |
| 数据解析 | 解析 Emby/Jellyfin/Plex 数据，确定视频、字幕、版本和播放列表 | `utils/data_parser.py` |
| 播放器管理 | 启动播放器、维护播放列表、获取播放位置 | `utils/player_manager.py`、`utils/players.py` |
| 服务端通信 | 回传进度、处理重定向、字幕缓存、第三方同步 | `utils/net_tools.py` |
| 配置与通用能力 | 读取配置、路径转换、播放器选择、日志 | `utils/configs.py`、`utils/tools.py` |

## 3. 支持范围总览

### 3.1 媒体服务器

| 能力 | Emby | Jellyfin | Plex | 说明 |
| --- | --- | --- | --- | --- |
| 网页调用本地播放器 | 支持 | 支持 | 支持 | Plex 通过 `app.plex.tv` 页面适配。 |
| 首页/详情页播放拦截 | 支持 | 支持 | 条件支持 | 页面结构或服务端版本变化可能影响油猴脚本。 |
| HTTP 网络播放 | 支持 | 支持 | 支持 | 实际可用性还受域名解析、代理和播放器影响。 |
| 读盘模式/路径转换 | 支持 | 支持 | 条件支持 | 客户端必须能访问对应本地文件或挂载路径。 |
| 剧集连续播放 | 支持 | 支持 | 支持 | 受播放器、外挂字幕、多版本匹配等条件限制。 |
| 退出时回传最终进度 | 支持 | 支持 | 支持 | 可通过 `[emby] update_progress` 关闭；ISO、M3U8 不回传。 |
| 实时回传进度和暂停状态 | 支持 | 支持 | 未适配 | 当前实时接口面向 Emby/Jellyfin，播放器还需支持 mpv IPC。 |
| Bangumi 单向同步 | 支持 | 支持 | 不支持 | 仅同步符合风格筛选的常规动画剧集。 |
| Trakt 单向同步 | 支持 | 支持 | 支持 | 依赖外部条目 ID 和 Trakt OAuth 配置。 |

### 3.2 本地播放器

| 播放器 | 基础播放 | 连续播放 | 外挂字幕 | 进度回传 | 读盘模式 | 主要限制 |
| --- | --- | --- | --- | --- | --- | --- |
| mpv | 支持 | 支持 | 支持 | 实时 + 最终 | 支持 | 推荐组合；实时反馈、预读取、片头跳过等高级功能主要围绕 mpv 实现。 |
| mpv.net | 支持 | 支持 | 支持 | 条件支持 | 支持 | 作为 mpv 系播放器处理；实时反馈取决于 IPC 兼容和实际启动方式。 |
| PotPlayer | 支持 | 支持 | 条件支持 | 退出时 | 支持 | HTTP 播放可能出现地址关闭或渲染 Pin 问题；含 HTTP 外挂字幕时下一集受限。 |
| MPC-HC | 支持 | 支持 | 条件支持 | 退出时 | 支持 | HTTP 播放加载/拖动较慢；需开启 WebUI；HTTP 外挂字幕依赖内部字幕渲染器。 |
| MPC-BE | 支持 | 条件支持 | 条件支持 | 退出时 | 支持 | 播放列表条目较多时可能卡住，其他限制与 MPC-HC 类似。 |
| VLC | 支持 | 支持 | 条件支持 | 退出时 | 支持 | Linux/macOS 下含 HTTP 外挂字幕时，下一集可能无法加载字幕。 |
| IINA | 支持 | 条件支持 | 支持 | 实时 + 最终 | 支持 | 连续播放仅明确支持读盘模式；需要设置为播放结束后完全退出。 |
| 其他播放器 | 条件支持 | 未保证 | 由播放器决定 | 通常不支持 | 条件支持 | 项目只负责拼装命令并启动，通常无法获取播放位置。 |
| 弹弹play | 实验支持 | 读盘模式支持 | 条件支持 | 条件支持 | 支持 | HTTP 模式需等待、无法加载外挂字幕，配置和同步规则较特殊。 |

“外挂字幕支持”指项目能够把 Emby/Jellyfin/Plex 提供的外挂字幕交给播放器。播放器自身对格式、编码和网络字幕的支持仍可能不同。

## 4. 核心功能详情

### 4.1 网页播放拦截与本地调用

**状态：** [稳定]

- **作用**：点击网页原有播放按钮时，阻止浏览器内播放，将媒体信息发送到本机 `58000` 端口，再启动本地播放器。
- **入口**：Emby/Jellyfin/Plex 的首页卡片、详情页播放按钮，以及受支持的全部播放、随机播放或播放列表入口。
- **浏览器菜单**：可按当前服务器启用/禁用脚本；禁用后恢复网页播放器。
- **页面反馈**：调用成功时显示短暂的“正在播放”通知，并尝试关闭网页端兼容流或播放失败提示窗。
- **限制**：网页前端升级、主题改版或第三方美化脚本可能改变 DOM/API，导致拦截失效。直播频道可通过脚本内隐藏开关 `disableForLiveTv` 保留网页播放。
- **相关模块**：`user_script/embyToLocalPlayer.user.js`、`utils/http_server.py`。

### 4.2 网络模式与读盘模式

**状态：** [稳定]

| 模式 | 播放地址 | 适用情况 | 关键条件 |
| --- | --- | --- | --- |
| 网络模式 | 媒体服务器提供的 HTTP 地址 | 客户端没有挂载媒体目录，或希望直接走服务器网络 | 播放器能够访问服务器视频流。 |
| 读盘模式 | 转换后的本地/网络挂载路径 | NAS 目录已挂载到客户端，希望减少中转 | `[src]` 与 `[dst]` 映射正确，文件对客户端可见。 |

- **入口**：油猴菜单“读取硬盘模式”。
- **关键配置**：`[src]`、`[dst]`、`[dev] force_disk_mode_path`、`path_check`。
- **规则**：服务端路径按配置顺序匹配 `[src]` 前缀，找到后替换成同名键对应的 `[dst]` 前缀；首次匹配成功后停止。
- **强制模式**：`force_disk_mode_path` 可让特定服务端路径无视油猴开关，自动使用读盘模式。
- **兼容检查**：`path_check = yes` 会检查文件存在性，并尝试处理 NFC/NFD 字符差异；兼容性更高，但起播会变慢。
- **相关模块**：`utils/tools.py`、`utils/data_parser.py`、`embyToLocalPlayer_config.ini`。

### 4.3 播放器选择与启动参数

**状态：** [稳定]

- **作用**：根据默认播放器、服务端文件路径和媒体类型生成启动命令，传递开始时间、字幕、标题、全屏等信息。
- **关键配置**：`[exe]` 播放器路径；`[emby] player`、`fullscreen`；`[dev] player_by_path`、`pretty_title`、`player_proxy`、`one_instance_mode`。
- **按路径选择**：`player_by_path` 可让 ISO、BDMV、动漫目录或其他路径关键词使用不同播放器。
- **标题美化**：mpv 和 PotPlayer 可显示来自媒体服务器的剧名、季集信息等；播放网页列表时，为保持列表原始结构可能停用标题美化。
- **单实例限制**：开启 `one_instance_mode` 后，上一个受控播放器未结束时会拒绝新播放请求，以避免进度串台。
- **未适配播放器**：仍可通过 `[exe]` 添加启动命令，但项目不会保证播放列表和进度回传。
- **相关模块**：`utils/tools.py`、`utils/players.py`、`utils/http_server.py`。

### 4.4 STRM 播放

**状态：** [条件支持]

项目支持三种 STRM 使用方式：

| 方式 | 说明 | 关键配置 |
| --- | --- | --- |
| 媒体服务器中转 | 使用 Emby/Jellyfin 生成的视频流地址 | 默认行为。 |
| STRM 内容直连 | 直接播放 `.strm` 文件内部的网址或路径，绕过媒体服务器中转 | `[dev] strm_direct_host`。 |
| 本地同名媒体定位 | 读盘模式下，以 `.strm` 自身路径为基准，定位同名真实媒体文件 | `strm_local_by_file_path` 及相关配置。 |

- **本地定位规则**：优先从 STRM 内部 HTTP URL 的 path 或查询参数中识别媒体后缀；识别不到时使用 `strm_local_fallback_ext`，留空则保留 `.strm` 后缀。
- **冷挂载重试**：`strm_local_path_retry_seconds` 控制 CD2 等挂载盘首次未就绪时的最长等待时间。
- **本地视频 + 网络字幕**：使用本地同名媒体时，可继续下载 Emby 外挂字幕到 `.tmp/subtitles` 后交给播放器。
- **字幕缓存**：`strm_local_subtitle_cache`、`strm_local_subtitle_cache_days`、`strm_local_subtitle_cache_max_mb` 控制开关、保留天数和容量。
- **缺失时长**：STRM 没有媒体时长时，项目会尝试在播放中重新读取媒体信息；仍失败时可临时记录播放位置。
- **限制**：多版本 STRM 的文件名、媒体源和字幕对应关系更复杂；网络 STRM 暂不支持实时播放反馈。
- **相关模块**：`utils/data_parser.py`、`utils/player_manager.py`、`utils/net_tools.py`。

### 4.5 播放进度回传

**状态：** [稳定]

- **作用**：把本地播放器的播放位置回传到 Emby、Jellyfin 或 Plex，使“继续观看”和已观看状态保持一致。
- **最终回传**：受支持播放器退出后读取最终时间，三类媒体服务器均支持。
- **实时回传**：mpv/IINA 播放时周期性发送进度，并在暂停、恢复时立即同步；当前使用 Emby/Jellyfin 播放会话接口。
- **关键配置**：`[emby] update_progress`；`[dev] playing_feedback_enable`、`playing_feedback_interval`、`playing_feedback_host`。
- **默认间隔**：30 秒，代码最低限制为 10 秒。
- **播放完成判断**：播放列表和第三方同步通常以播放超过约 90% 为完成依据；媒体服务器本身仍可能使用自己的已观看规则。
- **跳过情况**：ISO、M3U8 不回传；播放器无法取得停止时间时不回传；缺失时长的 STRM 可能需要播放列表功能辅助补全。
- **相关模块**：`utils/player_manager.py`、`utils/players.py`、`utils/net_tools.py`。

### 4.6 播放列表与连续播放

**状态：** [条件支持]

- **作用**：把当前季、网页播放列表或媒体库批量播放结果加入本地播放器，并逐集回传进度。
- **关键配置**：`[playlist] enable_host`、`version_filter`、`item_limit`、`http_sub_auto_next_ep`；`[dev] version_prefer_for_playlist`。
- **同版本续播**：当前视频有多个版本时，下一集优先匹配相同版本特征；失败时可按版本偏好补齐。
- **网页列表**：Emby/Jellyfin 的“全部播放、随机播放、播放列表”只明确支持电影和音乐视频类型；当前脚本也能缓存部分剧集列表数据，但页面版本差异可能影响结果。
- **播放器限制**：PotPlayer/VLC 的 HTTP 外挂字幕场景可能无法给下一集添加字幕；可用 `http_sub_auto_next_ep` 在当前集超过 90% 后关闭并重新启动下一集。
- **S0 混播**：PotPlayer 读盘模式可用隐藏配置 `mix_s0`，让播放顺序以 Emby 列表为准并补入第零季内容。
- **建议**：README 建议保持播放列表开启，因为多集回传、STRM 时长补全、同版本续播等能力依赖它。
- **相关模块**：`utils/data_parser.py`、`utils/player_manager.py`、`utils/players.py`。

### 4.7 多版本选择

**状态：** [稳定]

- **作用**：在首页等没有手动选版本的入口，根据文件名关键词自动选择偏好版本，并尽量让连续播放保持一致版本。
- **关键配置**：`[dev] version_prefer`、`version_prefer_for_playlist`；`[playlist] version_filter`。
- **优先级**：`version_prefer` 从前到后匹配，前面的关键词优先。
- **连续播放匹配**：`version_filter` 是不区分大小写的正则差异字段，用于从相邻集文件名中识别清晰度、编码、字幕组等版本特征。
- **失败处理**：匹配不到可靠版本时可能禁用播放列表，避免连续播放错误文件。
- **相关模块**：`utils/tools.py`、`utils/data_parser.py`。

### 4.8 字幕处理

**状态：** [条件支持]

- **网页选择**：网页选择的外挂字幕有效；内置字幕通常交给播放器按自身语言规则选择。
- **自动选择**：未选择字幕时，根据 `[dev] subtitle_priority` 从前到后匹配外挂字幕名称。
- **读盘模式**：普通本地文件优先使用本地外挂字幕路径；本地 STRM 同名媒体可使用下载缓存后的 Emby 字幕。
- **其他版本字幕**：[实验/无支持] `sub_extract_priority` 可在当前版本缺少合适字幕时，从其他媒体版本中选择字幕资源。
- **播放器差异**：PotPlayer、VLC、MPC 在 HTTP 外挂字幕和播放列表场景存在额外限制，详见播放器矩阵。
- **相关模块**：`utils/data_parser.py`、`utils/players.py`、`utils/net_tools.py`。

### 4.9 网络、代理与重定向

**状态：** [条件支持]

- **系统代理**：`use_system_proxy = yes` 时脚本使用系统代理，并覆盖 `script_proxy`。
- **脚本代理**：`script_proxy` 用于 Python 请求；`player_proxy` 仅面向 mpv、mpv.net、IINA。
- **重定向预检查**：`redirect_check_host` 可提前解析并缓存视频流重定向地址，减少播放器重复请求。
- **缓存过期**：`redirect_expire_minute` 可按域名设置重定向链接有效时间。
- **证书**：`skip_certificate_verify` 可跳过脚本侧的 HTTPS 证书验证，但播放器仍可能独立校验证书。
- **本地替换**：[实验/无支持] `stream_redirect` 可成对替换播放地址，常用于本机 alist 或只反代视频流的 nginx。
- **相关模块**：`utils/net_tools.py`、`utils/data_parser.py`、`utils/configs.py`。

### 4.10 打开本地目录和文件路径显示

**状态：** [条件支持]

- **作用**：在媒体详情页文件路径附近增加打开文件夹入口，将服务端路径转换后交给本机文件管理器。
- **关键条件**：客户端必须有对应挂载路径；路径转换规则需正确。
- **浏览器隐藏开关**：`disableOpenFolder` 可禁用按钮；`crackFullPath` 可让非管理员页面尝试显示更完整的文件路径。
- **风险**：完整路径可能暴露服务器目录结构，不应在不可信的共享浏览器环境中启用。
- **相关模块**：`user_script/embyToLocalPlayer.user.js`、`utils/tools.py`。

### 4.11 “继续观看”页面增强

**状态：** [条件支持]

- **近期条目重排**：当前分支默认启用 `enableResumeReorder`。保留“继续观看”前两项原顺序，把其余项目中最近 3 天新增的内容前移。
- **隐藏指定剧集**：隐藏开关 `resumeHideSomeSeries` 启用后，油猴菜单可把当前电视剧加入隐藏列表，也可重置全部隐藏设置。
- **存储位置**：排序与隐藏数据使用浏览器本地存储，只影响当前浏览器中的列表展示，不修改服务器媒体库。
- **限制**：依赖 Emby/Jellyfin “继续观看”接口返回字段和页面请求格式；默认配置文件不管理这些开关。
- **相关模块**：`user_script/embyToLocalPlayer.user.js`。

### 4.12 同步观看房间

**状态：** [实验]

- **作用**：管理员在自己的本机 etlp 通过 Emby 油猴菜单维护持久房间；房间只绑定同一 Emby 服务器上的恰好两名用户，参与者客户端用 mpv/IINA 控制播放会话。
- **入口与流程**：管理员在 Emby 左侧原生导航点击“同步观看”，进入主内容页面后进行 loopback 认证并查看房间、创建/删除房间或执行暂停、继续、重新同步；侧栏入口尚未出现时可使用油猴菜单“同步观看房间”回退。普通参与者只需在本机开启控制会话，不使用网页播放器同步，也不建立客户端互连端口。
- **关键配置**：`[watch_together] enable`（所有参与者机器）以及管理员本机的 `admin_enable`、`server_url`、`admin_api_key`。修改后台线程配置后重启 etlp；房间元数据持久化到 `watch_together_rooms.json`，会一直保留到通过 UI 删除房间或手动处理文件，损坏文件不会自动覆盖。
- **范围与约束**：仅 Emby、同一服务器、两名用户、mpv/IINA；两端 `ItemId` 必须相同，媒体时长差不超过 3 秒，速度固定 1.0；允许不同 `MediaSourceId`。两人均可暂停/继续/seek，主用户决定初始位置和冲突优先；断开、停止或换片会暂停另一方等待，不自动起播或自动下一集。
- **安全**：管理员 key 只应留在管理员本机 INI，不能放入网页、油猴或日志；浏览器 token 只经 loopback 短期认证且不落盘；房间 JSON 不含 token。即使本地 HTTP 配置为 LAN，watch-together endpoints 仍只接受 loopback。
- **非目标**：不使用/宣传 Emby PartyService，不提供多人、邀请链接、聊天、网页播放控制或速度/字幕/音轨/音量设置；不声称已完成真实 Emby 实机测试或具备生产稳定性。
- **相关模块**：`user_script/embyToLocalPlayer.user.js`、`utils/watch_together_coordinator.py`、`utils/watch_together_client.py`、`utils/watch_together_store.py`、`utils/http_server.py`。

### 4.13 日志、进程和更新辅助

**状态：** [稳定]

- **启动清理**：默认启动时结束旧的 etlp 和已知播放器进程，避免端口占用或进度串台；由 `kill_process_at_start` 控制。
- **临时目录清理**：启动时清理项目临时缓存，并按字幕缓存策略保留必要文件。
- **日志**：`log_file` 控制日志路径；超过约 10 MB 时重置。`mix_log` 默认模糊域名和密钥等信息。
- **配置刷新**：本地服务收到请求时会重新读取配置，部分普通配置无需重启；后台线程和监听地址相关配置仍需重启。
- **更新工具**：`utils/update.py` 固定从 `watch_together` 测试分支更新本体，并生成新旧配置差异文件；不会覆盖已有 `embyToLocalPlayer_config*` 配置文件。
- **相关模块**：`embyToLocalPlayer.py`、`utils/configs.py`、`utils/tools.py`、`utils/update.py`。

## 5. 第三方观看记录同步

### 5.1 Bangumi / bgm.tv

**状态：** [条件支持]

- **方向**：仅从 Emby/Jellyfin 向 Bangumi 标记已观看，不会从 Bangumi 回写媒体服务器。
- **触发**：播放器正常关闭，且该集的播放进度达到完成阈值。
- **范围**：只处理符合 `[bangumi] genres` 的常规动画剧集；不支持 Plex。
- **关键配置**：`enable_host`、`username`、`access_token`、`private`、`genres`。
- **主要限制**：多季、长篇、OVA/剧场版/WEB 续作关系和日期匹配可能失败；令牌有有效期。
- **额外命令**：可通过命令行把 Bangumi“在看”列表中已完成条目标为已观看。
- **相关模块**：`utils/bangumi_sync.py`、`utils/bangumi_api.py`、`utils/net_tools.py`。

### 5.2 Trakt

**状态：** [条件支持]

- **方向**：仅从媒体服务器向 Trakt 写入观看历史。
- **触发**：播放器正常关闭，且达到完成阈值。
- **范围**：Emby、Jellyfin、Plex 均可配置。
- **关键配置**：`[trakt] enable_host`、`user_name`、`client_id`、`client_secret`。
- **授权**：本地服务通过 `/trakt_auth` 接收 OAuth 回调，并保存令牌文件。
- **主要限制**：电影通常需要 IMDb ID，剧集需要单集 IMDb 或 TheTVDB 等可匹配 ID；网页手动标记已播放不会触发同步。
- **相关模块**：`utils/trakt_sync.py`、`utils/trakt_api.py`、`utils/http_server.py`。

## 6. 仓库内附属工具

这些目录和脚本与主程序同仓库维护，但不是主播放流程的必需部分。

### 6.1 `embyBangumi`

**状态：** [条件支持，高风险写入]

- 使用 Emby 从 TMDB 刮削出的原产地名称和上映时间搜索 Bangumi。
- 把 Bangumi 首季评分写入 Emby 的“影评人评分/烂番茄评分”字段。
- 电影搜索失败时会扩大日期范围重试；搜索结果和失败结果按不同期限缓存。
- `dry_run = yes` 可先预览效果。
- **风险**：会修改 Emby 元数据，项目没有还原功能。正式运行前必须备份，确认结果后再关闭 dry-run。
- 主要位置：`embyBangumi/embyBangumi.py`、`embyBangumi/embyBangumi_config.ini`。

### 6.2 `embyDouban`

**状态：** [条件支持]

- 在 Emby 详情页展示豆瓣、Bangumi 评分、链接和标签。
- 豆瓣短评可通过油猴菜单开关。
- 使用浏览器缓存减少对外部接口的重复请求。
- 匹配依赖标题、IMDb ID、Bangumi 数据和第三方接口，结果可能缺失或不准确。
- 主要位置：`embyDouban/embyDouban.user.js`。

### 6.3 `embyEverywhere`

**状态：** [条件支持]

- 在 bgm.tv、豆瓣、IMDb、TMDB、TVDB、Trakt、Google 搜索结果等页面增加 Emby 搜索/跳转入口。
- 根据站点可取得的 IMDb、TMDB、TVDB ID，或标题与年份查询 Emby。
- 页面缺少可匹配 ID/标题时不会搜索；第三方网站改版可能导致入口失效。
- 主要位置：`embyEverywhere/embyEverywhere.user.js`。

### 6.4 `linkDoubanTrakt`

**状态：** [条件支持]

- 在豆瓣电影页面增加 Trakt 跳转，在 Trakt 电影/剧集页面增加豆瓣跳转。
- 主要依赖 IMDb ID，并通过豆瓣 API、Wikidata 或搜索结果补全映射。
- 使用浏览器缓存降低重复查询；第三方接口或页面结构变化可能影响结果。
- 主要位置：`user_script/linkDoubanTrakt.user.js`。

### 6.5 qBittorrent WebUI 打开/播放

**状态：** [条件支持]

- 在 qBittorrent WebUI 增加打开本地文件夹和调用本地播放器的操作。
- 多文件种子默认选择体积最大的文件播放。
- 默认按路径转换后从本地/挂载盘播放。
- [实验/无支持] 客户端找不到本地文件时，可让文件所在服务器上的 etlp 通过 HTTP Range 提供媒体文件；需要局域网监听、相同 token 和额外地址配置。
- 网络转发模式不支持外挂字幕，开放局域网监听时应设置 `http_server_token` 并限制可信网络。
- 主要位置：`qbittorrent_webui_open_file/qbittorrent_webui_open_file.js`、`utils/http_server.py`、`utils/tools.py`。

## 7. 实验和隐藏功能

以下功能代码已经存在，但不应当作默认能力承诺。除非明确需要，否则保持关闭。

| 功能 | 作用 | 关键配置/入口 | 主要限制 |
| --- | --- | --- | --- |
| ISO/BDMV 原盘播放 | 按路径切换 VLC、PotPlayer 或 mpv 播放原盘 | `player_by_path`、`strm_direct_host`、读盘模式 | ISO 不回传进度；Pot/mpv 需要本地挂载，VLC 更适合菜单展示。 |
| 本地 URL 替换 | 把源视频流地址替换成本机 alist/nginx 地址 | 隐藏配置 `stream_redirect` | 配置成对地址，错误替换会直接导致无法播放。 |
| 局域网 STRM 进度 | 在长期运行的另一台 etlp 上保存缺失时长 STRM 的临时进度 | `listen_on_localhost = no`、`server_side_href` | 无持久数据库；开放监听有安全风险。 |
| mpv IPC 数据传递 | 向 mpv Lua 脚本发送命令管道和播放列表数据 | `mpv_input_ipc_server`、`mpv_ipc_playlist_data` | 播放列表数据较大；静态管道可能影响回传。 |
| mpv 自动跳过片头片尾 | 根据 Emby 片头数据或章节标题/时长自动跳过或提示 | 隐藏配置 `skip_intro` | 依赖章节或扫描结果；规则误判会错误跳转。 |
| mpv 独立同步 Bangumi/Trakt | 不经过网页调用也可在播放超过 90% 后同步 | 独立 Lua 脚本 + 第三方同步配置 | 只面向 mpv 网络视频流，仍依赖媒体服务器用户信息。 |
| 预读取下一集 | 在当前集播放到指定比例后读取下一集首尾数据 | `prefetch_percent`、`prefetch_path`、`prefetch_host` | 主要为 nginx 分片缓存设计，配置复杂且消耗网络流量。 |
| 预读取继续观看 | 预取最近 7 天更新剧集并尝试补全 STRM 媒体信息 | `server_data_group`、`prefetch_conf` | 适合常开设备；需要 API key 和 user_id。 |
| Telegram 追更通知 | “继续观看”更新时通过机器人通知 | `[tg_notify]` | 依赖预读取继续观看和 Telegram 网络；默认配置模板未展示。 |
| 持久性缓存/边下边播 | 把网络视频缓存到本地并提供下载管理器 | `[gui]` | 未适配播放列表；NTFS 稀疏文件体验较差；会占用大量磁盘。 |
| 弹弹play | 调用弹弹play并尝试同步开始时间/播放位置 | `[dandan]` | API 启动慢，HTTP 模式需等待且无外挂字幕。 |
| PotPlayer S0 混播 | 让 Pot 读盘播放列表遵循 Emby 顺序并插入 S0 | `pot_conf`、`mix_s0` | 列表添加变慢，需要专用 Pot 配置。 |
| 媒体标题字符替换 | 避免高版本 PotPlayer 因标题字符导致启动失败 | `media_title_translate` | 错误替换可能影响显示或其他播放器，只建议用于 Pot。 |
| 其他版本字幕 | 当前版本无合适字幕时尝试使用其他版本字幕 | `sub_extract_priority` | 版本间时间轴可能不一致，不能保证同步。 |

## 8. 配置区域速查

| 配置区域 | 主要负责 |
| --- | --- |
| `[exe]` | 播放器别名和可执行文件路径。 |
| `[emby]` | 默认播放器、最终进度回传、全屏。名称沿用历史，但也影响 Jellyfin/Plex 主流程。 |
| `[src]` / `[dst]` | 服务端路径到客户端本地/挂载路径的成对转换。 |
| `[playlist]` | 连续播放启用范围、版本匹配、条目限制和简易自动下一集。 |
| `[dev]` | 字幕、版本、代理、重定向、STRM、日志、进程、实时反馈等高级设置。 |
| `[watch_together]` | [实验] 两人 Emby 同步观看房间开关、管理员地址和本机 admin key。仅 Emby 使用。 |
| `[bangumi]` | Bangumi 单向观看记录同步。 |
| `[trakt]` | Trakt 单向观看记录同步与 OAuth 应用信息。 |
| `[gui]` | 隐藏的持久缓存、下载和任务管理功能。 |
| `[tg_notify]` | 隐藏的 Telegram 追更通知。 |
| `[dandan]` | 隐藏的弹弹play 支持。 |

浏览器脚本中的 `config` 对象和油猴本地存储不属于 INI 配置。修改它们只影响浏览器端行为，例如直播是否交给网页播放器、继续观看重排、隐藏剧集和完整路径显示。

## 9. 后续修改导航

| 想修改的目标 | 先检查的配置/前端开关 | 主要模块 | 需要联动回归的范围 |
| --- | --- | --- | --- |
| 增加或更换播放器 | `[exe]`、`[emby] player`、`player_by_path` | `utils/tools.py`、`utils/players.py` | 启动参数、开始时间、字幕、播放列表、停止时间。 |
| 修改网页播放按钮行为 | `webPlayerEnable`、`mountDiskEnable`、脚本 `config` | `user_script/embyToLocalPlayer.user.js` | Emby/Jellyfin/Plex 首页、详情页、列表播放。 |
| 修改路径转换/读盘规则 | `[src]`、`[dst]`、`force_disk_mode_path`、`path_check` | `utils/tools.py`、`utils/data_parser.py` | Windows 路径、Linux/macOS 路径、NFC/NFD、STRM。 |
| 修改 STRM 本地定位 | `strm_direct_host`、`strm_local_*` | `utils/data_parser.py`、`utils/net_tools.py` | URL path、查询参数、后缀、冷挂载、字幕缓存、多版本。 |
| 修改版本选择 | `version_prefer`、`version_filter` | `utils/tools.py`、`utils/data_parser.py` | 首页播放、手选版本、下一集同版本、播放列表失败回退。 |
| 修改字幕策略 | `subtitle_priority`、`sub_extract_priority` | `utils/data_parser.py`、`utils/players.py` | 网络/本地字幕、内置/外挂字幕、各播放器参数。 |
| 修改连续播放 | `[playlist]` | `utils/player_manager.py`、`utils/data_parser.py`、`utils/players.py` | 多集回传、版本匹配、S0、HTTP 外挂字幕、各播放器列表 API。 |
| 修改实时进度反馈 | `playing_feedback_*` | `utils/player_manager.py`、`utils/net_tools.py` | mpv/IINA 单集和列表、暂停/恢复、Emby/Jellyfin 会话状态。 |
| 修改最终进度回传 | `[emby] update_progress` | `utils/net_tools.py`、`utils/http_server.py` | Emby/Jellyfin/Plex、短视频、播放完成、ISO/M3U8。 |
| 修改 Bangumi/Trakt 同步 | `[bangumi]`、`[trakt]` | `utils/bangumi_sync.py`、`utils/trakt_sync.py` | ID 匹配、完成阈值、令牌、Plex 差异。 |
| 修改继续观看排序/隐藏 | 浏览器脚本 `config` 和本地存储 | `user_script/embyToLocalPlayer.user.js` | 接口字段、前两项保序、三天范围、隐藏列表。 |
| 修改同步观看房间 | `[watch_together]`、管理员油猴菜单 | `user_script/embyToLocalPlayer.user.js`、`utils/watch_together_coordinator.py`、`utils/watch_together_store.py` | Emby 同服两用户、mpv/IINA 控制会话、loopback 认证、房间 JSON 完整性。 |
| 修改缓存/边下边播 | `[gui]` | `utils/downloader.py`、`utils/gui.py`、`utils/http_server.py` | 磁盘空间、稀疏文件、恢复任务、删除阈值、播放回退。 |
| 修改 qBittorrent 联动 | `server_side_href`、`http_server_token`、路径转换 | `qbittorrent_webui_open_file/qbittorrent_webui_open_file.js`、`utils/tools.py` | 单/多文件种子、本地路径、HTTP Range、安全性。 |

## 10. 已知边界与修改原则

- 项目主要依赖浏览器网页接口和播放器外部控制接口，两端升级都可能造成兼容问题。
- 数值和行为优先放在 INI 或油猴脚本配置中，不应在多个模块重复硬编码。
- 修改播放流程时，至少分别验证网络模式、读盘模式、单集、播放列表和 STRM。
- 修改进度逻辑时，必须区分实时反馈、播放器退出后的最终回传、第三方完成同步三个阶段。
- 新增播放器不能只验证“能启动”，还要明确开始时间、字幕、连续播放和进度回传分别是否支持。
- 开放 `listen_on_localhost = no` 或媒体文件 HTTP 转发时，应限制在可信局域网并设置 token；当前本地服务不是面向公网设计的通用媒体服务器。
- 评分回填、缓存删除等会修改外部数据或本地文件的功能，应继续保留预演、确认或明确阈值，避免不可逆操作。
