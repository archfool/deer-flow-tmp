# 保险产品销售对话机器人：技术模块 README

本文档记录这次为了“保险产品销售对话机器人”新增的技术模块。

文档的介绍优先级如下：

1. 优先介绍本次新增的通用代码和通用模块；
2. 重点解释这些通用模块如何解决此前讨论过的三个核心痛点；
3. 最后再介绍“保险产品销售”这个具体产品如何使用这些通用模块落地。

本文不会专门介绍 DeerFlow 原本已经存在的通用模块。已有 Gateway、SQLAlchemy、配置系统、LangGraph agent runtime 等，只会在“本次新增模块如何接入它们”时简要提到。

## 1. 本次新增的通用能力

这次新增的通用能力主要有两组：

| 通用能力 | 代码位置 | 解决的问题 |
| --- | --- | --- |
| 可持久化工作流运行时 | `backend/packages/harness/deerflow/workflows/` | 任务自由跳转、暂停恢复、并行执行、skill 编排 |
| 主体记忆模块 | `backend/packages/harness/deerflow/subject_memory/` | 每个用户的每个业务主体维护独立记忆文件，支持版本、候选更新和来源治理 |

这两组能力都放在 `harness` 层，而不是保险 `app` 层。原因是它们不是保险专用能力，后续可以复用于其他垂直领域。

保险产品销售只是本次的第一个落地场景。

## 2. 新增通用模块总览

### 2.1 通用工作流模块

位置：

```text
backend/packages/harness/deerflow/workflows/
```

文件：

```text
backend/packages/harness/deerflow/workflows/README.md
backend/packages/harness/deerflow/workflows/__init__.py
backend/packages/harness/deerflow/workflows/models.py
backend/packages/harness/deerflow/workflows/registry.py
backend/packages/harness/deerflow/workflows/repository.py
backend/packages/harness/deerflow/workflows/persistence.py
backend/packages/harness/deerflow/workflows/engine.py
```

它提供的是一套通用任务运行时：

- 定义任务模板；
- 定义任务步骤；
- 定义可执行 skill 契约；
- 创建任务实例；
- 持久化任务状态；
- 根据 DAG 依赖推进任务；
- 支持等待用户输入；
- 支持等待人工确认；
- 支持暂停、恢复、取消；
- 支持同一波次步骤并行执行；
- 支持通过 `owner_id`、`subject_id`、`thread_id` 查询任务。

### 2.2 通用主体记忆模块

位置：

```text
backend/packages/harness/deerflow/subject_memory/
```

文件：

```text
backend/packages/harness/deerflow/subject_memory/README.md
backend/packages/harness/deerflow/subject_memory/__init__.py
backend/packages/harness/deerflow/subject_memory/models.py
backend/packages/harness/deerflow/subject_memory/repository.py
```

它提供的是一套通用“主体级记忆文件”能力：

- 以 `owner_id + subject_id + memory_type` 定位一份记忆；
- 保存 JSON 文档；
- 保存版本历史；
- 支持 JSON Pointer Patch；
- 支持乐观锁；
- 支持模型推断候选更新；
- 支持接受或拒绝候选更新；
- 支持 SQLAlchemy 持久化；
- 支持内存实现，便于测试和无数据库模式运行。

在保险场景中，第一个使用它的业务记忆类型是：

```text
insurance.customer-profile
```

也就是客户档案。

### 2.3 通用数据库迁移

位置：

```text
backend/packages/harness/deerflow/persistence/migrations/versions/0003_workflows_subject_memory.py
```

新增表：

```text
subject_memories
subject_memory_revisions
subject_memory_candidates
workflow_tasks
workflow_task_events
```

这些表同时服务于两个通用模块：

- `subject_memory` 使用前三张表；
- `workflows` 使用后两张表。

开发环境使用 SQLite，生产环境面向 PostgreSQL。实现上继续沿用 DeerFlow 既有 SQLAlchemy / Alembic 体系，没有引入新的数据库框架。

## 3. 通用工作流模块：代码定义

本节只介绍本次新增的 `workflows` 模块。

### 3.1 模块目标

`workflows` 模块的目标是把一个业务任务从“藏在 agent 对话状态里的过程”变成“可持久化、可恢复、可观察、可编排的任务实例”。

这直接对应此前讨论的痛点二：

> 用户不应该进入某个子 agent 后就被困在里面。任务应该可以暂停、恢复、临时跳转、并行推进，并且任务状态应该能被前端和主 agent 统一管理。

因此我们没有采用“一个任务一个子 agent”的方案，而是新增了通用任务运行时。

核心对象关系：

```text
TaskDefinition
  └── WorkflowStep
        └── SkillDefinition + SkillHandler

TaskInstance
  └── StepExecution
        └── SkillExecutionResult
```

运行链路：

```text
业务代码创建 TaskDefinition
  ↓
业务代码注册 SkillDefinition + handler
  ↓
WorkflowEngine.start() 创建 TaskInstance
  ↓
WorkflowEngine.advance() 推进可运行步骤
  ↓
TaskRepository 持久化状态
```

### 3.2 `models.py`

位置：

```text
backend/packages/harness/deerflow/workflows/models.py
```

这个文件定义工作流运行时的核心数据模型。这些模型都可以被序列化，不包含数据库 session、协程、模型客户端或运行时锁。

这样设计是为了保证任务实例可以：

- 写入数据库；
- 跨请求恢复；
- 被前端查询；
- 被 API 返回；
- 在进程重启后继续观察；
- 后续支持分布式调度。

主要定义如下。

#### 3.2.1 `TaskStatus`

任务实例的生命周期状态。

当前值：

```text
ready
running
waiting_input
waiting_confirmation
suspended
blocked
completed
cancelled
failed
```

说明：

