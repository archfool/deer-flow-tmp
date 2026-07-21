# 保险销售领域模块

本模块在 DeerFlow Harness 上提供客户档案和企业级保障检视算法纵向切片。它位于 `app` 层：Harness 只负责持久化工作流、中断恢复和版本化主体记忆，不理解保险维度、理由码或报告口径。

## 功能范围

- 每个 `(认证用户, customer_id)` 独立存储家庭客户档案。
- 显式事实和权威接口更新使用乐观锁；模型推断必须先进入待确认候选区。
- 通过客户中心、项目档案、中保信报告和精确测算四类 Tool 形成证据链。
- 执行疾病、医疗、伤残、护理、身故、财富、养老和传承八维保障检视。
- 将应急流动性作为辅助财务指标，不将它伪造成保障缺口。
- 对内诊断、代理人复核、局部失效重算和批准后对客 HTML 全流程可持久化。
- 对内报告和对客文案通过结构化 Narrative Harness 生成，规则码和审计字段与业务可见内容隔离。
- 代理人复核支持批准、自然语言修改事实、调整话术方向和驳回；批准前不会生成任何对客产物。
- 向 DeerFlow 主 Agent 暴露客户档案 Tool 和完整的保障检视生命周期 Tool。

## 模块边界

- `models.py`：家庭、成员、财务、保单和字段来源模型。
- `profile.py`：基于 Subject Memory 的客户档案校验和版本服务。
- `adapters.py`：外部客户数据端口。
- `coverage_review/`：规则 Harness、领域 Tool、证据、诊断内核、投影、报告和 DeerFlow DAG。
- `service.py`：连接客户档案、领域 Tool 和 WorkflowEngine。
- `router.py`：认证 HTTP API，包括追问补充、复核视图和自然语言复核意见。
- `tools.py`：供 DeerFlow 主 Agent 调用的 LangChain Tool。

## 数据与审批

`owner_id` 始终由 Gateway 或 Tool Runtime 解析，不作为模型可见参数。`None` 表示未知，空列表表示已明确确认没有，两者不可互换。

对客报告不会因为某个 Prompt 提示而生成。它只在代理人批准当前 `kernel_hash` 后产生，并在 manifest 中记录审批人、证据哈希、内核哈希、规则包和模板版本。

代理人任务卡只接收窄化后的复核视图，不下发完整 `ReviewPacket`、内部报告或理由码。事实修改会重算并再次停在复核闸门，叙事修改保持诊断内核不变。

详细架构、运行命令和真实 Tool 接入方式见 [`coverage_review/README.md`](coverage_review/README.md)。
