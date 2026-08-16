# ADR-001: 分离 beta 与 stable 的发布频道

- 状态: accepted
- 日期: 2026-08-14
- 相关组件: Git 分支、PowerShell 打包、Python 更新器、油猴脚本

## 背景

仓库同时维护测试版和稳定版。GitHub 的 Latest 下载入口只有一个
Latest 指针，不能同时表达 beta 和 stable；如果两个频道共用包名或更新地址，测试包
可能覆盖稳定安装，油猴脚本也可能被另一个频道自动替换。

## 问题

需要让分支、打包物、Release 资产、更新器和油猴脚本的入口保持同一频道，并兼容已有
没有频道元数据的 beta/源码安装。

## 可选方案

1. 继续共用 Latest 和同一个包名，由用户自行判断版本。
2. 为每个频道使用独立包名和 tag，更新器查询 Releases API 后选择对应频道资产。
3. 维护两套完全不同的脚本身份，使 beta 和 stable 可以并行安装。

## 最终选择

选择方案 2，并配合以下约束：

- `stable` 是默认分支和稳定频道，`beta` 是测试频道，`main` 只同步上游。
- `package_release.ps1` 要求当前分支严格等于 `-Channel` 参数；beta 版本以 `-beta`
  结尾，stable 版本不以 `-beta` 结尾。两个 wrapper 分别固定传入 beta/stable。
- 频道使用不同资产名：`etlp-remote-control-beta.zip` 和
  `etlp-remote-control-stable.zip`，各自带同名 `.sha256` sidecar。
- GitHub 展示规则固定为：stable Release 标记为仓库 `Latest`，beta Release 标记为
  `Prerelease`；更新器仍按频道查询 Releases API，不把 GitHub 的 `Latest` 当作频道选择依据。
- `release_info.py` 将频道写入运行包。更新器访问 GitHub Releases API，只选择非 draft、
  同频道 tag 且同时有 ZIP 和 sidecar 的 Release，再下载明确 tag 下的资产，不使用
  `Latest` 地址。
- 新 Release 可以额外上传 `release-plan.json`。发现该资产时，更新器在下载 ZIP 前校验
  schema、频道、tag、分支、资产名和 sidecar SHA-256，并在下载后校验包大小；历史 Release
  没有该资产时仍走原有的三次请求和 SHA-256 兼容路径，避免破坏已存在的发布物。
- 为兼容已部署的旧 beta 更新器，stable Latest Release 在过渡期额外保留旧路径所需的
  `etlp-remote-control-beta.zip` 和 `.sha256` 兼容资产。它们必须是当前 beta 包的原样副本，
  新更新器不得选择这两个兼容资产。
- 油猴脚本保留相同的 `@name` 和 `@namespace`，但 update/download/homepage/support
  URL 指向当前频道；打包阶段再次规范化这些 URL，防止源码分支漂移。用户只安装一个频道。

## 选择原因

独立包名和明确 tag 能在下载前建立频道边界，分支门禁能阻止从 `main` 或错误分支生成
发布包，API 选择能避免 Latest 指针串频道。保留脚本身份可让用户切换频道时覆盖同一
脚本，不会留下两个同时运行的脚本实例。

## 已知代价

- 更新器需要额外请求 GitHub Releases API，并依赖每个频道同时上传 ZIP 和 sidecar。
- 含清单的 Release 会额外请求一个小型 JSON 资产；清单缺失仍可兼容历史 Release，但
  清单存在且校验失败时更新会失败关闭。
- 过渡期 stable Release 会多出两个 beta 兼容资产；旧客户端完成一次自举升级后应移除，
  之后 stable Release 恢复只保留 stable 正式资产。
- API 列表缺少合格资产、tag 不合规或 sidecar 校验失败时更新会失败关闭，用户需要
  手动选择正确频道或恢复历史包。
- stable 和 beta 的油猴 raw URL不同，安装说明必须明确要求只选择一个频道。

## 后续影响与回滚

源码安装和缺少/非法 `RELEASE_CHANNEL` 的旧安装按 beta 兼容。回滚代码时应按 Stack
commit 逐个 revert，并保留历史 tag；不要用强制推送覆盖稳定分支，也不要把 beta 包
改名后冒充 stable。

## 验证依据

- `scripts/package_release.ps1`、`scripts/package_beta.ps1`、`scripts/package_stable.ps1`
- `utils/release_info.py`、`utils/update.py`、`utils/configs.py`
- `tests/test_update.py`、`tests/test_release_info.py`、`tests/test_user_script_channel.py`
- beta/stable 临时命名分支上的实际 ZIP、频道元数据和 SHA-256 sidecar smoke test