| 状态 | 含义 |
| --- | --- |
| `ready` | 任务可被推进 |
| `running` | 任务正在运行 |
| `waiting_input` | 任务缺少用户输入 |
| `waiting_confirmation` | 任务等待人工确认 |
| `suspended` | 用户主动暂停任务 |
| `blocked` | 没有可运行步骤，但也未正常完成 |
| `completed` | 任务完成 |
| `cancelled` | 任务被取消 |
| `failed` | 任务失败 |

`completed`、`cancelled`、`failed` 是终态。

#### 3.2.2 `StepStatus`

单个步骤的生命周期状态。

当前值：

```text
pending
running
waiting_input
waiting_confirmation
completed
failed
skipped
```

任务状态由所有步骤状态汇总得出。例如：

- 任意步骤失败，任务进入 `failed`；
- 任意步骤等待输入，任务进入 `waiting_input`；
- 所有步骤完成或跳过，任务进入 `completed`。

#### 3.2.3 `SideEffectLevel`

skill 副作用等级声明。

当前值：

```text
none
read
write
irreversible
```

语义：

| 值 | 含义 |
| --- | --- |
| `none` | 无副作用，例如纯计算 |
| `read` | 只读，例如读取客户档案 |
| `write` | 会写入状态或外部系统 |
| `irreversible` | 不可逆外部动作，例如发送消息、下单、提交审批 |

当前第一阶段主要把它作为元数据保留下来。后续可以基于这个字段做 guardrail、权限控制、人工确认、审计和执行拦截。

#### 3.2.4 `InputRequest`

结构化追问请求。

字段：

| 字段 | 含义 |
| --- | --- |
| `path` | 缺失数据路径，类似 JSON Pointer |
| `prompt` | 展示给用户的追问话术 |
| `reason` | 为什么缺少该数据就不能继续 |
| `sensitive` | 前端是否应该遮蔽输入 |

它的作用是把“缺什么数据”从自然语言提示变成结构化对象。

例如保障检视缺少年收入时，skill handler 可以返回：

```json
{
  "path": "/financial/annual_income",
  "prompt": "请补充客户家庭年收入。",
  "reason": "重疾、寿险和养老测算都需要收入作为基础。",
  "sensitive": false
}
```

#### 3.2.5 `ConfirmationRequest`

人工确认请求。

字段：

| 字段 | 含义 |
| --- | --- |
| `prompt` | 需要用户确认的内容 |
| `risk` | 不确认就提交可能带来的风险 |

它用于处理高风险步骤或模型推断步骤。当前第一阶段已经有模型定义和运行时状态支持，但保险保障检视里还没有大量使用确认步骤。

#### 3.2.6 `SkillDefinition`

可执行 skill 的机器可读契约。

字段：

| 字段 | 含义 |
| --- | --- |
| `name` | skill 名称 |
| `version` | skill 版本 |
| `description` | skill 描述 |
| `input_schema` | 输入 JSON Schema |
| `output_schema` | 输出 JSON Schema |
| `allowed_tools` | 允许调用的工具名 |
| `side_effect` | 副作用等级 |
| `timeout_seconds` | 超时时间 |
| `max_attempts` | 最大尝试次数 |

注意：当前第一阶段运行时已经使用 `name` 和 `version` 查找 handler，但还没有完整执行以下治理能力：

- 工具权限拦截；
- step 级超时；
- step 级重试；
- 副作用自动确认。

这些字段是后续运行时治理的接口预留。

#### 3.2.7 `WorkflowStep`

任务 DAG 中的一个步骤节点。

字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 步骤 ID，在一个任务内必须唯一 |
| `skill` | 对应的 skill 名称 |
| `skill_version` | 对应的 skill 版本，默认 `"1"` |
| `depends_on` | 前置步骤 ID 列表 |
| `description` | 步骤说明 |

当前 `WorkflowStep` 没有显式并行字段。并行是由 DAG 依赖自动推导出来的：

- 前置依赖都完成；
- 当前步骤是 `pending` 或可重试的 `waiting_input`；
- 本轮还没尝试过；
- 那它就是 ready step；
- 同一波次的 ready steps 会并行运行。

#### 3.2.8 `TaskDefinition`

不可变、带版本的任务定义。

字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 任务名称 |
| `version` | 任务版本 |
| `description` | 任务说明 |
| `input_schema` | 初始输入 Schema |
| `steps` | `WorkflowStep` 列表 |

它在 Pydantic `model_validator` 中校验 DAG：

- step id 不能重复；
- 依赖的 step id 必须存在；
- step 不能依赖自己；
- 整体 DAG 不能有循环依赖。

这个校验是运行时稳定性的关键。错误的任务定义会在注册或创建阶段暴露，而不是让任务在执行时永久卡住。

#### 3.2.9 `StepExecution`

某个步骤在某个任务实例中的运行状态。

字段：

| 字段 | 含义 |
| --- | --- |
| `status` | 步骤状态 |
| `attempts` | 已尝试次数 |
| `output` | 步骤输出 |
| `input_requests` | 步骤发出的追问 |
| `confirmation_request` | 步骤发出的确认请求 |
| `error` | 错误信息 |
| `started_at` | 首次启动时间 |
| `updated_at` | 更新时间 |

`StepExecution` 是 `TaskInstance.steps` 的值。每个 `WorkflowStep` 会对应一个 `StepExecution`。

#### 3.2.10 `TaskInstance`

某个用户实际启动的一次任务实例。

字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 任务实例 ID |
| `task_name` | 对应的任务定义名称 |
| `definition_version` | 对应的任务定义版本 |
| `owner_id` | 所有者 ID |
| `subject_id` | 业务主体 ID，例如客户 ID |
| `thread_id` | 对话线程 ID |
| `parent_task_id` | 父任务 ID，预留给任务嵌套 |
| `status` | 任务整体状态 |
| `input_data` | 任务输入快照 |
| `output_data` | 已完成步骤的输出汇总 |
| `steps` | 每个步骤的运行状态 |
| `pending_inputs` | 当前任务层面的待补充输入 |
| `suspension_reason` | 暂停原因 |
| `error` | 错误信息 |
| `revision` | 乐观锁版本 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

