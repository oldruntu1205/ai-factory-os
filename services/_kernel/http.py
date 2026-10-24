"""最小 HTTP 服务内核（P0 骨架，仅标准库）。

职责边界（有意收窄）：
- 只做契约 §10.1 中属于本 service 的路由分发；
- 统一执行准入前置：身份存在 → Context 校验 → 逐接口语义约束；
- 统一错误体（errors.py）；
- 追加轨迹证据（evidence.py）。

不做：TLS、真实认证受众、限流、指标导出、broker、DB —— 均为后续阶段。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import context as ctx_module
from . import errors, evidence, metrics
from .registry import REPO_ROOT, Registry, RegistryUnavailable

MAX_BODY_BYTES = 1 << 20  # 1 MiB，P0 骨架的显式上限


class Route:
    """一条契约接口。pattern 支持 `{name}` 占位。"""

    def __init__(
        self,
        method: str,
        pattern: str,
        owner: str,
        handler: Callable[["Request"], tuple[int, dict[str, Any]]],
        *,
        requires_context: bool = True,
        requires_tool: bool = False,
        requires_policy: bool = False,
        requires_approval: bool = False,
        nested_context: bool = False,
    ) -> None:
        self.method = method
        self.pattern = pattern
        self.owner = owner
        self.handler = handler
        self.requires_context = requires_context
        self.requires_tool = requires_tool
        self.requires_policy = requires_policy
        self.requires_approval = requires_approval
        # P0 契约把 Context 字段平铺在请求体顶层；P1 契约把完整 Context 嵌在 `context` 下。
        # 两者都是"公共 Context 显式提供"，只是承载位置不同，内核必须都支持。
        self.nested_context = nested_context
        self.regex = _compile(pattern)

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        matched = self.regex.match(path)
        if not matched:
            return None
        return matched.groupdict()


def _compile(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[A-Za-z0-9_]+\})", pattern)
    out = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            name = part[1:-1]
            out.append(f"(?P<{name}>[A-Za-z0-9][A-Za-z0-9._:-]*)")
        else:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + "$")


class Request:
    __slots__ = (
        "method",
        "path",
        "path_params",
        "body",
        "headers",
        "trace_id",
        "context",
        "registry",
        "service_id",
    )

    def __init__(
        self,
        *,
        method: str,
        path: str,
        path_params: dict[str, str],
        body: dict[str, Any],
        headers: dict[str, str],
        trace_id: str,
        registry: Registry,
        service_id: str,
    ) -> None:
        self.method = method
        self.path = path
        self.path_params = path_params
        self.body = body
        self.headers = headers
        self.trace_id = trace_id
        self.registry = registry
        self.service_id = service_id
        self.context: ctx_module.Context | None = None


class ServiceApp:
    """一个运行单元的 HTTP 应用。"""

    def __init__(self, service_id: str, routes: list[Route], registry: Registry) -> None:
        self.service_id = service_id
        self.routes = routes
        self.registry = registry
        self.started_at = time.time()

    # ---- 健康 -------------------------------------------------------------

    def liveness(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "live": True,
            "ready": False,
            "checked_at": _utc_now(),
            "policy_version": None,
            "backlog": metrics.pending_backlog(),
            "dependencies": [],
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        deployment = self.registry.service_deployment(self.service_id)
        declared = list(deployment.get("required_dependencies", [])) if deployment else []
        dependencies: list[dict[str, Any]] = []
        all_ready = bool(declared)
        for dependency_id in declared:
            probe = self.registry.dependency_probe(dependency_id)
            ready = _probe(dependency_id, probe) if probe else False
            dependencies.append({"dependency_id": dependency_id, "ready": ready})
            all_ready = all_ready and ready
        policy_version = self.registry.default_policy_version()
        if policy_version is None:
            all_ready = False
        # backlog 必须是**真实积压**：曾经这里硬编码 0——那会永远报告"无积压"，
        # 即使系统里堆满了未收敛的命令。现在从 journal 派生（§14 要求就绪含积压）。
        backlog = metrics.pending_backlog()
        body = {
            "service_id": self.service_id,
            "live": True,
            "ready": all_ready,
            "checked_at": _utc_now(),
            "policy_version": policy_version,
            "backlog": backlog,
            "dependencies": dependencies,
        }
        return all_ready, body

    # ---- 业务 -------------------------------------------------------------

    def dispatch(self, request: Request) -> tuple[int, dict[str, Any]]:
        # 1) 先解析路由：不属于本 service 的路径一律 404，且**在认证之前**返回，
        #    这样"路径是否存在"不依赖调用方身份，避免把路由表当侧信道。
        route = None
        path_params: dict[str, str] = {}
        method_mismatch = False
        for candidate in self.routes:
            matched = candidate.match(request.method, request.path)
            if matched is not None:
                route = candidate
                path_params = matched
                break
            if candidate.regex.match(request.path):
                method_mismatch = True
        if route is None:
            raise errors.V3Error(
                errors.HTTP_NOT_FOUND if not method_mismatch else errors.HTTP_BAD_REQUEST,
                "RESOURCE_NOT_FOUND",
                "路径不属于本服务或不存在",
            )
        request.path_params = path_params

        # 1.5) **路由覆盖取证**（默认关闭）。
        #
        # 为什么需要它：想知道"某条接口到底有没有被测试真的调用过"，
        # 靠 grep 测试代码**不可靠**——测试用 f-string 拼路径、用常量做前缀，
        # 文本匹配既会漏（拼出来的路径 grep 不到）也会误报（注释里提一句就算命中）。
        # 本项目已经吃过"文本探针假阳性"的亏，所以改为**在真实 dispatch 上记账**：
        # 设 `V3_ROUTE_COVERAGE_FILE` 时，每命中一条路由就追加一行。
        #
        # 记账放在**路由匹配之后、认证之前**：覆盖关心的是"这条路径可达"，
        # 而认证失败也是真实的调用（例如越权用例），同样算覆盖。
        coverage_file = os.environ.get("V3_ROUTE_COVERAGE_FILE")
        if coverage_file:
            try:
                with open(coverage_file, "a", encoding="utf-8") as handle:
                    handle.write(f"{request.method} {route.pattern}\n")
            except OSError:
                # 取证失败绝不能影响业务请求
                pass

        # 2) 身份：契约要求每个业务接口 security 至少一个 scheme。
        if not _has_identity(request.headers):
            raise errors.reject_unauthenticated()

        # 3) Context：POST 从 body（P1 契约从 body.context）取，GET 从 X-Context 头取。
        if route.requires_context:
            if request.method == "GET":
                raw_source = request.headers.get("x-context")
                try:
                    payload = json.loads(raw_source) if raw_source else None
                except json.JSONDecodeError as exc:
                    raise errors.reject_bad_request("X-Context", f"X-Context 不是合法 JSON ({exc})") from exc
                raw = ctx_module.extract(payload, source="X-Context")
            else:
                source = request.body
                if route.nested_context:
                    nested = request.body.get("context") if isinstance(request.body, dict) else None
                    if nested is None:
                        raise errors.reject_bad_request("context", "P1 接口要求提供完整的 context 对象")
                    # 顶层 tenant_id 必须与嵌套 Context 一致（契约 §2）。
                    outer_tenant = request.body.get("tenant_id")
                    if outer_tenant is not None and nested.get("tenant_id") != outer_tenant:
                        raise errors.V3Error(
                            errors.HTTP_FORBIDDEN,
                            "NESTED_TENANT_MISMATCH",
                            "顶层租户与 Context 不一致",
                            details={"denied_field": "tenant_id"},
                        )
                    source = nested
                raw = ctx_module.extract(source, source="requestBody")
            # 可信身份来自认证头；请求自填 tenant 必须与它一致（不信任自填）。
            request.context = ctx_module.validate(
                raw, request.registry, identity_tenant=_identity_tenant(request.headers, request.registry)
            )

        # 4) 工具登记：接口上声明了 x-registry=tools 的字段必须先过登记表。
        if route.requires_tool:
            tool_id = _first_tool_id(request)
            if tool_id is None:
                raise errors.reject_bad_request("tool_id", "缺少 tool_id")
            if request.registry.tool(tool_id) is None:
                raise errors.reject_unknown_tool(tool_id)

        return route.handler(request)


def _first_tool_id(request: Request) -> str | None:
    source = request.body if request.method == "POST" else {}
    if isinstance(source, dict):
        value = source.get("tool_id")
        if isinstance(value, str) and value:
            return value
    return None


def _has_identity(headers: dict[str, str]) -> bool:
    authorization = headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return False
    return len(authorization.split(" ", 1)[1].strip()) > 0


def _identity_tenant(headers: dict[str, str], registry: Registry) -> str | None:
    """从可信认证信息推出 tenant。

    契约：`tenant` 必须来自可信认证，**不能采信请求自称的 tenant**。
    Mock 形态：`X-V3-Identity` 给出服务身份，`X-V3-Tenant` 给出该身份要代表的租户；
    后者必须在该身份的授权集合内，否则视为不一致（返回一个必然不等的哨兵值）。
    """
    identity = headers.get("x-v3-identity")
    if not identity or not registry.service_account_registered(identity):
        return None
    return registry.identity_tenant(identity, headers.get("x-v3-tenant"))


def _probe(dependency_id: str, spec: str) -> bool:
    # 故障注入：L3 验收需要证明"依赖不可用时 ready=false / 503"，而不是固定 true。
    forced_down = {item.strip() for item in os.environ.get("V3_DEPENDENCIES_DOWN", "").split(",") if item.strip()}
    if dependency_id in forced_down:
        return False
    if spec == "always":
        return True
    if spec.startswith("file:"):
        target = spec[5:]
        path = target if os.path.isabs(target) else str(REPO_ROOT / target)
        return os.path.isfile(path)
    if spec.startswith("tcp:"):
        _, _, rest = spec.partition(":")
        host, _, port = rest.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                return True
        except (OSError, ValueError):
            return False
    return False


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_handler(app: ServiceApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "v3-p0-skeleton"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
            return

        # -- HTTP verbs ----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        # -- 内部 ----------------------------------------------------------

        def _handle(self, method: str) -> None:
            trace_id = uuid.uuid4().hex
            path = self.path.split("?", 1)[0]
            headers = {key.lower(): value for key, value in self.headers.items()}
            body: dict[str, Any] = {}
            raw_body = b""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                self._send(413, errors.error_body(
                    errors.V3Error(errors.HTTP_BAD_REQUEST, "REQUEST_INVALID", "请求体超过上限"),
                    trace_id,
                ))
                return
            if length:
                raw_body = self.rfile.read(length)
                try:
                    body = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send(400, errors.error_body(
                        errors.reject_bad_request("requestBody", f"请求体不是合法 JSON ({exc})"),
                        trace_id,
                    ))
                    return

            if method == "GET" and path == "/health/live":
                self._send(200, app.liveness())
                return
            if method == "GET" and path == "/health/ready":
                ready, payload = app.readiness()
                self._send(200 if ready else 503, payload)
                return
            # 运维指标面（§14）：只读、不要求 Context——它服务于探活与监控抓取，
            # 本身不承载业务数据。响应里带上**显式的环境与数据来源说明**，
            # 使"从 Mock journal 派生"不会被误读成"生产指标已就绪"。
            if method == "GET" and path == "/internal/v1/operations/metrics":
                snapshot = metrics.compute_metrics()
                self._send(200, {
                    "service_id": app.service_id,
                    "environment": metrics.ENVIRONMENT,
                    "source": "derived_from_journal",
                    "inventory": metrics.metric_inventory(),
                    "snapshot": snapshot,
                    "no_data_signals": snapshot["no_data"],
                })
                return
            if method == "GET" and path == "/internal/v1/operations/metrics/text":
                self._send_raw(200, metrics.render_prometheus(), "text/plain; charset=utf-8")
                return

            request = Request(
                method=method,
                path=path,
                path_params={},
                body=body,
                headers=headers,
                trace_id=trace_id,
                registry=app.registry,
                service_id=app.service_id,
            )
            try:
                status, payload = app.dispatch(request)
            except RegistryUnavailable as exc:
                error = errors.reject_registry_unavailable(str(exc))
                status, payload = error.status, errors.error_body(error, trace_id)
            except errors.V3Error as error:
                status, payload = error.status, errors.error_body(error, trace_id)
                _record_rejection(request, error)
            except KeyError as exc:
                # **缺请求字段不是服务故障。**
                #
                # 很多 handler 直接取 `body["x"]`，字段缺失时抛 `KeyError`。
                # 此前它会落到下面的兜底分支变成 **503 INTERNAL_ERROR** ——
                # 实测 33 个端点在"业务字段全缺"时都是这个表现。
                #
                # 把调用方的请求错误报成 5xx，会让调用方以为服务坏了（于是重试、
                # 甚至告警），也让排障的人去查服务而不是查请求。
                #
                # 这里只做**这一件**事：把"请求体里缺字段"改报 400 并点名缺哪个。
                # 判定依据是 KeyError 的键名必须是合法字段名（标识符）——
                # 那正是 `body["x"]` 抛出的形状；其它 KeyError（如字典取值错键）
                # 仍走 503 兜底，不掩盖真正的内部缺陷。
                missing = exc.args[0] if exc.args else None
                if isinstance(missing, str) and _is_request_field(missing, request):
                    error = errors.reject_bad_request(
                        missing, f"请求体缺少必填字段：{missing}"
                    )
                    status, payload = error.status, errors.error_body(error, trace_id)
                    _record_rejection(request, error)
                else:
                    raise
            except Exception as exc:  # pragma: no cover - 兜底，避免裸栈外泄
                # 对外只给结构性错误；内部栈写 stderr，方便 L3 定位（不含业务正文）。
                import traceback

                traceback.print_exc()
                error = errors.V3Error(
                    errors.HTTP_UNAVAILABLE,
                    "INTERNAL_ERROR",
                    "服务内部错误",
                    details={"reason_code": type(exc).__name__},
                )
                status, payload = error.status, errors.error_body(error, trace_id)

            if 200 <= status < 300 and request.context is not None and path.startswith(("/api/", "/internal/", "/edge/")):
                _record_accepted(request, status, payload)
            self._send(status, payload)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_raw(self, status: int, body: str, content_type: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def _is_request_field(name: str, request: "Request") -> bool:
    """`KeyError(name)` 里的 `name` 是否**确实是客户端该提供的请求字段**？

    这个判据决定了"缺字段"兜底分支的**边界**——
    太宽会把服务内部的编程错误也吞成 400，掩盖真缺陷；太窄就漏报。

    三类算请求字段：

    1. 公共 Context 的键（`CONTEXT_KEYS`）——平铺或嵌在 `context` 下都算；
    2. `path_params` 里的键（`{command_id}` 之类由路由解析出来的）；
    3. **该路由契约声明的请求体属性名**。

    第 3 条是关键：它让"这个字段本来就是给客户端填的"可被机械判定，
    而不是靠"名字像标识符"这种弱条件（`isidentifier()` 对
    `internal_registry_key` 这种内部键同样为真，等于没判）。
    """
    if name in ctx_module.CONTEXT_KEYS:
        return True
    if name in request.path_params:
        return True
    return name in _declared_body_fields(request.method, request.path)


@lru_cache(maxsize=256)
def _declared_body_fields(method: str, path: str) -> frozenset[str]:
    """从 OpenAPI 契约里读出该路由请求体的属性名集合。

    契约是唯一权威：与其在代码里维护一份"哪些字段必填"的名单
    （极易与契约漂移），不如直接问契约。

    读不到契约（例如路径带实际 id 而非模板）时返回空集——
    此时宁可走 503 兜底（不掩盖），也不猜测。
    """
    document, operation = _find_operation(method, path)
    if operation is None:
        return frozenset()

    body = operation.get("requestBody")
    if not body:
        return frozenset()
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    return frozenset(_property_names(schema, document))


def _find_operation(method: str, path: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """找到路径对应的 operation，并返回**它自己所属的那份 spec**。

    合并多份 spec 时不能共用一棵 `components` 树：同名 schema 会互相覆盖，
    `$ref` 就会解析到别的 spec 的定义（实测踩过）。

    路径匹配分两步：先原样精确；再按**契约模板归一化后比较**——
    请求路径里带的是真实 id（`/releases/probe-release_id/gate-reopen`），
    契约里是模板（`/releases/{release_id}/gate-reopen`）。

    归一化**必须问契约**："这一段的字面值有没有在任何模板的同一位置出现过"：
    出现过就是字面段，没出现就是参数段。
    第一版把请求路径的非 `{...}` 段一律当字面量，于是 `probe-command_id`
    永远匹配不上 `{command_id}`，兜底判定直接失效。
    """
    combined = _load_openapi_contracts()
    paths = combined.get("paths", {})
    if not paths:
        return {}, None

    entry = paths.get(path)
    if entry and entry.get(method.lower()) is not None:
        return combined["_documents"].get(path, {}), entry[method.lower()]

    literals = _literal_positions()
    wanted = _normalize(path, literals)
    for template, operations in paths.items():
        if _normalize(template, literals) != wanted:
            continue
        operation = operations.get(method.lower())
        if operation is not None:
            return combined["_documents"].get(template, {}), operation
    return {}, None


@lru_cache(maxsize=1)
def _literal_positions() -> tuple[frozenset[str], ...]:
    """每个深度上"作为字面段出现过"的段名集合（来自合并契约，缓存）。"""
    paths = _load_openapi_contracts().get("paths", {})
    by_depth: list[set[str]] = []
    for template in paths:
        for depth, segment in enumerate(item for item in template.split("/") if item):
            if segment.startswith("{"):
                continue
            while len(by_depth) <= depth:
                by_depth.append(set())
            by_depth[depth].add(segment)
    return tuple(frozenset(item) for item in by_depth)


def _normalize(path: str, literals: tuple[frozenset[str], ...]) -> tuple[str, ...]:
    """按契约的字面段集合把路径归一化：非字面段记作 `{}`。"""
    segments = [segment for segment in path.split("/") if segment]
    normalized: list[str] = []
    for depth, segment in enumerate(segments):
        known = literals[depth] if depth < len(literals) else frozenset()
        normalized.append(segment if segment in known else "{}")
    return tuple(normalized)


def _property_names(node: dict[str, Any], document: dict[str, Any], depth: int = 0) -> set[str]:
    """收集 schema 里声明的属性名（含 `$ref` / `allOf` / `oneOf` 组合）。"""
    if depth > 6 or not isinstance(node, dict):
        return set()
    if "$ref" in node:
        return _property_names(_resolve_ref(node["$ref"], document), document, depth + 1)
    names: set[str] = set(node.get("properties") or {})
    for key in ("allOf", "oneOf", "anyOf"):
        for branch in node.get(key) or []:
            names |= _property_names(branch, document, depth + 1)
    return names


@lru_cache(maxsize=1)
def _load_openapi_contracts() -> dict[str, Any]:
    """合并 `packages/contracts/openapi/*.openapi.json`（只读、缓存）。

    **每个 spec 自带自己的 `components`**，因此不能简单地把各 spec 的
    `components` 合并成一份——同名 schema 会互相覆盖，`$ref` 就会解析到
    别的 spec 的定义（实测：`TaskCreateRequest` 被解析成了另一个 spec 的同名节点，
    于是"已声明属性"成了空集）。这里改成**记住每条路径来自哪份 spec**，
    解析 `$ref` 时回到它自己的那棵树。
    """
    combined: dict[str, Any] = {"paths": {}, "_documents": {}}
    contracts_dir = REPO_ROOT / "packages" / "contracts" / "openapi"
    if not contracts_dir.exists():
        return {}
    for spec in sorted(contracts_dir.glob("*.openapi.json")):
        try:
            document = json.loads(spec.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        document["_source"] = spec.name
        for path, operations in (document.get("paths") or {}).items():
            if path in combined["paths"]:
                # 同名路径出现在多份 spec 里：保留先到的，避免静默覆盖
                continue
            combined["paths"][path] = operations
            combined["_documents"][path] = document
    return combined


def _resolve_schema(node: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """就地解析 `$ref`，**逐级回到引用所在的那份 spec**。"""
    seen: set[str] = set()
    current = node
    for _ in range(8):
        ref = current.get("$ref")
        if not ref:
            return current
        if ref in seen:  # 防环
            return {}
        seen.add(ref)
        resolved = _resolve_ref(ref, document)
        if not resolved:
            return {}
        current = resolved
    return current


def _resolve_ref(ref: str, document: dict[str, Any]) -> dict[str, Any]:
    """解析 `$ref`：支持 `#/...` 内部指针与 `../x.json#/$defs/Y` 外链+片段。"""
    if ref.startswith("#"):
        return _walk_pointer(document, ref)

    file_part, _, fragment = ref.partition("#")
    external = (REPO_ROOT / "packages" / "contracts" / file_part.lstrip("./")).resolve()
    try:
        loaded = json.loads(external.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if fragment:
        return _walk_pointer(loaded, "#" + fragment)
    return loaded if isinstance(loaded, dict) else {}


def _walk_pointer(tree: Any, pointer: str) -> dict[str, Any]:
    """按 JSON Pointer 片段取值（`#/$defs/Foo`、`#/components/schemas/Bar`）。"""
    if not pointer.startswith("#"):
        return {}
    target: Any = tree
    for segment in pointer[2:].split("/") if len(pointer) > 2 else []:
        segment = segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict):
            return {}
        target = target.get(segment)
    return target if isinstance(target, dict) else {}


def _record_accepted(request: Request, status: int, payload: dict[str, Any]) -> None:
    context = request.context
    assert context is not None
    evidence.append(
        {
            "kind": "invocation",
            "outcome": "accepted",
            "http_status": status,
            "service_id": request.service_id,
            "route": request.path,
            "method": request.method,
            "tenant_id": context.tenant_id,
            "factory_id": context.factory_id,
            "actor_id": context.actor_id,
            "employee_id": context.employee_id,
            "task_id": context.task_id,
            "trace_id": context.trace_id,
            "data_class": context.data_class,
            "schema_version": context.schema_version,
            "result_refs": _collect_refs(payload),
        }
    )


def _record_rejection(request: Request, error: errors.V3Error) -> None:
    """拒绝也要留痕：契约要求日志只有 trace 与脱敏理由。"""
    context = request.context
    evidence.append(
        {
            "kind": "invocation",
            "outcome": "rejected",
            "http_status": error.status,
            "service_id": request.service_id,
            "route": request.path,
            "method": request.method,
            "tenant_id": getattr(context, "tenant_id", None),
            "task_id": getattr(context, "task_id", None),
            "trace_id": getattr(context, "trace_id", None) or request.trace_id,
            "data_class": getattr(context, "data_class", None),
            "reason_code": error.code,
        }
    )


def _collect_refs(payload: dict[str, Any]) -> list[str]:
    refs = []
    for key in ("task_id", "run_id", "command_id", "decision_id", "invocation_id", "evaluation_ref", "approval_id"):
        value = payload.get(key)
        if isinstance(value, str):
            refs.append(f"{key}={value}")
    return refs


def serve(app: ServiceApp, host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"[{app.service_id}] listening on http://{host}:{port}")
    httpd.serve_forever()
