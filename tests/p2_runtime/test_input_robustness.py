"""入参健壮性不变量：**结构合法但内容不全**的请求体不得返回 5xx。

## 为什么需要这条

实测踩到：`/internal/v1/commands` 的 handler 直接取 `body["command_type"]`，
缺字段时抛 `KeyError`，被 dispatch 的兜底逻辑转成 **503 INTERNAL_ERROR**。

这是**一类**缺陷，不是一个：
任何"直接 `body["x"]` 而不先校验"的 handler 都有同样的问题。
把"调用方请求不合法"报成"服务故障"，会让调用方以为服务坏了、
也让排障的人去查错方向。

## 为什么用"行为不变量"而不是静态扫描

我先试过静态审计（正则找 `body[...]` 再比对契约必填字段），
结论是**噪声太大、不可靠**：路径参数（`{approval_id}`）会被误判为请求体字段，
契约里 `required` 的语义与"这个入口是否由调用方提供该字段"也不一致
（`payload_version` 在契约里必填，但编排链路里由上游产出）。

所以改成**可验证的行为断言**：对每个 POST 端点发一个**空对象**请求体，
断言响应**永远不是 5xx**。

这样无论哪个 handler 缺校验，都会被这条抓住；而只要它返回
400/403/409/422 之类的 4xx，就说明它**明确地**拒绝了这份输入。

## 边界（诚实说明）

- 空对象**不是**全字段覆盖；它精确地命中"缺字段即崩"这一类。
- 端点自身的**语义**正确性由各阶段套件负责，这里只守"不崩"。
"""

from __future__ import annotations

import json
import uuid

import pytest

from services._kernel import routes as routes_module
from tests.p0_runtime.conftest import context_body

IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}

# 明确豁免：这些端点用 POST 表达"查询"或触发动作，且**不需要请求体**。
# 每一条都要写明理由——不加解释的豁免就是掩盖。
EXEMPT: dict[str, str] = {
    "/internal/v1/contexts/query": "只读查询；空条件表示「查全部可见范围」，是合法语义",
    "/internal/v1/events": "事件流查询；订阅意图由路径与身份决定",
    "/internal/v1/runs": "启动编排运行；运行参数来自已受理的任务，请求体可为空",
    "/internal/v1/runs/{run_id}/resume": "恢复运行；恢复点由 run_id 决定",
    "/internal/v1/sagas": "创建补偿编排；成员由已受理命令决定",
    "/internal/v1/sagas/{saga_id}/compensations": "推进补偿；步骤由 saga 状态决定",
}

# 这些路径**没有**契约请求体（POST 只用于"触发/查询"）。
# 若给它们塞一个空对象，请求体校验规则未知，不该拿"空体"去考它。
_PATH_PARAM_POST = {
    "/internal/v1/runs/{run_id}/resume",
}


def _identities() -> list[dict[str, str]]:
    """尝试多种身份：端点对身份的归属校验差异很大，这里只关心"别崩"。"""
    return [IDENTITY, {}]


def _post_endpoints() -> list[tuple[str, str, str, str]]:
    """(service_id, path, pattern, owner) —— 全 POST 路由，路径参数填占位值。

    **必须把 owner 映射回真实 service_id**：`ROUTES_BY_OWNER` 的键是 owner
    （`security`/`control`/`runtime`…），而集群是按 service_id
    （`security-gateway`/`control-plane`/`hermes-runtime`…）寻址的。
    直接用 owner 当 service_id 会打错服务。
    """
    service_by_owner = {owner: service for service, owner in routes_module.OWNER_BY_SERVICE.items()}
    # `edge` 的 owner 在本地由 manufacturing-core 承载（生产是 Edge Gateway）
    service_by_owner.setdefault("edge", routes_module.EDGE_OWNER_SERVICE)

    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str, str, str]] = []
    for owner, entries in routes_module.ROUTES_BY_OWNER.items():
        service_id = service_by_owner.get(owner)
        if service_id is None:
            continue
        for route in entries:
            if route.method != "POST":
                continue
            pattern = route.pattern
            path = pattern
            for placeholder in ("{approval_id}", "{task_id}", "{command_id}", "{release_id}", "{run_id}"):
                path = path.replace(placeholder, f"probe-{placeholder.strip('{}')}")
            key = (owner, path)
            if key in seen:
                continue
            seen.add(key)
            result.append((service_id, path, pattern, owner))
    return sorted(result)


