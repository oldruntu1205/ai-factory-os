"""权威事实的**写入点**必须被点名（上游 §6 / §17 A14）。

## 这条守卫为什么存在

本项目在一个会话里**三次**栽在同一类缺陷上：

| 次数 | 缺陷 | 表现 |
| --- | --- | --- |
| 1 | 补偿路径伪造回执 | "什么都没做"的补偿报 `COMPENSATED` |
| 2 | `posting_status` 硬编码 | 界面会把未过账显示成已过账 |
| 3 | 补偿回执的 `command_id` 与命令不一致 | 照回执查命令查不到 |

三次的共同形状是：**某处代码凭空写下一个"成功"断言**，
而没有任何真实执行支撑它。这类缺陷**门禁抓不到**——
因为它不违反任何单点断言，只是让断言"看起来成立"。

## 判据：白名单式**写入点收敛**

权威事实只有两种**合法来源**：

1. **外部回执送达**（`_handler_p15_receipt`）；
2. **查源系统得出结论**（`_handler_p3_reconcile`）。

所以：凡是**字面量**写 `authority_verified: True` 或 `business_success: True` 的函数，
**必须在这两个白名单里**；新出现的函数会被点名，必须解释它凭什么是权威来源。

`_p3_receipt`（合成夹具，构造测试场景的步骤账）登记为**已知例外**，
理由写在代码里——它不是在"声称某动作做过了"，而是"描述一个已完成步骤的形状"。

## 判据的边界（诚实说明）

- 它**只查字面量**。`bool(receipt and receipt.get(...))` 这种**派生**写法不受约束
  ——那正是我们想要的形态，所以不拦。
- 它**不能**证明白名单里的函数真的做了核查。它只保证**新增写入点必须被看见**。
- 它**不查** `SUCCEEDED` 状态字符串（那出现在很多合法的视图构造里，
  逐个判定噪声太大）；只查回执侧的"权威/业务成功"断言。

> 结论：这条守卫把"凭空写成功"从**静默可能**变成**必须显式登记**。
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTES = REPO_ROOT / "services" / "_kernel" / "routes.py"

#: 允许**字面量**写权威成功断言的函数 —— 每一个都必须说明它凭什么是权威来源。
AUTHORITY_FACT_WRITERS = {
    "_handler_p15_receipt": "外部权威回执经接口送达；回执由可信执行方给出",
    "_handler_p3_reconcile": "向源系统查询后记录结论；结论来自源系统而非自报",
    "_p3_receipt": (
        "**已知例外**：合成夹具，构造测试场景的步骤账（描述一个已完成步骤的形状），"
        "不是在声称某个动作刚刚做过了。"
        "详见 docs/adr/0007-compensation-is-a-real-command.md 的已知限制"
    ),
}

CLAIM_FIELDS = {"authority_verified", "business_success"}


def _literal_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _functions_with_claims() -> dict[str, list[tuple[int, str]]]:
    """函数名 → [(行号, 字段名)]，只收录**字面量 True** 的写入。"""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    found: dict[str, list[tuple[int, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            # 关键字形式：authority_verified=True
            if isinstance(inner, ast.keyword) and inner.arg in CLAIM_FIELDS:
                if _literal_true(inner.value):
                    found.setdefault(node.name, []).append((inner.value.lineno, inner.arg))
            # 字典字面量形式："authority_verified": True
            elif isinstance(inner, ast.Dict):
                for key, value in zip(inner.keys, inner.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in CLAIM_FIELDS
                        and _literal_true(value)
                    ):
                        found.setdefault(node.name, []).append((value.lineno, str(key.value)))
    return found


def test_only_named_functions_may_assert_authority_success():
    """**只有被点名的函数**可以字面量写权威成功断言。

    失败时该怎么处理：不是把新函数加进白名单就完事——
    要先问清楚"它凭什么是权威来源"。若答案是"执行方自报"或"此处假定成功"，
    那就是上一轮修掉的那类缺陷。
    """
    offenders: list[str] = []
    for name, claims in sorted(_functions_with_claims().items()):
        if name in AUTHORITY_FACT_WRITERS:
            continue
        lines = ", ".join(f"L{line}({field})" for line, field in claims)
        offenders.append(f"{name}: {lines}")
    assert not offenders, (
        "以下函数凭空写下权威成功断言，但不在白名单内：\n  "
        + "\n  ".join(offenders)
        + "\n\n权威事实只有两种合法来源：外部回执送达、查源系统得出结论。"
        "\n如果新函数确实属于其中之一，请把它加进 AUTHORITY_FACT_WRITERS **并写明理由**。"
    )


def test_allowlist_has_no_stale_entries():
    """白名单里不得留**已经不存在**的函数。

    否则白名单会变成"历史遗留清单"：它拦住的东西早已删掉，
    而真正需要登记的新函数会被它的噪声淹没。
    """
    present = set(_functions_with_claims())
    stale = sorted(set(AUTHORITY_FACT_WRITERS) - present)
    assert not stale, (
        f"白名单里的函数已不存在（或不再写权威断言）：{stale}——请清理，"
        "否则白名单会退化成没人维护的历史清单"
    )


def test_every_allowlisted_function_states_a_reason():
    """白名单每一项都必须写明"它凭什么是权威来源"。"""
    for name, reason in AUTHORITY_FACT_WRITERS.items():
        assert reason and len(reason.strip()) >= 10, f"{name} 的理由太短或为空"


def test_the_probe_can_actually_detect_a_fabricated_claim(tmp_path):
    """**能力自检**：喂一段构造代码，守卫必须报出那个函数。

    没有这条，守卫可能在"探测能力为零"时长期绿灯——
    干净树上"没有伪造"与"根本不会识别伪造"**在观测上无法区分**。
    本项目已在这上面栽过（仓库卫生守卫、领域词表漂移探针）。
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def _handler_something(request):\n"
        "    return {'authority_verified': True, 'business_success': True}\n"
        "\n"
        "def _derived_ok(record):\n"
        "    return {'business_success': bool(record.get('receipt'))}\n",
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Dict):
                    for key, value in zip(inner.keys, inner.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in CLAIM_FIELDS
                            and _literal_true(value)
                        ):
                            names.add(node.name)
    assert names == {"_handler_something"}, (
        f"守卫应**正好**报出字面量写成功的那个函数、且不误报派生写法；实际 {names}"
    )
