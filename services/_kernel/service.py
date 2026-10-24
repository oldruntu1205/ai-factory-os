"""通用运行单元装配：读登记表的部署映射，注册本 service 的 owner 路由。

service_id 的默认值来自环境变量 `V3_SERVICE_ID`（由各自的 `__main__.py` 设置），
因此同一个内核可以承载八个运行单元，而每个进程只暴露自己的接口集合。
"""

from __future__ import annotations

import os

from . import journal, routes as routes_module
from .http import ServiceApp, serve
from .registry import Registry, RegistryUnavailable

KNOWN_SERVICES = tuple(routes_module.OWNER_BY_SERVICE)

# 只有命令 owner 需要跑对账（把权威回执回流到命令状态）。
RECONCILING_SERVICES = ("transaction-service",)


def build_app(service_id: str, registry: Registry | None = None) -> ServiceApp:
    reg = registry or Registry.load(os.environ.get("V3_REGISTRY_PATH"))
    if service_id not in routes_module.OWNER_BY_SERVICE:
        raise ValueError(f"未登记的运行单元: {service_id}（可选: {', '.join(KNOWN_SERVICES)}）")
    return ServiceApp(service_id, routes_module.routes_for(service_id), reg)


def start_reconciler(service_id: str) -> journal.Reconciler | None:
    """启动回执对账线程（仅 transaction-service）。"""
    if service_id not in RECONCILING_SERVICES:
        return None
    reconciler = journal.Reconciler()
    reconciler.start()
    return reconciler


def resolve_bind(service_id: str, registry: Registry) -> tuple[str, int]:
    deployment = registry.deployment()
    host = str(deployment.get("host") or "127.0.0.1")
    entry = registry.service_deployment(service_id)
    if entry is None:
        raise RegistryUnavailable(f"部署映射缺少 {service_id}")
    # 端口覆盖只用于本地验证（同机并行起多份、故障注入），不是生产配置项。
    override = os.environ.get(f"V3_PORT_{service_id.upper().replace('-', '_')}")
    if override:
        try:
            return host, int(override)
        except ValueError as exc:
            raise RegistryUnavailable(f"端口覆盖不是整数: {override}") from exc
    port = entry.get("port")
    if not isinstance(port, int):
        raise RegistryUnavailable(f"部署映射缺少 {service_id} 的端口")
    return host, port


def main(service_id: str) -> None:
    registry = Registry.load(os.environ.get("V3_REGISTRY_PATH"))
    app = build_app(service_id, registry)
    host, port = resolve_bind(service_id, registry)
    start_reconciler(service_id)
    serve(app, host, port)
