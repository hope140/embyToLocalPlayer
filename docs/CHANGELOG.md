# 更新日志

## 2026.08.14-release-channels

### 分支与发布频道整理

- `stable` 作为默认稳定分支，`beta` 作为测试分支，`main` 只用于同步上游。
- 新增统一的频道打包入口：只能从对应分支生成 `etlp-remote-control-stable.zip` 或
  `etlp-remote-control-beta.zip`，并同时生成 `.sha256` sidecar。
- Windows 频道包现在同时携带 Python 3.9 x86 embedded runtime 与 `third_party/*.whl`；
  用户不需要单独安装 Python，首次启动只从包内本地准备依赖，不会联网下载依赖。
- 更新器按安装包内的频道查询 GitHub Releases API，只下载同频道且同时存在 ZIP 与
  sidecar 的 Release 资产；旧版或源码安装继续按 beta 兼容。
- 为兼容旧版 beta 更新器，stable Latest Release 暂时额外保留旧路径所需的 beta ZIP 和
  `.sha256` 兼容资产；新更新器仍按频道 API 选择资产。
- 油猴脚本保留同一脚本身份，update/download/homepage/support URL按频道指向对应
  分支；同一安装不应同时启用 beta 和 stable 两套脚本。
- 历史 beta tag 保留用于回滚，历史说明不因频道整理而改写。

## 2026.08.06-clouddrive2-beta

> 当前 beta 分支包含 CloudDrive2 STRM 支持、本地网关播放、远程轮询稳定性
> 以及 mpv 起播窗口时序优化；同时保留本分支已有的 Emby 控制台遥控能力。

### 新增：Emby 控制台远程控制（mpv/IINA）

- 独立的 `[remote_control] enable = yes`（默认开启）控制通道：当前 mpv/IINA
  播放会以独立 Emby 会话身份建立控制 WebSocket，声明 Pause/Unpause/
  PlayPause/Seek/Stop/DisplayMessage 能力。
- Emby 网页控制台可对当前播放暂停、继续、seek、发送消息；命令会转给本地
  mpv/IINA，并立即回传播放状态（Playing/Progress/Stopped、暂停/恢复/倍速事件）。
- 断线自动重连（指数退避）、心跳保活、暂停状态确认后才回执；所有失败都
  只降级为“无遥控”，绝不影响普通本地播放。
- `websocket-client==1.8.0` 以内置 wheel 形式随包提供（校验 SHA256 后使用），
  无需用户安装 Python 依赖。

### 移除：同步观看房间

- 本版本**不含** watch-together 房间同步（coordinator/store/房间 HTTP 端点/
  油猴同步界面/`[watch_together]` 配置段全部移除）。
- 油猴脚本为无同步功能的版本，元数据和更新地址固定指向本仓库 beta 分支。

### 其他

- 更新器安全化：zip 成员校验（防 zip-slip、符号链接、越界路径）、
  配置文件保护、GitHub 分支归档前缀自动展平；更新源指向本仓库 `beta` 分支。
- 播放进度上报的 `EventName` 修正为服务端协议大小写（`TimeUpdate`）。
- 新增 `scripts/package_beta.ps1` 打包脚本，产出可运行 zip。
- 遥控代码统一更名（remote_control_client / RemoteControlClient），日志
  前缀为 `remote-control`，不再出现 watch-together 字样。
