# 企业级保障检视算法模块

本模块依据《保障检视模块项目技术设计 v2》和《保障检视模块技术开发方案 v1》实现。它是 DeerFlow 上的持久化算法工作流，不是独立前端，也不让大模型自由决定数字、理由码或保障动作。

## 核心口径

- 固定八维顺序：`D1` 疾病、`D2` 医疗、`D3` 伤残、`D5` 护理、`D4` 身故、`A1` 财富、`B1` 养老、`C1` 传承。
- `D3` 只表示伤残；历史“意外保障”只有在工具同时声明 `disability-*` 语义版本时才可迁移，否则阻断内核。
- 应急流动性是辅助财务指标 `EMERGENCY_LIQUIDITY_ALERT`，不是第九维，不得生成保险动作。
- 运行规则包含 8 个维度、20 个标准理由码和 29 个触发场景，编译阶段会校验闭环。
- 八维数字只来自精确测算 Tool。Agent 和 Skill 不内置第二套业务测算公式。
- 3.1 事实、5.2 解释和 5.3 动作是同一个不可变 `DiagnosisKernel` 的三个独立投影。
- 5.1、5.2、5.3 和面谈支持由 `CoverageReviewNarrativeHarness` 生成结构化语言资产；模型不得重算数字或改写封闭动作，输出越界时自动降级为规则约束文案。
- 对客 HTML 必须通过代理人复核闸门，由固定模板和受控 ViewModel 渲染。
- 理由码、框架码、规则哈希和原始投影只进入服务端 `audit_manifest`，不进入代理人复核视图、对内 Markdown 或对客 HTML。

## 工作流

真实业务顺序是 `1 → 2 → 3 → 5 → 4 → 6`，步骤 7 作为全程语气与合规护栏。

```text
客户中心 ─┐
客户档案 ─┼─> 证据闸门/追问 ─> 精确测算 ─> 诊断内核
中保信报告 ─┤                                ├─> 3.1 事实
触发场景 ─┘                                ├─> 5.2 解释
                                                 ├─> 5.3 动作
                                                 └─> 5.1/5.4-5.6
                                                          │
                                                 一致性闸门
                                                          │
                                                     对内报告
                                                          │
                                         代理人复核：批准/改事实/改表达/驳回
                                                          │
                                               ViewModel + HTML 模板
```

客户事实、触发场景或叙事变更时，`WorkflowEngine.invalidate_steps()` 只失效当前节点及其下游。事实变更会重算内核；纯叙事变更不会重算数字。

代理人复核卡展示客户身份、触发语境、八维数字、辅助财务观察、保留项、纠错项和告警。自然语言事实修改先提取为白名单 Patch，再重新固化内核；称呼、解释密度或话术方向只重跑语言和报告节点。批准前 `customer-copy`、ViewModel 和 HTML 节点保持未执行。

## 目录

- `models.py`：证据、测算、内核、投影、复核和报告的 Pydantic 契约。
- `rule_assets/source/`：可评审的 canonical JSON 规则源。
- `rule_assets/bundles/`：带 SHA-256 的已编译运行包。
- `rules.py`：规则编译、数量校验和跨表闭环校验。
- `tools.py`：四类业务 Tool 协议、稳定 Mock 和历史响应 Normalizer。
- `evidence.py`：证据汇聚、中保信抽取、冲突识别、触发合成和追问。
- `kernel.py`：精确测算请求与确定性诊断内核。
- `projections.py`：3.1、5.2、5.3 独立投影和一致性闸门。
- `display.py`：对内与对客产物共用的中文金额、责任和时间格式。
- `knowledge.py` 与 `knowledge/skill_registry.yaml`：书籍 Skill 的权限登记、场景路由和受控最小片段加载。
- `narrative.py`：模型结构化输出、事实引用校验、内部码隔离和离线降级文案。
- `reports.py`：对内 Markdown、复核包、对客 ViewModel 和 HTML 校验。
- `workflow.py`：23 步 DeerFlow durable workflow 及 Executable Skill 注册。
- `templates/`：对客 HTML/CSS 固定模板。

## 本地运行

在 `backend/` 目录执行：

```bash
PYTHONPATH=.:packages/harness python scripts/run_coverage_review_demo.py \
  --trigger A2 \
  --output-dir .deer-flow/coverage-review-demo
```

输出包含 `internal_report.md`、`customer_report.html`、`manifest.json` 和 `task_snapshot.json`。演示使用内存 Repository 和四个 Mock Tool，不需要内网服务。

Gateway 运行时默认固定使用 `minimax-m2.5-main` 的 `ModelNarrativeHarness`，也可通过 `COVERAGE_REVIEW_MODEL` 显式指定已配置模型。MiniMax-M2.5 通过唯一 Pydantic Schema 工具调用返回语言资产；自由文本、多个工具调用、工具名错误或参数非法均不进入工作流状态。模型异常或输出越界时 Harness 会生成可识别的 `fallback` 结果，但正式对内/对外报告节点会拒绝该结果并进入工作流重试或失败状态，不会把降级文案静默发布。可通过 `COVERAGE_REVIEW_KNOWLEDGE_ROOT` 指定书籍 Skills 根目录；未配置知识目录时不加载书籍正文。

## 测试与规则编译

```bash
PYTHONPATH=.:packages/harness python -m pytest \
  tests/test_insurance_coverage_review.py \
  tests/test_insurance_coverage_workflow.py \
  tests/test_insurance_intake.py -q

PYTHONPATH=.:packages/harness python -c \
  "from app.insurance.coverage_review import compile_rule_bundle, write_compiled_bundle; write_compiled_bundle(compile_rule_bundle())"
```

运行时只加载 `rule_assets/bundles/coverage_review.v2.1.json`，并重算 `bundle_hash` 检测篡改；不在业务请求中现场编译源文件。Bundle 记录编译器版本、全部源哈希、20 码集合哈希、决策覆盖哈希和编译期金标准检查。

## 接入真实内网服务

分别实现 `CustomerCenterTool`、`CustomerProfileTool`、`ZhongbaoxinReportTool` 和 `PreciseGapCalculatorTool`，组成 `CoverageReviewTools` 后注入 `InsuranceService`。超时、重试、鉴权、熔断和原始响应审计应封装在 Adapter 实现中；上游异常字段只能在 Normalizer 层迁移，不得污染诊断内核。

书籍 Skills 不复制到业务代码中。部署层把独立 Skills Root 传给 `load_knowledge_excerpt()`；注册表强制其权限为 `COMMUNICATION_KNOWLEDGE`，禁止用于客户事实、测算规则、保单条款或法律合规结论。
