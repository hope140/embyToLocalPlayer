# etlp - embyToLocalPlayer

> **当前定位**：这是 [hope140/embyToLocalPlayer](https://github.com/hope140/embyToLocalPlayer) 的独立维护版本，面向本地播放器、路径转换、STRM 和播放进度回传。它基于并跟踪上游 [kjtsune/embyToLocalPlayer](https://github.com/kjtsune/embyToLocalPlayer)，但不是上游默认能力的完整镜像；本文只描述仓库当前代码和配置实际提供的能力。

## 分支与发布频道

| 分支 | 角色 | 打包与 Release | 油猴脚本入口 |
| --- | --- | --- | --- |
| `stable`（默认） | 稳定版 | `scripts/package_stable.ps1`；资产为 `etlp-remote-control-stable.zip` 和对应 `.sha256` | [stable 用户脚本](https://raw.githubusercontent.com/hope140/embyToLocalPlayer/stable/user_script/embyToLocalPlayer.user.js) |
| `beta` | 测试版 | `scripts/package_beta.ps1`；tag 以 `-beta` 结尾；资产为 `etlp-remote-control-beta.zip` 和对应 `.sha256` | [beta 用户脚本](https://raw.githubusercontent.com/hope140/embyToLocalPlayer/beta/user_script/embyToLocalPlayer.user.js) |
| `main` | 上游同步 | 只用于同步上游，不从此分支打包或发布 | 不作为安装入口 |

beta 和 stable 保留相同的 Tampermonkey 脚本身份，安装时只选择一个频道，不要同时安装两份。更新器读取安装包内的频道元数据，只查找对应频道的 Release 资产，不使用跨频道的 `Latest` 下载地址。

为兼容尚未升级的旧 beta 安装，当前 stable 的 Latest Release 暂时额外保留旧版更新器请求的
`etlp-remote-control-beta.zip` 和对应 `.sha256` 兼容资产。这两个文件内容仍是 beta 包；新更新器不会跨频道使用它们。

## 开源许可与致谢

本项目遵循 [Apache License, Version 2.0](LICENSE)。本仓库基于并跟踪上游 [kjtsune/embyToLocalPlayer](https://github.com/kjtsune/embyToLocalPlayer)，在保留原有许可和归属信息的前提下进行独立维护与扩展。感谢上游作者 **kjtsune** 及所有贡献者的开源工作。

再分发或发布修改版本时，请按 Apache License, Version 2.0 要求保留许可证、版权和归属声明，并明确标注已修改的文件或内容。

## 与上游及其他分支的差异

| 范围 | 当前代码 | 说明 |
| --- | --- | --- |
| CloudDrive2 STRM | 支持可选的本地 CloudDrive2 gRPC 解析，并通过 ETLP 本地短期 gateway URL 播放 | 需要本机可访问的 CloudDrive2 API、有效 token 和明确的 `path_map`；这是本分支的实验扩展，不应当当作上游默认能力。 |
| Emby 控制台远程控制 | 内置当前机器播放器的独立控制通道，面向 mpv/IINA | `[remote_control] enable = yes` 默认开启；控制当前播放器的暂停、继续、seek、停止和消息显示，不建立房间、不让多台设备互相跟随。 |
| 实时进度回传 | mpv/IINA 可在播放中回传进度和暂停状态 | 由 `[dev] playing_feedback_*` 控制；其他播放器仍可在退出时回传最终进度。 |
| 同步观看房间 | **不包含** | 本分支不提供房间、参与者、同步播放或跟随观看能力；该能力请使用独立项目 [EmbyWatchTogether](https://github.com/hope140/EmbyWatchTogether)。 |
| 旁线功能 | **不作为本分支能力提供** | 不承诺豆瓣/Bangumi、Simkl/Trakt、聚合搜索、qBittorrent 联动等能力；不要把上游 README 或其他实验分支的旧文案复制到这里。 |

## 主要能力

- 从 Emby/Jellyfin 网页调用本地播放器；脚本保留部分 Plex 兼容逻辑，具体以实际页面和接口为准。
- 支持网络播放、读取硬盘模式、服务端路径到本地/挂载路径的转换，以及 `.strm` 直连或本地定位。
- 支持单集和播放列表连续播放；可按版本关键词选择视频并尝试保持下一集版本。
- 支持外挂字幕、字幕缓存、日志和播放器按路径选择等配置化能力。
- mpv、mpv.net、PotPlayer、MPC-HC、MPC-BE、VLC、IINA 等播放器可以通过 `[exe]` 配置；实时反馈主要面向 mpv/IINA，播放器差异见 [FUNCTIONS.md](FUNCTIONS.md)。

## 安装与启动

### 1. 安装浏览器脚本

1. 安装 Tampermonkey 或 Violentmonkey。
2. 按需安装一个频道的 [stable 用户脚本](https://raw.githubusercontent.com/hope140/embyToLocalPlayer/stable/user_script/embyToLocalPlayer.user.js) 或 [beta 用户脚本](https://raw.githubusercontent.com/hope140/embyToLocalPlayer/beta/user_script/embyToLocalPlayer.user.js)，刷新 Emby/Jellyfin 页面。两者不要同时安装。
3. 从 [hope140 Releases](https://github.com/hope140/embyToLocalPlayer/releases) 选择对应频道的 Release，下载 `etlp-remote-control-stable.zip` 或 `etlp-remote-control-beta.zip` 及其 `.sha256`，解压到英文路径。发布包包含 `embyToLocalPlayer_config.ini`、运行时依赖和 Windows 启动脚本。

### 2. Windows

发布包根目录的 `embyToLocalPlayer_debug.bat` 提供以下入口：

- `1`：在当前窗口启动 `embyToLocalPlayer.py`，便于查看日志。
- `2`：写入 Windows 启动文件夹并后台启动。
- `3`：打开启动文件夹。
- `4`：打开路径转换辅助工具。
- `6`：运行当前安装包频道的更新程序；源码分支和包内频道元数据必须一致。

源码检出时，启动脚本位于 `utils/others/embyToLocalPlayer_debug.bat`；也可以直接运行：

```powershell
python embyToLocalPlayer.py
```

看到日志中的 `serving at 127.0.0.1:58000` 后，再回到网页点击原有播放按钮测试。关闭控制台会停止本地服务。

### 3. macOS / Linux

在仓库根目录运行：

```bash
python3 embyToLocalPlayer.py
```

源码中的 `utils/others/etlp_run.command` 可作为 macOS/Linux 的简单启动入口；macOS 需要先赋予执行权限。开机启动请使用系统自己的登录项或桌面会话配置，先确认手动启动和播放正常。

## 基础模式与配置

所有配置位于 `embyToLocalPlayer_config.ini`；修改后重启本地服务。

### 读取硬盘模式与路径转换

- 浏览器脚本的 `mountDiskEnable` 控制是否把媒体交给本地/挂载文件；`webPlayerEnable` 可保留网页播放器。
- `[src]` 与 `[dst]` 使用同名键成对配置：把 Emby/Jellyfin 显示的服务端前缀替换为客户端本地前缀。客户端必须确实能访问目标文件或挂载点。
- `force_disk_mode_path` 可按服务端路径前缀强制读取硬盘模式；`path_check` 控制路径转换时是否额外检查文件存在。

### CloudDrive2 STRM 前置条件

`[clouddrive2]` 默认配置为：

```ini
[clouddrive2]
enable = yes
origin = http://127.0.0.1:19798
api_token =
path_map = X:\115=>/115open/115
request_timeout_seconds = 2
```

使用前必须满足：

1. 本机 CloudDrive2 的 gRPC API 可从 `origin` 访问；该地址不是 Emby 登录地址。
2. 提供 CloudDrive2 API token。优先设置环境变量 `ETLP_CLOUDDRIVE2_TOKEN`，否则填写 `api_token`；不要把真实 token 提交到仓库。
3. 为 Windows 盘符或 UNC 路径配置明确的 `path_map`，格式为 `本地前缀=>云端前缀`。它必须和 `[src]/[dst]` 产出的本地挂载路径一致。
4. ETLP 会把可解析的本地 `.strm` 路径登记到本地短期 `/cd2/<nonce>` gateway，再按需解析 CloudDrive2 URL；解析失败会回退原挂载盘文件，不应把 gateway 当成公网媒体服务。

没有 token、`path_map` 或可用 gRPC 依赖时，CloudDrive2 gateway 不会接管播放；这不等同于 CloudDrive2 已经配置成功。

### 进度回传与 Emby 控制台

```ini
[emby]
update_progress = yes

[dev]
playing_feedback_enable = yes
playing_feedback_interval = 30
playing_feedback_host =

[remote_control]
enable = yes
```

- `playing_feedback_enable` 控制 mpv/IINA 的实时进度和暂停状态回传；`playing_feedback_interval` 最低为 10 秒；`playing_feedback_host` 可限制生效的服务器域名关键字。
- `[emby] update_progress` 控制播放器退出后的最终进度回传。
- `[remote_control] enable` 控制当前 mpv/IINA 的 Emby 控制台通道。服务端需要识别到对应活动会话和能力声明，网页控制台按钮才会出现；关闭它不会关闭普通播放或最终进度回传。

更多配置、模块导航、支持矩阵和边界约束见 [FUNCTIONS.md](FUNCTIONS.md)。

## 不在当前应用范围内

以下内容不属于当前应用的交付承诺：

- 同步观看房间、参与者管理、跨设备跟随播放；该能力请使用独立项目 [EmbyWatchTogether](https://github.com/hope140/EmbyWatchTogether)。
- 豆瓣/Bangumi、Simkl/Trakt、聚合搜索、qBittorrent 联动等旁线集成；
- 把 CloudDrive2 gateway 直接暴露到公网，或把本地服务当成通用媒体服务器。

如果需要比较上游实现，请直接查看 [上游仓库](https://github.com/kjtsune/embyToLocalPlayer)；上游文档中的功能、配置和 FAQ 不自动适用于本分支。
