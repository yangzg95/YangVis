"""首批内置本地工具示例：零依赖的随机数工具。

新增本地工具照这个文件抄：写普通函数（sync/async 均可），模型可见参数加类型
注解，docstring 首段写给模型看、``Args:`` 段写参数说明，再加上
``@local_tool(title=...)``。函数名即模型看到的工具名。
"""
import random
import uuid

from app.services.tools import local_tool


@local_tool(title="生成 UUID")
def generate_uuid(count: int = 1) -> str:
    """生成一个或多个随机 UUID（v4），多个时用逗号分隔。

    需要唯一标识、临时令牌或占位数据时调用。

    Args:
        count: 生成数量，1 到 100，默认 1。
    """
    if not 1 <= count <= 100:
        # 抛给 ToolNode 的异常会变成 ToolMessage 回灌模型，让它换个参数重试，
        # 不会中断整条回答。
        raise ValueError("count 必须在 1 到 100 之间")
    return ", ".join(str(uuid.uuid4()) for _ in range(count))


@local_tool(title="随机整数")
def random_integer(min_value: int = 0, max_value: int = 100) -> int:
    """在指定闭区间内返回一个随机整数。

    需要随机数、模拟数据或抽签时调用。

    Args:
        min_value: 区间下限（含），默认 0。
        max_value: 区间上限（含），默认 100。
    """
    if min_value > max_value:
        raise ValueError("min_value 不能大于 max_value")
    return random.randint(min_value, max_value)
