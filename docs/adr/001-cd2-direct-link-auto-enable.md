# ADR-001: CD2 直链开关采用显式配置下的自动开启

- 状态: accepted
- 日期: 2026-08-13
- 相关组件: `utils/clouddrive2_client.py`、`utils/clouddrive2_gateway.py`

## 背景

CloudDrive2 的部分云盘具备直链能力，但 `supportDirectLink` 可能默认关闭。仅请求
`get_direct_url` 时，服务端会继续返回 `downloadUrlPath`，导致用户需要先进入 CD2
界面手动开启云盘选项。

## 问题

ETLP 是否应在用户明确启用直链后，通过 CD2 API 自动开启该云盘选项，同时保持普通
播放的回退能力。

## 可选方案

1. 只提示用户手动在 CD2 界面开启。
2. 每次 ETLP 启动无条件修改 CD2 云盘配置。
3. 仅在 `get_direct_url = yes` 时按需读取、修改并回读确认。

## 最终选择

选择方案 3。ETLP 读取映射文件所属云盘的配置；只有在云盘报告
`supportDirectDownloadUrl = true` 且 `supportDirectLink = false` 时，才调用
`SetCloudAPIConfig`，并且只改变 `supportDirectLink`。写入失败、权限不足、旧 API 或
回读未确认时继续使用 `downloadUrlPath`/本地路径回退。

## 选择原因

`get_direct_url` 已经是用户明确的实验开关，可以作为外部配置写入的授权边界。按需执行
可避免启动时修改未播放的云盘，也避免在每个 Range 请求上重复写配置。

## 已知代价

- CD2 Token 需要 `allow_get_cloud_apis` 和 `allow_modify_cloud_apis`；账户/云盘还需
  具备 `enable_direct_link` 角色或会员资格。
- CD2 云盘配置会产生持久化的 `supportDirectLink` 变更；用户可关闭
  `get_direct_url` 或在 CD2 界面关闭该选项。
- 自动开启仅证明配置写入成功，不替代真实 CDN、播放器 Range 和会员状态验收。

## 验证依据

- `third_party/clouddrive2/clouddrive.proto` 定义了 `SetCloudAPIConfig`、
  `supportDirectLink` 和 `allow_modify_cloud_apis`。
- `tests/test_clouddrive2_client.py` 覆盖成功、权限/写入失败、不支持云盘和回读校验。
