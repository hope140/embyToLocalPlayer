# 更新日志

## 2026.08.05-remote_control.1（beta → main）

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
- 油猴脚本为无同步功能的原版，仅更新元数据指向本仓库 beta 分支。

### 其他

- 更新器安全化：zip 成员校验（防 zip-slip、符号链接、越界路径）、
  配置文件保护、GitHub 分支归档前缀自动展平；更新源指向本仓库 `beta` 分支。
- 播放进度上报的 `EventName` 修正为服务端协议大小写（`TimeUpdate`）。
- 新增 `scripts/package_beta.ps1` 打包脚本，产出可运行 zip。
- 遥控代码统一更名（remote_control_client / RemoteControlClient），日志
  前缀为 `remote-control`，不再出现 watch-together 字样。
