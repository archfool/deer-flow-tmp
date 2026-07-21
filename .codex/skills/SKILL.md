---
name: python-dev-standards
description: Python 项目开发规范与编码准则。只要涉及 Python 代码的编写、修改或调试，无论用户是否提及"规范"，都必须遵循此 skill。
---

# Python 开发规范 Skill

面向 AI-Coding 的 Python 项目开发行为准则。融合了经过验证的工程实践，旨在减少 LLM 编码中的常见错误，同时确保代码的可读性、可维护性和一致性。

**核心原则：宁可谨慎，不要冒进。对于极其简单的任务，可酌情灵活处理。**

---

## 1. 编码前先思考

**不要假设，不要隐藏困惑，主动暴露权衡取舍。**

在动手写代码之前：

- 明确陈述你的假设。如果不确定，先问。
- 如果存在多种理解方式，逐一列出——不要悄悄选一个。
- 如果有更简单的方案，说出来。必要时大胆反驳。
- 如果有任何不清楚的地方，停下来，指出困惑所在，然后提问。

**执行多步骤任务时，先列出简要计划：**

```
1. [步骤] → 验证方式: [检查点]
2. [步骤] → 验证方式: [检查点]
3. [步骤] → 验证方式: [检查点]
```

---

## 2. 简洁至上

**用最少的代码解决问题，不写投机性代码。**

- 不实现需求之外的功能。
- 单次使用的代码不做抽象封装。
- 不添加未被要求的"灵活性"或"可配置性"。
- 不为不可能出现的场景做错误处理。
- 如果 200 行能缩到 50 行，就重写。

自问：「一个资深工程师会不会觉得这写复杂了？」如果会，简化。

---

## 3. 精准修改

**只动必须动的地方，只清理自己弄脏的东西。**

修改已有代码时：

- 不要"顺手改进"旁边的代码、注释或格式。
- 不要重构没坏的东西。
- 匹配已有代码风格，即使你会用不同方式写。
- 发现无关的死代码，可以提一嘴——但别删。

当你的修改产生了孤立引用时：

- 移除**因你的修改**而变得无用的 import / 变量 / 函数。
- 不要移除修改前就已存在的死代码，除非用户明确要求。

**检验标准：每一行改动都能直接追溯到用户的需求。**

---

## 4. 目标驱动执行

**定义成功标准，循环验证直到通过。**

将模糊任务转化为可验证的目标：

- 「加个校验」→「为非法输入写测试，然后让测试通过」
- 「修这个 bug」→「写一个复现测试，然后让它通过」
- 「重构 X」→「确保重构前后测试全部通过」

明确的成功标准让你能独立迭代。模糊的标准（「让它跑起来」）只会导致反复确认。

---

## 5. Python 函数与参数的中文注释（必须遵守）

所有 Python 函数和类方法**必须**包含中文 docstring 和参数注释。这是团队协作和可维护性的硬性要求。

### 5.1 函数 docstring 规范

使用 Google 风格 docstring，内容全部使用中文：

```python
def calculate_total_price(
    unit_price: float,
    quantity: int,
    discount: float = 0.0
) -> float:
    """计算商品总价。

    根据单价、数量和折扣比例计算最终价格。
    折扣为 0~1 之间的小数，例如 0.15 表示八五折。

    Args:
        unit_price: 商品单价（单位：元）。
        quantity: 购买数量，必须为正整数。
        discount: 折扣比例，默认为 0（即不打折）。取值范围 [0, 1]。

    Returns:
        折后总价（单位：元）。

    Raises:
        ValueError: 当 quantity 小于等于 0 或 discount 不在合法范围时抛出。
    """
```

### 5.2 类的注释规范

```python
class OrderProcessor:
    """订单处理器。

    负责订单的创建、校验和状态流转。
    支持同步和异步两种处理模式。

    Attributes:
        db_session: 数据库会话实例。
        max_retries: 最大重试次数，默认为 3。
    """

    def __init__(self, db_session: Session, max_retries: int = 3) -> None:
        """初始化订单处理器。

        Args:
            db_session: 数据库会话实例，用于持久化操作。
            max_retries: 处理失败时的最大重试次数。
        """
```

### 5.3 关键规则

- 所有公开函数（public function）和类**必须**有中文 docstring。
- 内部辅助函数（以 `_` 开头）也建议写 docstring，至少写一行功能描述。
- `Args`、`Returns`、`Raises` 等段落标题保留英文（Google 风格），描述内容使用中文。
- 行内注释（`#`）：解释**为什么**这么做，而非**做了什么**。用中文书写。
- 复杂的业务逻辑段落前加一行中文注释说明意图。
- 类型注解（type hints）是补充，不能替代 docstring 中的参数说明。

---

## 6. 统一日志规范（必须遵守）

项目中**禁止**使用 `print()` 进行调试或运行时输出。所有日志输出必须通过统一的日志模块。

### 6.1 标准日志配置（入口处调用一次）

在项目入口模块（如 `main.py`）或专用 `logger.py` 中**只配置一次**日志：

```python
# logger.py
"""项目统一日志配置模块。"""

import logging
import sys
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None
) -> None:
    """配置项目根日志记录器。

    在项目入口处调用一次即可，所有子模块通过 logging.getLogger(__name__)
    自动继承此配置。不要在各业务模块中重复调用。

    Args:
        level: 日志级别，可选 DEBUG / INFO / WARNING / ERROR / CRITICAL。
        log_file: 日志文件路径。为 None 时仅输出到控制台。
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加 handler（例如被意外调用两次）
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件输出（可选）
    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
```

在入口处调用：

```python
# main.py
from logger import setup_logging

setup_logging(level="INFO", log_file="logs/app.log")
```

### 6.2 在业务模块中获取日志（不要调用 setup）

各模块只需用标准库获取 logger，**不要** import 或调用 `setup_logging`：

```python
import logging

# 模块顶部获取 logger，自动继承入口处的配置
logger = logging.getLogger(__name__)


def process_data(file_path: str) -> dict:
    """处理数据文件。

    Args:
        file_path: 数据文件的路径。

    Returns:
        解析后的数据字典。
    """
    logger.info("开始处理数据文件: %s", file_path)

    try:
        # 读取并解析文件
        result = _parse_file(file_path)
        logger.info("数据处理完成，共 %d 条记录", len(result))
        return result
    except FileNotFoundError:
        logger.error("文件不存在: %s", file_path)
        raise
    except Exception:
        logger.exception("处理文件时发生未预期的错误")
        raise
```

### 6.3 关键规则

- **绝对禁止**在提交的代码中使用 `print()` 输出调试信息。
- 使用 `logger.exception()` 记录异常，它会自动附加 traceback。
- 日志消息使用 `%s` 占位符（惰性格式化），**不要**用 f-string：
  - 正确：`logger.info("用户 %s 登录成功", user_id)`
  - 错误：`logger.info(f"用户 {user_id} 登录成功")`
- 循环内避免高频 `INFO` 日志，大批量操作用 `DEBUG` 或按间隔采样记录。

---

## 检验清单

以上规则生效的标志：

- diff 中没有无关改动。
- 不存在因为"过度设计"而需要返工的情况。
- 澄清性提问发生在编码**之前**，而不是犯错**之后**。
- 每个 Python 函数都有清晰的中文 docstring。
- 项目中看不到裸露的 `print()` 调用，所有输出走统一日志。
