"""全路由可达性：**每条注册路由都必须真的能被调到**（结构约束，不映射到 A01–A14）。

## 为什么需要这条（本轮实测发现的缺口）

我先用文本探针（grep 测试代码里有没有出现该路径）审计覆盖，结论**不可靠**：
测试用 f-string 拼路径、把路径拆成常量，于是同一件事既会漏报也会误报。
中途我的两版探针分别给出"8 条未覆盖"和"5 条未覆盖"，**都是错的**。

于是改成**在真实 dispatch 上记账**（`V3_ROUTE_COVERAGE_FILE`），跑完 1140 条用例后看事实：

```
注册路由: 61   实测被调用过的不同路由: 60
从未被调用: GET /api/v1/edge/shadow-reports/{report_id}
```

也就是说，**有一条已注册、已声明"接口已接线"的接口，从来没有被任何用例调过**。
它是不是坏的？**不知道**——这正是问题：接口接线了却零覆盖，
要么它是死代码，要么它坏着而没人发现。两种都不该存在。

## 这条断言守什么

对**每条注册路由**发一次请求（带身份、带最小可行上下文），断言它**确实路由到了处理器**：

- 不得是"路径不属于本服务"（那意味着这条路径其实打不到）；
- 不得是 400 且理由为"方法不匹配"（那意味着方法登记错了）；
- 业务未命中（对象不存在）**不算失败**——请求走到了处理器、处理器按业务规则回答了，
  这正是可达性的证明；但必须给出**明确的业务错误码**，不能是空壳 404。

**它不断言业务语义**——语义由各阶段套件负责。
它断言的是"**这条路存在且可达**"，这正是零覆盖路由暴露出的空白。

## 为什么值钱

新增一条路由却忘了接测试时，这条会立刻报红——
不需要谁记得去更新覆盖清单（清单本身会过期）。
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from services._kernel import routes as routes_module
from tests.p0_runtime.conftest import context_body

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}

# 路径参数占位值。取真实存在的标识，让请求有能力走到更深的分支。
_PLACEHOLDERS = {
    "{task_id}": "task-demo",
    "{command_id}": "command-demo",
    "{approval_id}": "approval-demo",
    "{release_id}": "release-demo",
    "{run_id}": "run-demo",
    "{saga_id}": "saga-demo",
    "{request_id}": "request-demo",
    "{dlq_id}": "dlq-demo",
    "{report_id}": "report-demo",
    "{device_ref}": "device-demo",
    "{tool_id}": "pmc.dispatch.publish",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _service_by_owner() -> dict[str, str]:
    by_owner = {owner: service for service, owner in routes_module.OWNER_BY_SERVICE.items()}
    # `edge` 的 owner 在本地由 manufacturing-core 承载（生产是 Edge Gateway）
    by_owner.setdefault("edge", routes_module.EDGE_OWNER_SERVICE)
    return by_owner


def _concrete_path(pattern: str) -> str:
    path = pattern
    for name, value in _PLACEHOLDERS.items():
        path = path.replace(name, value)
    # 兜底：任何未登记的占位符都要被替换，否则请求会打到字面量 `{x}` 上，
    # 那样测出来的是"路径不存在"，而不是我们要验的可达性。
    return re.sub(r"\{[^}]+\}", "placeholder", path)


def _all_routes() -> list[tuple[str, str, str, str]]:
    """(service_id, method, concrete_path, pattern)。"""
    by_owner = _service_by_owner()
    rows: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for owner, entries in routes_module.ROUTES_BY_OWNER.items():
        service_id = by_owner.get(owner)
        if service_id is None:
            continue
        for route in entries:
            key = (route.method, route.pattern)
            if key in seen:
                continue
            seen.add(key)
            rows.append((service_id, route.method, _concrete_path(route.pattern), route.pattern))
    return sorted(rows)


ALL_ROUTES = _all_routes()


def test_every_registered_route_is_actually_probed():
    """自检：探针必须覆盖**全部**注册路由，不能悄悄漏掉。

    没有这条，"全路由可达"这句话本身就可能是残缺的——
    收集函数少列了几条，测试照样全绿。本项目在别处已经吃过这种亏。
    """
    registered: set[tuple[str, str]] = set()
    for entries in routes_module.ROUTES_BY_OWNER.values():
        registered |= {(route.method, route.pattern) for route in entries}
    probed = {(method, pattern) for _, method, _, pattern in ALL_ROUTES}
    assert probed == registered, (
        f"探针漏了注册路由：{sorted(registered - probed)}\n"
        f"探针多出未注册路由：{sorted(probed - registered)}"
    )


@pytest.mark.parametrize(
    "service_id,method,path,pattern",
    ALL_ROUTES,
    ids=[f"{m}-{p}" for _, m, _, p in ALL_ROUTES],
)
def test_route_is_reachable(cluster, service_id, method, path, pattern):
    """每条注册路由都必须能被调通（而不是打不到 / 方法登记错）。"""
    context = context_body(trace_id=f"trace-reach-{uuid.uuid4().hex[:8]}")

    if method == "GET":
        status, payload = cluster.get(
            service_id, path, headers={**IDENTITY, "X-Context": _json(context)}
        )
    else:
        body = {**context, "context": context}
        status, payload = cluster.post(service_id, path, body, headers=IDENTITY)

    message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
    code = str(payload.get("code", "")) if isinstance(payload, dict) else ""

    # **"路由不存在"与"业务对象不存在"是两件事，必须分辨。**
    #
    # 两者都可能是 404，但含义相反：
    # - 路由表里没这条路径 ⇒ 可达性失败；
    # - 路径存在、只是引用（command/approval/release…）不存在 ⇒ **可达性成立**。
    route_missing = status == 404 and "不属于本服务" in message
    assert not route_missing, (
        f"{method} {pattern} → 404：这条路由**注册了却打不到**（承载服务 {service_id}）。\n"
        f"响应：{message[:200]}"
    )
    assert not (status == 400 and "方法" in message), (
        f"{method} {pattern} → 400（{message}）：方法可能登记错了"
    )
    assert status < 500, (
        f"{method} {pattern}（承载 {service_id}）返回 {status}：{message[:200]}"
    )
    # 业务未命中必须是**明确的**业务错误码，不能是个空壳 404
    if status == 404:
        assert code and code != "RESOURCE_NOT_FOUND", (
            f"{method} {pattern} → 404 但未给出业务错误码（code={code!r}）："
            "调用方无法区分「路由不存在」与「对象不存在」"
        )