`TaskInstance.from_definition()` 根据 `TaskDefinition.steps` 初始化所有步骤状态。也就是说，任务一创建，就已经有完整的状态骨架。

#### 3.2.11 `SkillExecutionResult`

skill handler 的返回值。

字段：

| 字段 | 含义 |
| --- | --- |
| `output` | skill 输出 |
| `input_requests` | 需要用户补充的输入 |
| `confirmation_request` | 需要用户确认的内容 |

handler 不直接修改任务实例，而是返回 `SkillExecutionResult`。任务状态由 `WorkflowEngine` 统一更新。

#### 3.2.12 `SkillExecutionContext`

传给 skill handler 的只读上下文快照。

字段：

| 字段 | 含义 |
| --- | --- |
| `task` | 当前任务实例快照 |
| `step` | 当前步骤定义 |
| `input_data` | 任务输入快照 |
| `dependency_outputs` | 前置步骤输出 |

这个设计限制了 handler 的副作用边界：handler 只能基于上下文计算结果，不能直接改数据库里的任务状态。

### 3.3 `registry.py`

位置：

```text
backend/packages/harness/deerflow/workflows/registry.py
```

这个文件定义任务和 skill 的注册机制。

主要定义：

| 定义 | 作用 |
| --- | --- |
| `SkillHandler` | handler 类型别名 |
| `DefinitionRegistry` | 任务定义注册表 |
| `SkillRegistry` | skill 契约和 handler 注册表 |

#### 3.3.1 `DefinitionRegistry`

职责：

- 注册 `TaskDefinition`；
- 按 `(name, version)` 查询任务定义；
- 防止重复注册；
- 支持不传版本时返回最后注册的同名定义。

设计点：

- 版本号是不透明字符串；
- 默认版本由注册顺序决定；
- 已启动的 `TaskInstance` 会保存明确的 `definition_version`；
- 因此任务恢复时不会因为新版本注册而误用另一个定义。

#### 3.3.2 `SkillRegistry`

职责：

- 注册 `SkillDefinition + handler`；
- 按 `(name, version)` 查询 skill；
- 调用同步或异步 handler；
- 强制 handler 返回 `SkillExecutionResult`。

这意味着：

- `SkillDefinition` 是契约；
- Python handler 是实际执行逻辑；
- `WorkflowStep.skill` 只保存 skill 名称；
- 运行时通过 `SkillRegistry` 找到对应 handler。

### 3.4 `engine.py`

位置：

```text
backend/packages/harness/deerflow/workflows/engine.py
```

这个文件实现通用工作流调度运行时。

主要定义：

| 定义 | 作用 |
| --- | --- |
| `WorkflowStateError` | 非法状态迁移异常 |
| `_deep_merge()` | 合并本轮输入 patch |
| `WorkflowEngine` | 核心任务运行时 |

#### 3.4.1 `WorkflowEngine.start()`

启动任务：

```text
DefinitionRegistry.get()
  ↓
TaskInstance.from_definition()
  ↓
TaskRepository.create()
```

输入包括：

- `task_name`；
- `owner_id`；
- `subject_id`；
- `thread_id`；
- `parent_task_id`；
- `input_data`；
- `definition_version`。

输出是一个已持久化的 `TaskInstance`。

#### 3.4.2 `WorkflowEngine.advance()`

推进任务。

核心流程：

```text
加载 TaskInstance
  ↓
检查终态 / 暂停态
  ↓
合并 input_patch
  ↓
处理 confirmations
  ↓
标记任务 running 并持久化
  ↓
找出 ready steps
  ↓
为每个 ready step 构造 SkillExecutionContext
  ↓
持久化 step running 状态
  ↓
asyncio.gather 并行执行 handler
  ↓
根据 SkillExecutionResult 更新 StepExecution
  ↓
汇总 TaskStatus
  ↓
持久化并返回
```

它会在以下情况下停止推进：

- 没有 ready step；
- 任务等待用户输入；
- 任务等待人工确认；
- 任务失败；
- 任务完成；
- 达到 `max_waves` 限制。

`max_waves` 是安全阀。即使 `TaskDefinition` 已经禁止静态循环，它仍能防止未来动态工作流错误导致一个请求里无限推进。

#### 3.4.3 并行执行

并行不是手工配置的，而是由依赖关系推导。

同一波次中，所有依赖已完成的步骤会被一起执行：

```text
ready steps
  ↓
contexts
  ↓
asyncio.gather(...)
```

这让业务任务自然支持：

- 多个数据收集步骤并行；
- 多个分析维度并行；
- 多份报告生成并行；
- 未来更多可并行 skill。

#### 3.4.4 等待输入

如果 handler 返回 `input_requests`：

- 当前 step 进入 `waiting_input`；
- 任务聚合所有等待输入；
- 按 `path` 去重；
- 任务进入 `waiting_input`；
- 前端或 agent 可以把这些输入请求展示给用户。

用户补充信息后，再调用 `advance(input_patch=...)`。运行时会：

- 合并新输入；
- 把等待输入的 step 恢复为 `pending`；
- 继续推进任务。

#### 3.4.5 暂停、恢复、取消

`WorkflowEngine` 提供：

- `suspend()`；
- `resume()`；
- `cancel()`。

暂停后任务进入 `suspended`。暂停态任务不能直接 `advance()`，必须先 `resume()`。

这个能力是“任务自由跳转”的核心：用户可以暂停当前任务，临时执行另一个任务，之后再恢复原任务。

### 3.5 `repository.py`

位置：

```text
backend/packages/harness/deerflow/workflows/repository.py
```

这个文件定义任务仓储契约和内存实现。

主要定义：

| 定义 | 作用 |
| --- | --- |
| `TaskConflictError` | 乐观锁冲突或任务冲突 |
| `TaskRepository` | 工作流引擎依赖的持久化接口 |
| `InMemoryTaskRepository` | 进程内任务仓储 |

