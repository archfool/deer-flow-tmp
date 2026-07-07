# 保险销售领域模块

本模块是基于 DeerFlow 通用工作流和主体记忆能力构建的第一个领域纵向切片，负责客户家庭档案和 DRAFT 保障检视。它被刻意放在 `app` 层：可发布的 Harness 只负责运行任务和版本化文档，不理解保险术语或公式。

## 范围

第一阶段提供：

- 每个 `(认证用户, customer_id)` 一份独立客户档案；
- 显式输入或权威接口更新采用乐观锁，并保留完整修订历史；
- 模型推断先进入候选区，人工接受后才能生效；
- 家庭级保障检视任务和成员级输入；
- 寿险、重疾、医疗、意外、养老、应急储备六个维度；
- 保守、基准、充分三个 DRAFT 场景；
- 相互独立的客户可见版和内部诊断版报告 Schema；
- Mock 外部数据 Adapter 和合成测试数据；
- 用于启动、暂停、恢复和推进任务的 API 与 Agent Tool。

本模块不提供经过验证的行业参数、产品推荐、保费报价或经过法务审批的客户报告。在正式业务数据替换参数表之前，所有报告都带有 DRAFT/演示免责声明。

## 目录说明

- `models.py`：家庭、成员、财务、保单和字段来源模型。
- `profile.py`：主体记忆之上的 Schema 校验服务。
- `adapters.py`：外部客户数据端口和确定性 Mock。
- `mock_data.py`：仅用于开发和测试的合成数据。
- `coverage_review/`：任务定义、可执行 Skill、公式和两套独立报告渲染器；DAG 说明见其 README。
- `service.py`：连接客户档案、Adapter 和工作流的应用服务。
- `router.py`：需要认证的 Gateway API。
- `tools.py`：供保险 Agent Tool Group 使用的 LangChain Tool。

## 数据所有权

`owner_id` 始终由 Gateway 或 Tool Runtime 解析，不会作为模型可见参数。任务创建后永久绑定一个 `customer_id`；对话切换到其他客户不会改变已有任务的归属。

客户事实不会填充默认值。`None` 表示未知，空列表表示用户或权威接口已经明确确认“没有记录”。这一区别对负债和现有保单尤其重要：保障未知绝不能解释为保障为零。

## 更新客户档案

API 接收 JSON Pointer Patch。服务先把 Patch 应用到副本，校验完整 `CustomerProfile`，然后才执行持久化。

- `user_explicit` 和 `authoritative_api` 可以立即提交。
- `model_inference` 创建待确认候选项。
- `system_derived` 会被拒绝；计算结果属于任务输出，不属于客户事实。

## 接入真实数据服务

实现 `CustomerDataAdapter` 并注入 `InsuranceService`。HTTP 鉴权、超时和重试逻辑应封装在 Adapter 内。返回数据必须先转换为通过校验的客户档案 Patch，再以 `AUTHORITATIVE_API` 来源提交；原始远端响应不得直接放入 LLM Prompt。

