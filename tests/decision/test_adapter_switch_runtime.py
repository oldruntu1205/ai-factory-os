"""**换实现，内核不改、测试不改**——上游 §3「均需实现同一契约」的正面证明。

## 这条用例想证明什么

前面 `test_decision_adapter.py` 与 `test_adapter_parity.py` 都是**进程内**调用：
它们证明"两个类都满足协议"，但**没有**证明"把服务切到另一个实现，服务照常工作"。

而"换实现不改内核"这句话只有在**真的换一次**之后才算验证过。

## 怎么换

内核通过 `V3_REGISTRY_PATH` 读登记表（`service.py`：
`Registry.load(os.environ.get("V3_REGISTRY_PATH"))`），而适配器由登记表的
`decision_adapter.implementation` 决定。所以：

1. 把登记表复制一份，只改 `implementation` 为 `rules`；
2. 用 `V3_REGISTRY_PATH` 指向副本起集群；
3. 让**同一组契约性判据**跑在它上面。

## 判据只取"两个实现都该满足"的部分

不比对具体路由值（那是各实现的判定差异，不是契约），只验**契约性质**：
输出有限类型、置信度不伪造、决策类型回显、输入真的影响结论。

## 一条纪律：临时登记表放 tmp_path，不进仓库

仓库卫生门禁（`test_repo_hygiene.py`）不允许测试产物污染仓库。
而且**改仓库里的登记表**会让"切换一次"变成"永久改了配置"——
那正好掩盖了我们要验的东西。
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from tests.p0_runtime.conftest import Cluster, context_body, schema_binding

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "services/_kernel/registry.mock.json"
IDENTITY = {"X-V3-Identity": "svc-hermes-runtime"}


@pytest.fixture(scope="module")
def rules_registry(tmp_path_factory) -> pathlib.Path:
    """把登记表切到 `rules` 适配器，写到临时目录（**不动仓库里的那份**）。"""
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert document["decision_adapter"]["implementation"] == "mock", (
        "仓库默认应当是 mock；本用例专门验'切到 rules'"
    )
    switched = copy.deepcopy(document)
    switched["decision_adapter"]["implementation"] = "rules"

    target = tmp_path_factory.mktemp("rules-registry") / "registry.json"
    target.write_text(json.dumps(switched, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def rules_cluster(rules_registry, tmp_path_factory):
    """用切过的登记表起一个集群。**其余一切与 P0 集群相同。**"""
    cluster = Cluster(
        evidence_path=tmp_path_factory.mktemp("rules-evidence"),
        registry_path=rules_registry,
    )
    cluster.start()
    yield cluster
    cluster.stop()


def _headers(trace: str) -> dict:
    return {**IDENTITY, "X-Context": json.dumps(context_body(trace_id=trace))}


def _decision_body(*, kind: str, tool_id: str, trace: str) -> dict:
    context = context_body(trace_id=trace)
    return {
        **context,
        "context": context,
        "input_ref": "input-demo",
        "input_schema": schema_binding(),
        "catalog_version": "catalog-mock-1",
        "decision_kind": kind,
        "tool_id": tool_id,
    }


def test_service_runs_on_a_second_adapter_without_any_code_change(rules_cluster):
    """**核心判据**：服务切到第二个适配器后，接口照常可用且回显决策类型。

    这一步没有任何代码改动——只改了登记表的一个字符串。
    如果它失败，说明"适配层"是假的：换实现仍要动代码。
    """
    trace = "trace-switch-route"
    status, body = rules_cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        _decision_body(kind="Route", tool_id="erp.stock.query", trace=trace),
        headers=IDENTITY,
    )
    assert status == 200, body
    assert body["decision_kind"] == "Route"
    assert body["route"], "必须有路由结论"
    assert body["confidence"] is None, "规则适配器同样不得伪造置信度"


def test_second_adapter_still_refuses_what_it_cannot_do(rules_cluster):
    """切了实现，**未实现的类型仍然被拒绝**——契约不因实现不同而放宽。"""
    trace = "trace-switch-rank"
    status, body = rules_cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        _decision_body(kind="Rank", tool_id="erp.stock.query", trace=trace),
        headers=IDENTITY,
    )
    assert status == 422, body
    assert body["code"] == "DECISION_KIND_NOT_IMPLEMENTED"


def test_second_adapter_routes_differently_by_risk(rules_cluster):
    """切了实现之后，**输入真的影响结论**（这正是"第二个实现"的意义）。

    在 mock 下同样两个请求会得到同一条路由；在 rules 下必须不同。
    这条同时验证了两件事：换实现生效了，且 rules 不只是 mock 的复制品。
    """
    read_status, read_body = rules_cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        _decision_body(kind="Route", tool_id="erp.stock.query", trace="trace-switch-read"),
        headers=IDENTITY,
    )
    write_status, write_body = rules_cluster.post(
        "decision-service",
        "/internal/v1/decisions",
        _decision_body(kind="Route", tool_id="pmc.dispatch.publish", trace="trace-switch-write"),
        headers=IDENTITY,
    )
    assert read_status == 200 and write_status == 200, (read_body, write_body)
    assert read_body["route"] != write_body["route"], (
        "切到 rules 后，只读与写入仍得到同一条路由——第二个实现没有真正生效。\n"
        f"  只读：{read_body['route']}\n  写入：{write_body['route']}"
    )
