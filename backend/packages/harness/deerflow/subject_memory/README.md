# 版本化主体记忆

DeerFlow 自带的长期记忆描述的是使用 Agent 的用户。业务应用还需要保存另一个业务主体的权威记忆，例如一个保险代理人会管理多个客户家庭。本模块以 `(owner_id, subject_id, memory_type)` 作为组合身份保存这类文档。

## 为什么不复用 `agents/memory`

通用长期记忆更新器采用尽力而为的 LLM 提取方式。业务事实则需要 Schema 校验、来源记录、乐观并发控制和审计历史。因此，主体记忆 Repository 不负责从文本中提取事实：应用必须先校验领域模型，再提交明确的 JSON Pointer Patch。

## 数据表

- `subject_memories`：保存当前 JSON 快照和单调递增版本号。
- `subject_memory_revisions`：保存已提交 Patch 的追加式历史。
- `subject_memory_candidates`：保存等待人工确认的推断变更。

用户明确陈述和权威服务返回的数据可以由应用提交。模型推断必须调用 `propose_candidate()`；接受候选项时，会执行与普通更新相同的乐观锁检查并写入修订历史。派生分析结果通常应保存在任务输出中，不应反写为客户事实。

## 隔离与并发

所有查询都包含 `owner_id`。即使知道文档或候选项 ID，调用方也无法读取或接受其他所有者的数据。`expected_version` 实现比较并交换；多个任务并发更新时会抛出 `MemoryConflictError`，而不会静默覆盖较新的客户数据。

Patch 使用 RFC 6901 JSON Pointer 路径，例如 `/financial/annual_income`。Repository 支持字典键和列表索引，并拒绝穿越不存在的父节点。领域服务必须在提交 Patch 前重新校验完整文档。

