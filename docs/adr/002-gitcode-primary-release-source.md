# ADR-002: GitCode 作为默认 Release 下载源

- 状态: accepted
- 日期: 2026-09-01
- 相关组件: `utils/update.py`、`scripts/release_publish.ps1`、发布包用户脚本元数据

## 背景

ETLP 的 beta 和 stable 安装包按频道发布，并通过 ZIP、SHA-256 sidecar 和可选的
`release-plan.json` 完成更新前校验。国内网络环境下 GitCode Release 附件更适合作为
默认下载入口，但 GitHub 仍需要保留为可审计的备用来源。

## 问题

GitCode 的 Release API 响应结构与 GitHub 不同，附件通常通过
`browser_download_url` 提供，缺失时应使用官方 attachment download endpoint。用户脚本
还需要稳定的 branch raw URL；当前 GitCode 内容 API 返回的链接包含 blob SHA，不能把它
当作长期稳定的分支更新入口。

## 可选方案

1. 继续只使用 GitHub Release。
2. 只使用 GitCode Release。
3. 按 GitCode、GitHub 的顺序查询同频道 Release，在来源切换时重复全部现有校验。

## 最终选择

采用方案 3。更新器先查询
`https://api.gitcode.com/api/v5/repos/h0pe14o/embyToLocalPlayer/releases`，仅当 GitCode
API 异常、没有携带频道 ZIP 与 sidecar 的合格 Release，或选定 Release 的附件下载、sidecar、
manifest、ZIP 大小或 SHA-256 校验失败时查询 GitHub。两端的 Release 都必须通过频道筛选、
SHA-256 校验、可选 manifest 校验以及 ZIP 安全校验；两端都失败时保持 fail-closed，不替换
现有安装。

发布脚本的 dry-run 显示两个平台的目标资产。获得明确执行授权时，脚本要求 GitCode CLI
已登录或通过 `GC_TOKEN`/`GITCODE_TOKEN` 环境变量认证，要求两个平台的 tag 已存在，然后
创建 GitHub Release 并上传同一组 ZIP、sidecar 和 `release-plan.json` 到 GitCode。脚本不
自动创建 tag 或 push 分支，也不把认证信息写入源码、配置或输出。

## 选择原因

- GitCode 是默认路径，满足国内下载速度和可达性诉求。
- GitHub 作为明确回退，降低单一平台 API 或网络故障对更新的影响。
- 来源差异只影响 Release 查询和附件 URL，内容信任仍由现有 SHA-256、manifest 和 ZIP
  安全检查承担。
- 用户脚本继续使用已实测的 GitHub branch raw URL，避免依赖不稳定的 blob-specific 地址。

## 已知代价

- 发布维护者需要在两个平台提供同一版本和同一组资产；任一平台缺资产时客户端会回退或
  fail-closed。
- GitCode CLI 是发布时的额外工具要求；未登录或未安装时，发布脚本会在远端写入前停止。
- GitHub 和 GitCode 的 Release 元数据可能存在延迟，发布后应分别核对资产和哈希。
- 来源回退会增加一次网络请求和校验时间，但不会降低任一来源的验证门槛。

## 后续影响

- 新增或修改 Release 源时，必须同步维护 `tests/test_update.py`、发布脚本 dry-run 和本
  文档。
- 不得将 GitCode 仓库代码镜像、普通内容 API 或 branch raw URL 当作 Release 附件的替代品。
- 任何远端已有资产的哈希不一致都不能被脚本自动镜像或覆盖，应先停止发布并修正资产。

## 验证依据

- `utils/update.py`：GitCode API 优先、GitHub 回退、资产 URL 和校验流程。
- `tests/test_update.py`：两源选择、API 异常、无合格资产和双源失败场景。
- `scripts/release_publish.ps1`：plan/ZIP/sidecar/元数据预检及双平台 dry-run 命令。
- GitCode Release API 文档：Release 列表返回 `assets[].name` 与
  `assets[].browser_download_url`，附件下载 endpoint 为
  `/api/v5/repos/:owner/:repo/releases/:tag/attach_files/:file_name/download`。
