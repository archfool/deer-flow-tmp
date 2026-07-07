# 持久化工作流运行时

本模块为基于 DeerFlow 构建的对话式应用提供领域无关的工作流能力。它刻意不接管对话循环：每一轮用户消息仍然先进入主 Agent，由主 Agent 请求本运行时创建、推进、暂停、恢复或取消持久化任务实例。这样的边界可以避免用户被困在一个长期运行的子 Agent 中。

## 核心概念

- `TaskDefinition`：由多个 `WorkflowStep` 组成的不可变、带版本 DAG。
- `SkillDefinition`：单个可复用能力的机器可读契约。`SKILL.md` 仍可保存模型指令，但超时、副作用、输入和输出元数据由该契约管理。
- `TaskInstance`：可持久化的运行状态，包括输入、各步骤结果、待回答问题、父任务关联和乐观锁版本。
- `WorkflowEngine`：并行执行所有已就绪的同级步骤，在输入或确认屏障处停止，并在每个执行波次后持久化。
- `TaskRepository`：屏蔽具体存储后端。内存实现用于测试和 `database.backend=memory`；SQL 实现位于 `deerflow.workflows.persistence`。

## 中断语义

`suspend()` 是可恢复的用户操作，会完整保留步骤输出和待回答问题；`cancel()` 是终态操作。父任务 ID 用于表达继续执行或依赖关系，但不会强制界面采用单一调用栈：同一时间可以存在多个活动任务实例，当前对话焦点由调用方决定。

引擎每次只推进到下一个稳定屏障。Skill 可以返回 `InputRequest` 或 `ConfirmationRequest`，当前 Agent Run 随即结束，后续用户消息再调用 `advance()`。等待用户时不会保留仍在运行的 Python 协程。

## 安全性与确定性

- 定义加载时会拒绝未知依赖和循环依赖。
- 任务读写必须携带 `owner_id`；仅知道任务 ID 不代表拥有访问权限。
- Repository 保存采用乐观锁版本，防止并发对话轮次静默覆盖状态。
- 相互独立的步骤可以并行，但副作用等级必须在 `SkillDefinition` 中声明，并由应用 Guardrail 执行限制。
- 不支持 Skill 隐式递归调用。复用关系必须表示为显式 DAG 或子任务，确保依赖和审计事件保持可见。

## 最小示例

```python
skills = SkillRegistry()
skills.register(definition, handler)
engine = WorkflowEngine(
    definitions=[task_definition],
    skills=skills,
    repository=repository,
)
task = await engine.start(
    task_name="coverage-review",
    owner_id=user_id,
    subject_id=customer_id,
)
task = await engine.advance(task.id, owner_id=user_id)
```

应用层应通过领域服务暴露任务状态，不应把 Repository 对象直接交给 LLM Tool。