`TaskRepository` 抽象方法：

```text
create(task)
get(task_id, owner_id=...)
save(task, expected_revision=...)
list(owner_id=..., subject_id=..., thread_id=...)
```

`WorkflowEngine` 只依赖这个接口，不关心底层是 SQL 还是内存。

`InMemoryTaskRepository` 使用 `asyncio.Lock` 做进程内并发保护，并用 `revision` 实现乐观锁。

### 3.6 `persistence.py`

位置：

```text
backend/packages/harness/deerflow/workflows/persistence.py
```

这个文件实现 SQLAlchemy 版本的任务仓储。

主要定义：

| 定义 | 作用 |
| --- | --- |
| `WorkflowTaskRow` | `workflow_tasks` 表 ORM 模型 |
| `WorkflowTaskEventRow` | `workflow_task_events` 表 ORM 模型 |
| `SqlTaskRepository` | SQLAlchemy 任务仓储实现 |

#### 3.6.1 `WorkflowTaskRow`

任务当前状态表。

实际字段：

```text
id
task_name
definition_version
owner_id
subject_id
thread_id
parent_task_id
status
payload_json
revision
created_at
updated_at
```

说明：

- `payload_json` 保存完整 `TaskInstance`；
- 其他列是高频查询和过滤字段；
- `owner_id` 用于隔离不同用户；
- `subject_id` 用于查询某个业务主体下的任务；
- `thread_id` 用于查询某个对话线程下的任务；
- `revision` 用于乐观锁。

#### 3.6.2 `WorkflowTaskEventRow`

任务事件表。

实际字段：

```text
id
task_id
owner_id
event_type
from_revision
to_revision
status
payload_json
summary
created_at
```

它用于追加式记录任务状态变化，为后续审计、排错、任务时间线和观测指标预留基础。

#### 3.6.3 `SqlTaskRepository`

SQLAlchemy 任务仓储实现。

关键点：

- 创建任务时写入 `WorkflowTaskRow`；
- 同时写入一条 `created` 事件；
- 保存任务时使用 `WHERE revision = expected_revision`；
- 更新成功后写入 `state_changed` 事件；
- 如果更新行数不是 1，抛出 `TaskConflictError`。

这个乐观锁机制是后续多请求、多 worker 场景下保护任务状态一致性的基础。

## 4. 通用主体记忆模块：代码定义

本节只介绍本次新增的 `subject_memory` 模块。

### 4.1 模块目标

`subject_memory` 的目标是提供一套通用的“主体记忆文件”能力。

这里的“主体”不是特指客户。它可以是：

- 保险客户；
- 企业客户；
- 项目；
- 合同；
- 案件；
- 设备；
- 任何需要长期维护结构化记忆的业务对象。

该模块解决此前讨论的痛点一：

> 每个用户的每个客户都要有自己的客户档案记忆文件。这个记忆文件要可读、可展示、可更新、可审计，并且 LLM 推断不能直接污染正式事实。

通用设计是：

```text
owner_id + subject_id + memory_type
```

在保险场景中：

```text
owner_id    = 代理人或系统用户 ID
subject_id  = customer_id
memory_type = insurance.customer-profile
```

### 4.2 `models.py`

位置：

```text
backend/packages/harness/deerflow/subject_memory/models.py
```

主要定义：

| 定义 | 类型 | 作用 |
| --- | --- | --- |
| `MemoryUpdateSource` | `StrEnum` | 记忆更新来源 |
| `CandidateStatus` | `StrEnum` | 候选更新状态 |
| `SubjectMemoryDocument` | `BaseModel` | 当前记忆文档 |
| `SubjectMemoryRevision` | `BaseModel` | 已提交版本修订 |
| `SubjectMemoryCandidate` | `BaseModel` | 待确认候选更新 |

#### 4.2.1 `MemoryUpdateSource`

当前值：

```text
user_explicit
authoritative_api
model_inference
system_derived
```

语义：

| 值 | 含义 | 是否应直接写入正式事实 |
| --- | --- | --- |
| `user_explicit` | 用户明确提供 | 可以 |
| `authoritative_api` | 权威接口返回 | 可以 |
| `model_inference` | LLM 推断 | 不应直接写入，应进入候选 |
| `system_derived` | 系统分析派生 | 一般不写入事实，应保存到任务输出 |

通用模块只定义来源类型，不强制业务策略。业务策略由领域服务决定。

保险客户档案服务使用的策略是：

- 用户明确数据直接写入；
- 权威接口数据直接写入；
- LLM 推断进入候选；
- 系统派生数据拒绝写入客户事实。

#### 4.2.2 `CandidateStatus`

当前值：

```text
pending
accepted
rejected
```

候选更新的生命周期：

```text
pending -> accepted
pending -> rejected
```

`accepted` 会修改正式文档并写入修订记录；`rejected` 不修改正式文档。

#### 4.2.3 `SubjectMemoryDocument`

当前记忆文档。

字段：

| 字段 | 含义 |
| --- | --- |
| `owner_id` | 所有者 ID |
| `subject_id` | 主体 ID |
| `memory_type` | 记忆类型 |
| `version` | 当前版本 |
| `data` | JSON 文档 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

它表示“当前正式版本”。

#### 4.2.4 `SubjectMemoryRevision`

已提交的修订记录。

字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 修订记录 ID |
| `owner_id` | 所有者 ID |
| `subject_id` | 主体 ID |
| `memory_type` | 记忆类型 |
| `from_version` | 变更前版本 |
| `to_version` | 变更后版本 |
| `patch` | 本次 patch |
| `source` | 更新来源 |
| `reason` | 更新原因 |
| `thread_id` | 关联对话线程 |
| `task_id` | 关联任务 |
| `created_at` | 创建时间 |

它用于审计“文档为什么从版本 N 变成版本 N+1”。

#### 4.2.5 `SubjectMemoryCandidate`

待确认候选更新。