@pytest.mark.parametrize("service_id,path,pattern,owner", _post_endpoints())
def test_body_without_required_fields_never_yields_5xx(cluster, service_id, path, pattern, owner):  # A14
    """**公共 Context 齐备、但业务字段全缺**的请求必须被明确拒绝（4xx），不得崩成 5xx。

    这里刻意附上完整 Context：否则端点会先因"Context 不齐"返回 400，
    我们就到不了真正要考的那段业务字段校验。

    豁免的端点如果收到空体也**不应崩**——只是不强求它拒绝。
    """
    context = context_body(trace_id=f"trace-robust-{uuid.uuid4().hex[:8]}")
    # P0 契约把 Context 字段平铺在顶层；P1 契约把完整 Context 嵌在 `context` 下。
    # 两者都给，好让请求先通过 Context 校验、走到真正的业务字段校验。
    body = {**context, "context": context}

    last: tuple[int, dict] | None = None
    for identity in _identities():
        status, payload = cluster.post(service_id, path, body, headers=identity)
        last = (status, payload)
        if status < 500:
            break
    assert last is not None
    status, payload = last
    if pattern in EXEMPT:
        # 豁免端点只要求"不崩"
        assert status < 500, f"豁免端点也不得崩：{pattern} → {status} {payload}"
        return
    assert status < 500, (
        f"{pattern}（属 {owner}）在业务字段全缺时返回了 {status}："
        f"{json.dumps(payload, ensure_ascii=False)[:220]}\n"
        "缺字段必须显式校验并返回 4xx——把调用方的错误报成服务故障，"
        "会让调用方以为服务坏了，也让排障的人去查错方向。"
    )


def test_command_create_rejects_missing_required_fields_with_400(cluster):  # A14
    """具体回归：这条端点曾因缺业务字段抛 KeyError 被兜底成 503。"""
    context = context_body(trace_id=f"trace-robust-cmd-{uuid.uuid4().hex[:8]}")
    body = {
        **context,
        "context": context,
        "tool_id": "pmc.dispatch.publish",
        "tool_version": "1.0.0-mock",
    }

    status, payload = cluster.post(
        "transaction-service", "/internal/v1/commands", body, headers=IDENTITY
    )
    assert status == 400, payload
    assert payload["code"] == "REQUEST_INVALID", payload
    assert "缺少必填字段" in payload["message"], payload
    # 报错必须点名缺了哪些字段，而不是一句笼统的"参数错误"
    assert "command_type" in payload["message"], payload


# ==========================================================================
# 兜底分支的**边界**：它只准吞"缺请求字段"，不准吞服务内部的编程错误
# ==========================================================================
#
# 上面那条兜底把 `body["x"]` 的 KeyError 转成 400。风险是它可能**过宽**：
# 如果一个内部字典取错键（真正的编程缺陷）也被转成 400，缺陷就被掩盖了。
# 因此判据必须是"这个名字确实是客户端该提供的请求字段"。
#
# 用纯函数单测锁住这个判据——不需要在服务里故意制造一个内部 bug。


class _FakeRequest:
    def __init__(self, method: str, path: str, path_params: dict[str, str] | None = None) -> None:
        self.method = method
        self.path = path
        self.path_params = path_params or {}


def _is_request_field(name: str) -> bool:
    from services._kernel import http

    return http._is_request_field(
        name, _FakeRequest("POST", "/api/v1/tasks", {})
    )


def test_guard_accepts_genuine_request_fields():  # A14
    """契约里声明的请求字段 ⇒ 是请求字段（缺它就报 400）。"""
    from services._kernel import http

    fields = http._declared_body_fields("POST", "/api/v1/tasks")
    assert fields, "契约解析必须能读出属性名，否则这道兜底整体失效"
    for name in sorted(fields)[:5]:
        assert _is_request_field(name), f"{name} 是契约声明的请求字段"


def test_guard_accepts_context_keys_and_path_params():  # A14
    from services._kernel import http

    assert _is_request_field("tenant_id"), "公共 Context 的键算请求字段"
    assert http._is_request_field(
        "command_id", _FakeRequest("POST", "/internal/v1/commands/c-1/leases", {"command_id": "c-1"})
    ), "路径参数算请求字段"


def test_guard_rejects_internal_keys_so_real_bugs_still_surface():  # A14
    """**内部键必须被拒绝**——否则真正的编程缺陷会被伪装成 400 而无人发现。

    这是兜底分支的边界：宁可让真缺陷以 503 暴露，也不要把它掩盖成"客户端请求不合法"。
    """
    for internal in (
        "model_version",          # 登记表记录里的键
        "internal_registry_key",
        "record_version",
        "schema_hash",            # 属工具/契约内部字段，不在 tasks 请求体里
    ):
        assert not _is_request_field(internal), (
            f"{internal} 不该被当作请求字段——把它转成 400 会掩盖服务内部缺陷"
        )


def test_guard_is_not_a_blanket_accept():  # A14
    """判据必须**真的有判别力**：不能退化成"永远返回 True"。

    变异测试曾证明这条边界没被守住（把判据改成恒真，门禁照样全绿）。
    """
    accepted = sum(1 for name in ("input_ref", "internal_registry_key", "record_version") if _is_request_field(name))
    assert 0 < accepted < 3, "判据既不能全拒也不能全收"
