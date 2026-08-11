# Knowledge Review 模板

在具有实质性代码修改、Bug、架构或兼容性调查的任务结束时复制本模板，填入任务
报告或对应 Stack 的完成报告。没有新增内容时保留“无”并说明已检查范围。

```text
Knowledge Review
任务/Stack:
验证范围:

Knowledge Findings:
- 新增约束: 无
- 隐蔽坑: 无
- 被证明错误的假设: 无
- 建议沉淀项: 无

证据:
- 代码:
- 测试:
- 实际运行/日志/可复现结果:

去重检查:
- 已搜索的文档/关键词:
- 是否更新已有结论: 否

分流判断:
- docs/lessons-learned.md: 不需要 / 新增 / 更新
- docs/architecture.md: 不需要 / 更新
- docs/adr/: 不需要 / 新增或更新 ADR-___

未验证范围与残余风险:
```

## 使用规则

1. 先搜索 `AGENTS.md`、`docs/architecture.md`、`docs/lessons-learned.md` 和
   `docs/adr/`，避免把同一事实重复写入多个地方。
2. 只有代码、测试、实际运行/日志、可复现结果，或官方文档与当前实现交叉确认的
   内容才可以进入正式知识库。
3. 任务日志、临时状态、服务器拓扑、凭据、token、私钥和个人绝对路径不进入正式
   知识库；本机长期运维信息只按 `LOCAL_OPERATIONS.md` 规则保留。
4. 主线程审核证据和分流结果；子代理的 `Knowledge Findings` 是输入，不是最终验收
   证据。