字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 候选更新 ID |
| `owner_id` | 所有者 ID |
| `subject_id` | 主体 ID |
| `memory_type` | 记忆类型 |
| `base_version` | 候选基于的文档版本 |
| `patch` | 待确认 patch |
| `source` | 来源，当前只允许模型推断 |
| `status` | 候选状态 |
| `reason` | 候选原因 |
| `thread_id` | 关联对话线程 |
| `task_id` | 关联任务 |
| `created_at` | 创建时间 |
| `resolved_at` | 接受或拒绝时间 |

它用于解决“模型推断看起来合理，但不能直接变成事实”的问题。

### 4.3 `repository.py`

位置：

```text
backend/packages/harness/deerflow/subject_memory/repository.py
```

这个文件包含主体记忆的主要实现。

主要定义：

| 定义 | 作用 |
| --- | --- |
| `MemoryConflictError` | 版本冲突、并发写入或文档不可用 |
| `InvalidMemoryPatch` | patch 结构或路径非法 |
| `apply_json_pointer_patch()` | 应用 JSON Pointer Patch |
| `SubjectMemoryRow` | 当前文档表 ORM 模型 |
| `SubjectMemoryRevisionRow` | 修订记录表 ORM 模型 |
| `SubjectMemoryCandidateRow` | 候选更新表 ORM 模型 |
| `SubjectMemoryRepository` | 通用仓储接口 |
| `SqlSubjectMemoryRepository` | SQLAlchemy 仓储实现 |
| `InMemorySubjectMemoryRepository` | 内存仓储实现 |

### 4.4 JSON Pointer Patch

当前实现使用的是“路径到值”的 dict 形式：

```json
{
  "/financial/annual_income": 600000,
  "/financial/monthly_expense": 25000
}
```

它不是 RFC 6902 JSON Patch 数组。

语义是：对每个 JSON Pointer 路径执行替换或写入。

支持能力：

- 修改 dict 字段；
- 修改 list 指定下标；
- 根对象替换；
- RFC 6901 的 `~1` 和 `~0` 解码；
- 缺少父路径时报错；
- 根对象不是 JSON object 时报错。

错误由 `InvalidMemoryPatch` 表示。

### 4.5 SQLAlchemy ORM 模型

#### 4.5.1 `SubjectMemoryRow`

表：

```text
subject_memories
```

实际字段：

```text
id
owner_id
subject_id
memory_type
version
data_json
created_at
updated_at
```

约束：

```text
owner_id + subject_id + memory_type 唯一
```

这个唯一约束保证同一个所有者、同一个主体、同一种记忆类型只有一份当前文档。

#### 4.5.2 `SubjectMemoryRevisionRow`

表：

```text
subject_memory_revisions
```

实际字段：

```text
id
owner_id
subject_id
memory_type
from_version
to_version
patch_json
source
reason
thread_id
task_id
created_at
```

它是追加式审计记录。

#### 4.5.3 `SubjectMemoryCandidateRow`

表：

```text
subject_memory_candidates
```

实际字段：

```text
id
owner_id
subject_id
memory_type
base_version
patch_json
source
status
reason
thread_id
task_id
created_at
resolved_at
```

它保存尚未进入正式文档的候选更新。

### 4.6 `SubjectMemoryRepository`

通用仓储接口。

方法：

```text
create(...)
get(...)
apply_patch(...)
list_revisions(...)
propose_candidate(...)
list_candidates(...)
accept_candidate(...)
reject_candidate(...)
```

业务服务只依赖这个接口，不依赖 SQLAlchemy。

### 4.7 `SqlSubjectMemoryRepository`

SQLAlchemy 实现。

关键行为：

- `create()` 创建当前文档，并写入初始 revision；
- `get()` 读取当前文档；
- `apply_patch()` 使用 `expected_version` 做乐观锁；
- `list_revisions()` 返回修订历史；
- `propose_candidate()` 创建模型推断候选；
- `accept_candidate()` 接受候选并写入正式文档；
- `reject_candidate()` 拒绝候选，不修改正式文档。

`propose_candidate()` 当前只允许：

```text
MemoryUpdateSource.MODEL_INFERENCE
```

并且会在创建候选时先应用一次 patch 校验，避免用户批准后才发现路径不可用。

`accept_candidate()` 会检查当前文档版本是否仍等于 `base_version`。如果文档已经被其他更新推进，就抛出 `MemoryConflictError`。

### 4.8 `InMemorySubjectMemoryRepository`

内存实现。

用途：

- 单元测试；
- 没有数据库 session factory 的开发模式；
- 快速验证业务逻辑。

它和 SQL 实现保持同样的接口语义，并使用 `asyncio.Lock` 做进程内并发保护。

## 5. 通用持久化与迁移

迁移文件：

```text
backend/packages/harness/deerflow/persistence/migrations/versions/0003_workflows_subject_memory.py
```

新增五张表：

| 表 | 所属通用模块 | 用途 |
| --- | --- | --- |
| `subject_memories` | `subject_memory` | 保存主体记忆当前文档 |
| `subject_memory_revisions` | `subject_memory` | 保存主体记忆修订历史 |
| `subject_memory_candidates` | `subject_memory` | 保存待确认候选更新 |
| `workflow_tasks` | `workflows` | 保存任务实例当前状态 |
| `workflow_task_events` | `workflows` | 保存任务事件 |

这次新增的持久化实现遵循两个原则：

1. 当前状态物化，便于读取和查询；
2. 关键变更追加记录，便于审计和排错。

对于任务：

- `workflow_tasks.payload_json` 保存完整 `TaskInstance`；
- `workflow_task_events.payload_json` 保存状态变化时的快照。

对于主体记忆：

- `subject_memories.data_json` 保存当前文档；
- `subject_memory_revisions.patch_json` 保存每次提交的 patch；
- `subject_memory_candidates.patch_json` 保存待确认 patch。

## 6. 通用模块如何解决三个痛点

### 6.1 痛点一：客户档案记忆

