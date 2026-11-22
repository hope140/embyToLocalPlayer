# 2026.11.22-beta 发布记录

状态：已发布双平台测试版。[GitCode 测试版](https://gitcode.com/h0pe14o/embyToLocalPlayer/releases/tag/2026.11.22-beta) · [GitHub 测试版](https://github.com/hope140/embyToLocalPlayer/releases/tag/2026.11.22-beta)。

## 源码与变更

- beta 源码：`cf9391b56c9812d600e410b7951529242b78484b`，tree `b952a5e45c0c9afd58d545caedd8c720a90183f7`。
- [PR #9](https://github.com/hope140/embyToLocalPlayer/pull/9) 移除旧 qBittorrent 播放及专用媒体转发入口，清理关联的未使用代码、油猴权限和示例配置。正常的媒体服务器播放、CD2 和 Emby 远控路径保留。
- 上一测试版的使用验收记录见 [2026.11.12-beta 发布记录](2026-11-12-beta-release.md)。

## Windows 验证

- Python 3.12 与官方 Windows Python 3.9.13 x86 各通过 332 项测试。嵌入式运行时可执行文件的 Python Software Foundation 签名有效，9 个随包 wheel 的 SHA-256 与源码一致。
- 独立解压，使用包内 Python 验证依赖离线加载、随机回环端口启动与关闭、再次启动。原 `/playMediaFile/`（POST）和 `/send_media_file.mkv`（GET/HEAD）均返回 404。
- 成品测试使用隔离配置及临时目录，未联机播放或验收真实客户端组合。

## 资产

- ZIP：`etlp-remote-control-beta.zip`，13,329,260 bytes。
- SHA-256：`c826db170a80cff6e793d20f7258de3bbdd3d1107876297de11ff02abb30f444`。
- 随发布提供 `etlp-remote-control-beta.zip.sha256` 与 `release-plan.json`。包内频道为 `beta`，版本为 `2026.11.22-beta`，源码标识为 `cf9391b56c98`。

## 发布核验

GitCode 通过现有 Pull 镜像手动同步，两个标签均指向本记录所列提交。现有发布脚本创建两个 prerelease。从两平台公开链接回下载 ZIP、校验文件、发布清单，逐件 SHA-256 均与本地一致，ZIP 内版本、提交与频道也一致。
