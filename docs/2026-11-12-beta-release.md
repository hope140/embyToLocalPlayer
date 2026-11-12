# 2026.11.12-beta 发布记录

状态：已按双平台流程发布测试版。此前使用验收已由维护者于 2026-11-12 确认，不重复要求使用验收。

## 源码与资产

- 源码：`a3231f3570798e41ed4742faac519c85d84ee703`，tree `f54580af973deab4d55c12d6892c632cb6f9b87b`。
- 相对 `2026.09.12.3-beta` 为既有 8 个提交；本次没有新增产品行为。
- ZIP：`etlp-remote-control-beta.zip`，13,329,701 bytes。
- SHA-256：`7d868ab5515661c65bb06dbdaab7f6b208949b152bb84888bafc508cafcdaa70`。
- 两平台提供相同的 ZIP、SHA-256 sidecar、`release-plan.json` 与发布说明。

## 验证与验收边界

- Python 3.12.13 和官方 Windows Python 3.9.13 x86 各完成 332 项测试，全部通过；运行时可执行文件的 Python Software Foundation 签名有效。
- 实际 ZIP 的版本、源码、频道、大小和哈希一致；离线 requests、grpc、protobuf 及项目内置 websocket wheel 加载通过。
- 使用独立配置、临时锁目录和回环端口验证解压成品的启动、正常关闭及再次启动；未操作现有播放器或服务。
- 维护者确认此前使用验收已完成。现有记录没有锁定此前验收包的精确哈希、客户端版本和逐平台场景，不将此扩写成所有 Emby/mpv/IINA/CD2 组合全量通过。本次新构建包的自动验证与此前使用验收分别记录。
- 首轮测试缺少 requests、历史 Git 对象，并遇到控制台编码问题；补齐隔离依赖、准确历史对象和 UTF-8 后完整重跑通过。成品探针按产品 wheel 加载与 JSON 关闭接口契约修正后通过，未修改产品或放宽断言。

## 发布状态

- [GitHub 测试版](https://github.com/hope140/embyToLocalPlayer/releases/tag/2026.11.12-beta)
- [GitCode 测试版](https://gitcode.com/h0pe14o/embyToLocalPlayer/releases/tag/2026.11.12-beta)

GitCode 为拉取镜像，直接创建标签被仓库规则拒绝后未重试或改动配置。维护者通过“项目设置 → 仓库镜像 → Pull → 立即同步”正常同步；两平台标签均核实指向上述完整源码提交，再按现有 `scripts/release_publish.ps1` 执行双平台测试发布。

两平台公开下载的 ZIP、sidecar 和发布清单逐项与本地已验证资产 SHA-256 一致。ZIP 内版本为 `2026.11.12-beta`，频道为 `beta`，源码标识为 `a3231f357079`。ETLP 使用 SHA-256 与发布清单核验，没有独立的产品签名流程；Python 运行时签名验证单独记录于上文。

发布前 GitCode 列表解析曾因控制台编码失败，未创建 Release；在 UTF-8 控制台中解析通过后重跑原脚本完成，未修改产品或发布脚本。

公开 beta 已更新为 `2026.11.12-beta`，stable 仍为 `2026.09.11`。未修改 main/stable、旧发布资产或部署设备。后续发布记录提交仅更新文档，不改变已固定的包源码和标签。