虽然痛点来自保险客户档案，但解决方案是通用 `subject_memory`。

通用方案：

```text
owner_id + subject_id + memory_type
```

解决的问题：

- 每个用户的每个主体都有独立记忆；
- 同一主体可以有多种记忆；
- JSON 文档便于领域 schema 演进；
- revision 记录让更新可审计；
- candidate 机制防止 LLM 推断直接污染正式事实；
- expected version 防止并发覆盖。

保险领域只是在这个通用机制上约束：

```text
subject_id  = customer_id
memory_type = insurance.customer-profile
```

### 6.2 痛点二：任务自由跳转、嵌套和并行

解决方案是通用 `workflows`。

核心不是让用户进入某个子 agent，而是创建一个可持久化的 `TaskInstance`。

任务实例具备：

- 状态；
- 输入；
- 输出；
- 步骤状态；
- 待补充输入；
- 暂停原因；
- 父任务 ID；
- 乐观锁版本。

因此可以支持：

- 用户暂停当前任务；
- 用户恢复之前任务；
- 用户取消任务；
- 用户查看任务状态；
- 用户在等待输入时切去做别的任务；
- 同一个客户下有多个任务并行存在；
- 后续通过 `parent_task_id` 扩展父子任务关系。

并行能力来自 DAG：

```text
depends_on 满足的步骤 -> 同一波次并行执行
```

不是来自某个保险专用流程。

### 6.3 痛点三：子任务、工具和 skill 的拆解

解决方案是通用 `TaskDefinition + WorkflowStep + SkillDefinition + SkillHandler`。

推荐分层：

```text
TaskDefinition = 一个业务环节
WorkflowStep   = 业务环节中的一个步骤
SkillDefinition = 步骤可调用能力的契约
SkillHandler   = 具体 Python 执行逻辑
Tool           = 给 agent 暴露的外部动作入口
```

这个分层让任务具备：

- 可编排；
- 可测试；
- 可复用；
- 可追踪；
- 可并行；
- 可治理；
- 可逐步增加权限、超时、重试、审计能力。

## 7. 垂直领域如何使用这些通用模块

如果后续要在别的领域复用这套通用能力，推荐流程如下。

### 7.1 使用 `subject_memory`

领域侧需要做：

1. 定义自己的业务 schema；
2. 选择 `memory_type`；
3. 用 `owner_id + subject_id + memory_type` 定位记忆；
4. 在领域 service 中调用 `SubjectMemoryRepository`；
5. 对 JSON 文档做领域 schema 校验；
6. 决定不同 `MemoryUpdateSource` 的处理策略；
7. 对模型推断走 candidate 流程。

保险领域示例：

```text
CustomerProfileService
  ↓
SubjectMemoryRepository
  ↓
insurance.customer-profile
```

### 7.2 使用 `workflows`

领域侧需要做：

1. 定义业务任务名称和版本；
2. 拆分 `WorkflowStep`；
3. 为每个步骤定义 `SkillDefinition`；
4. 为每个 skill 编写 handler；
5. 注册到 `SkillRegistry`；
6. 构造 `TaskDefinition`；
7. 创建 `WorkflowEngine`；
8. 调用 `start()` 和 `advance()`。

保险领域示例：

```text
build_coverage_review_task_definition()
build_coverage_review_skill_registry()
InsuranceService._engine()
```

### 7.3 使用 SQL / InMemory Repository

领域服务不应该直接依赖 SQLAlchemy。

推荐方式：

```text
有数据库 session factory
  -> SqlTaskRepository + SqlSubjectMemoryRepository

无数据库 session factory
  -> InMemoryTaskRepository + InMemorySubjectMemoryRepository
```

保险领域当前在：

```text
backend/app/insurance/runtime.py
```

完成这个选择。

## 8. 保险产品销售具体实现

从本节开始，介绍保险产品销售这个具体落地实现。

注意：以下模块是业务层代码，不是通用 harness 能力。它们依赖前文介绍的 `workflows` 和 `subject_memory`。

### 8.1 保险领域模块

位置：

```text
backend/app/insurance/
```

文件：

```text
backend/app/insurance/README.md
backend/app/insurance/__init__.py
backend/app/insurance/adapters.py
backend/app/insurance/mock_data.py
backend/app/insurance/models.py
backend/app/insurance/profile.py
backend/app/insurance/router.py
backend/app/insurance/runtime.py
backend/app/insurance/service.py
backend/app/insurance/tools.py
```

职责：

- 定义保险客户档案模型；
- 将客户档案映射到通用主体记忆；
- 提供客户档案服务；
- 提供外部数据 Adapter 抽象；
- 提供 Mock Adapter；
- 装配通用 workflow runtime；
- 暴露 REST API；
- 暴露 Agent Tool。

### 8.2 `runtime.py`

位置：

```text
backend/app/insurance/runtime.py
```

职责：

- 在 Gateway 初始化后创建保险服务；
- 根据是否有 SQLAlchemy session factory 选择仓储实现；
- 保存进程级 `InsuranceService`；
- 给 API 和 Tool 共用同一个 service 实例。

逻辑：

```text
session_factory is None
  -> InMemoryTaskRepository
  -> InMemorySubjectMemoryRepository

session_factory exists
  -> SqlTaskRepository
  -> SqlSubjectMemoryRepository
```

### 8.3 `service.py`

位置：

```text
backend/app/insurance/service.py
```

`InsuranceService` 是保险业务应用服务。

职责：

- 创建 `CustomerProfileService`；
- 持有 `TaskRepository`；
- 持有 `CustomerDataAdapter`；
- 创建 `WorkflowEngine`；
- 注册保障检视任务定义；
- 注册保障检视 skill registry；
- 启动保障检视；
- 推进保障检视；
- 暂停、恢复、取消任务；
- 更新客户档案后继续推进任务。

关键方法：

```text
start_coverage_review(...)
advance_task(...)
get_task(...)
list_tasks(...)
suspend_task(...)
resume_task(...)
cancel_task(...)
update_profile_and_advance(...)
```

### 8.4 `profile.py`

位置：

```text
backend/app/insurance/profile.py
```

`CustomerProfileService` 把通用主体记忆约束成保险客户档案。

关键规则：

- 固定 `memory_type = "insurance.customer-profile"`；
- 用 `CustomerProfile` Pydantic 模型校验文档；
- 禁止修改 `customer_id`；
- 自动维护 `field_evidence`；
- `USER_EXPLICIT` 直接写正式档案；
- `AUTHORITATIVE_API` 直接写正式档案；
- `MODEL_INFERENCE` 进入候选更新；
- `SYSTEM_DERIVED` 不允许写入客户事实。

这就是“客户事实不能被模型推断直接污染”的业务层落实。

### 8.5 `models.py`

位置：

```text
backend/app/insurance/models.py
```

定义保险客户档案 schema。

主要对象：

```text
CustomerProfile
FamilyMember
FinancialProfile
InsurancePolicy
Liability
EducationPlan
RetirementPlan
MedicalPreference
FieldEvidence
```

客户档案顶层字段包括：

```text
customer_id
display_name
city
members
economic_roles
financial
policies
retirement_plan
education_plans
medical_preference
notes
field_evidence
```

在保险业务语义中：

- `None` 表示未知；
- `[]` 表示已确认没有；
- `field_evidence` 记录字段来源；
- 客户事实缺失时不能用默认值编造。

### 8.6 外部数据 Adapter

位置：

```text
backend/app/insurance/adapters.py
backend/app/insurance/mock_data.py
```

第一阶段只提供 Adapter 抽象和 Mock。

目的：

- 业务代码不直接绑定外部 HTTP 接口；
- 后续可以替换成 CRM、保单系统、客户系统等；
- 权威接口返回的数据可以作为 `AUTHORITATIVE_API` 来源更新客户档案。

当前 Mock Adapter 主要用于打通框架，没有真实业务数据。

## 9. 保障检视具体实现

保障检视是保险产品销售场景的第一个业务任务。

位置：

```text
backend/app/insurance/coverage_review/
```

文件：

```text
backend/app/insurance/coverage_review/README.md
backend/app/insurance/coverage_review/__init__.py
backend/app/insurance/coverage_review/models.py
backend/app/insurance/coverage_review/parameters.py
backend/app/insurance/coverage_review/calculator.py
backend/app/insurance/coverage_review/reports.py
backend/app/insurance/coverage_review/workflow.py
```

### 9.1 `workflow.py`

这个文件是保障检视对通用 `workflows` 模块的具体使用。

它定义：

- 保障检视任务名；
- 保障检视任务版本；
- skill handler；
- `SkillDefinition` 注册；
- `WorkflowStep` DAG；
- `TaskDefinition`。

当前任务名：

```text
insurance-coverage-review
```

当前版本：

```text
1
```

### 9.2 保障检视 DAG

当前 DAG：

```text
collect-general      ┐
collect-financial    ├─> 六维分析 ─┬─> customer-report
collect-policies     ┘             └─> internal-report
```

数据收集步骤：

```text
collect-general
collect-financial
collect-policies
```

六维分析步骤：

```text
analyze-life
analyze-critical-illness
analyze-medical
analyze-accident
analyze-retirement
analyze-emergency-reserve
```

报告步骤：

```text
customer-report
internal-report
```

因为六维分析都依赖三个数据收集步骤，所以三个数据收集完成后，六维分析可以同一波次并行执行。

### 9.3 保障检视 skill

当前 skill 包括：

| 步骤 | skill | 作用 |
| --- | --- | --- |
| `collect-general` | `insurance-collect-general` | 读取通用客户与家庭事实 |
| `collect-financial` | `insurance-collect-financial` | 读取财务事实 |
| `collect-policies` | `insurance-collect-policies` | 读取已有保障 |
| `analyze-life` | `insurance-analyze-life` | 分析寿险缺口 |
| `analyze-critical-illness` | `insurance-analyze-critical-illness` | 分析重疾缺口 |
| `analyze-medical` | `insurance-analyze-medical` | 分析医疗责任缺口 |
| `analyze-accident` | `insurance-analyze-accident` | 分析意外保障缺口 |
| `analyze-retirement` | `insurance-analyze-retirement` | 分析养老现金流缺口 |
| `analyze-emergency-reserve` | `insurance-analyze-emergency-reserve` | 分析应急储备缺口 |
| `customer-report` | `insurance-generate-customer-report` | 生成客户可见版报告 |
| `internal-report` | `insurance-generate-internal-report` | 生成内部诊断版报告 |

### 9.4 `parameters.py`

定义保障检视 DRAFT 参数。

当前参数版本：

```text
draft-2026-07-07
```

当前状态：

```text
draft
```

这些参数包括：

- 寿险收入替代年限；
- 重疾治疗费；
- 重疾收入损失补偿年数；
- 养老替代率；
- 应急储备月数；
- 意外身故/伤残倍数；
- 意外医疗额度；
- 医疗责任目标组合。

它们不是正式行业口径，只是首版测算参数。

### 9.5 `calculator.py`

实现六维保障缺口计算：

```text
life
critical_illness
medical
accident
retirement
emergency_reserve
```

每个维度输出三个场景：

```text
保守
基准
充分
```

客户事实缺失时不使用默认值。缺失事实会形成 input request 或在报告中明确标记。

### 9.6 `reports.py`

生成两套独立报告：

- 客户可见版；
- 内部诊断版。

两者不是同一个 schema 换标题，而是独立结构。

原因：

- 客户版面向客户，语言要克制；
- 内部版面向代理人，可以包含诊断、缺失信息、话术和风险提示；
- 内部判断不应该误发给客户；
- 后续权限控制和合规审核更清晰。

### 9.7 `models.py`

定义保障检视结果和报告模型。

主要对象：

```text
ScenarioAssessment
DimensionAssessment
CoverageReviewResult
CustomerCoverageReport
InternalCoverageReport
```

## 10. API 与 Agent Tool 接入

这部分是保险业务对外入口，不是通用模块。

### 10.1 REST API

路由文件：

```text
backend/app/insurance/router.py
```

挂载前缀：

```text
/api/insurance
```

主要接口：

```text
POST /customers
GET  /customers/{customer_id}
PATCH /customers/{customer_id}
GET  /customers/{customer_id}/candidates
POST /candidates/{candidate_id}/accept
POST /candidates/{candidate_id}/reject
POST /customers/{customer_id}/coverage-reviews
GET  /tasks/{task_id}
GET  /customers/{customer_id}/tasks
POST /tasks/{task_id}/advance
POST /tasks/{task_id}/suspend
POST /tasks/{task_id}/resume
POST /tasks/{task_id}/cancel
```

重要规则：

- `owner_id` 从服务端鉴权上下文解析；
- 请求体中的 owner 不可信；
- 客户档案按 owner/customer 隔离；
- 任务同样按 owner/customer 隔离。

### 10.2 Agent Tool

工具文件：

```text
backend/app/insurance/tools.py
```

当前工具：

```text
insurance_get_customer_profile
insurance_update_customer_profile
insurance_coverage_review
```

职责：

- `insurance_get_customer_profile`：读取客户档案；
- `insurance_update_customer_profile`：更新客户档案或创建候选更新；
- `insurance_coverage_review`：启动或推进保障检视。

Tool 的边界：

- Tool 是给 agent 调用的动作入口；
- Workflow 负责任务编排；
- Skill 负责子任务执行；
- Repository 负责持久化；
- 计算和报告逻辑放在领域模块里。

## 11. 本次新增测试文件

测试文件：

```text
backend/tests/test_workflow_runtime.py
backend/tests/test_subject_memory_repository.py
backend/tests/test_insurance_profile_service.py
backend/tests/test_insurance_coverage_review.py
backend/tests/test_insurance_coverage_workflow.py
```

覆盖方向：

- 通用工作流运行时；
- 通用主体记忆仓储；
- 保险客户档案服务；
- 保障检视计算；
- 保障检视 workflow。

此前按要求没有执行完整测试。如果进入提交或合并阶段，建议执行：

```bash
cd backend
make test
```

## 12. 当前实现限制

### 12.1 通用 workflow 限制

当前已经具备任务运行时核心能力，但还没有完整实现：

- step 级超时执行；
- step 级重试执行；
- allowed tools 权限拦截；
- side effect 自动确认；
- 分布式锁；
- 幂等请求 ID；
- 父子任务关系的完整查询与展示；
- 任务事件时间线 API；
- 任务 DAG 可视化。

### 12.2 通用 subject_memory 限制

当前已经具备主体记忆核心能力，但还需要增强：

- 字段级权限；
- 字段级脱敏；
- schema version；
- 候选更新 diff 展示；
- revision 回滚；
- 记忆归档与删除；
- 大文档分片或索引；
- PostgreSQL JSONB 查询优化。

### 12.3 保险业务限制

保障检视当前是后端 MVP，不是完整生产业务模型。

还缺少：

- 完整成员级分析；
- 产品库；
- 方案推荐；
- 保费预算约束；
- 利益测算；
- 销售沟通任务；
- 真实外部数据接口；
- 生产级合规审核；
- 前端任务工作台；
- 报告可视化。

## 13. 后续演进建议

优先级建议如下。

### 13.1 先加固通用模块

建议优先补齐：

- `WorkflowEngine` 的超时和重试；
- skill 权限与副作用治理；
- 任务幂等；
- 任务事件查询；
- `subject_memory` candidate diff；
- 字段级 evidence 自动维护；
- schema version；
- 敏感字段脱敏。

这些能力一旦补齐，不只服务保险产品销售，也可以服务后续所有垂直领域。

### 13.2 再加深保障检视

建议补齐：

- 成员级保障缺口；
- 更严格的必需事实校验；
- 六维计算参数治理；
- 真实 Mock 样例；
- 客户版报告可视化；
- 内部版话术增强；
- 报告快照和版本绑定。

### 13.3 最后扩展更多保险任务

在通用模块稳定后，再扩展：

- 需求分析；
- 方案推荐；
- 销售沟通；
- 利益测算。

这些新任务应该复用同一套：

```text
TaskDefinition
WorkflowStep
SkillDefinition
TaskInstance
SubjectMemoryDocument
```

而不是重新发明一套任务和记忆机制。

## 14. 本地开发提示

全栈启动：

```bash
make dev
```

只启动后端：

```bash
cd backend
make dev
```

后端静态检查：

```bash
cd backend
make lint
make format
```

后端测试：

```bash
cd backend
make test
```

## 15. 文档索引

通用模块 README：

```text
backend/packages/harness/deerflow/workflows/README.md
backend/packages/harness/deerflow/subject_memory/README.md
```

保险业务模块 README：

```text
backend/app/insurance/README.md
backend/app/insurance/coverage_review/README.md
```

根级产品与技术文档：

```text
保险产品销售的产品形态README.md
保险产品销售的技术模块README.md
```

## 16. 总结

本次技术实现的重点不是单独做一个“保障检视功能”，而是先为垂直领域 agent harness 补上两类通用基础设施：

1. `workflows`：可持久化、可恢复、可并行、可暂停的任务运行时；
2. `subject_memory`：按所有者和业务主体隔离、支持版本和候选确认的结构化记忆文件。

保险产品销售场景在这两类通用基础设施之上实现了：

- 客户档案；
- 保障检视任务；
- 六维测算；
- 两套报告；
- REST API；
- Agent Tool。

后续扩展需求分析、方案推荐、销售沟通和利益测算时，应优先复用这次新增的通用模块，而不是继续把流程写死在单个子 agent 或单段 prompt 里。

