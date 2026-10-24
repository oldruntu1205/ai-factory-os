"""基础设施清单门禁（上游 §15 `infra/compose|deployment|network/`）。

> 端口、环境变量和依赖版本归部署 manifest 管理；仓库仅提交 `.env.example`。

这一组用例守的是**清单与实现不漂移**：

1. 清单里的服务 id / owner / 端口 / 端口变量名，必须与代码逐项一致
   （`tests/p0_runtime/conftest.py` 的 `SERVICE_MODULES`/`service_ports`
   与 `services/_kernel/routes.py` 的 `OWNER_BY_SERVICE`）；
2. **清单与编排文件里不得硬编码凭据**——只允许 `${VAR}` 或 `${VAR:-默认}` 形式，
   且凭据类的默认值必须为空（"悄悄用一个弱默认值跑起来"是上游明文禁止的）；
3. 网络 ACL 的接口分层必须与代码里的路由归属一致，
   并且**内部接口不得从对外的 frontdoor 域可达**。

## 为什么这些要机器核对

端口写错、owner 对不上、内部接口被划进对外域——这三类问题
**在部署时才暴露**，而那时候排查成本最高。清单存在的意义就是让它们在这里报红。
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"

MANIFEST = yaml.safe_load((INFRA / "deployment" / "manifest.yml").read_text(encoding="utf-8"))
ACL = yaml.safe_load((INFRA / "network" / "network-acl.yml").read_text(encoding="utf-8"))
COMPOSE_TEXT = (INFRA / "compose" / "docker-compose.dev.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)

# 凭据类键名：这些**绝不允许**有非空默认值
CREDENTIAL_HINTS = re.compile(r"(?i)(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|PRIVATE|CREDENTIAL|AES_KEY)")


def _impl_services() -> dict[str, str]:
    from tests.p0_runtime.conftest import SERVICE_MODULES

    return dict(SERVICE_MODULES)


def _impl_ports() -> dict[str, int]:
    from tests.p0_runtime.conftest import service_ports

    return dict(service_ports())


def _impl_owner_by_service() -> dict[str, str]:
    from services._kernel.routes import OWNER_BY_SERVICE

    return dict(OWNER_BY_SERVICE)


# ==========================================================================
# 一、清单 ↔ 代码
# ==========================================================================

def test_manifest_covers_exactly_the_implemented_services():
    """清单里的服务集合必须与代码里的运行单元**完全一致**（不漏不多）。"""
    declared = {entry["id"] for entry in MANIFEST["services"]}
    implemented = set(_impl_services())
    assert declared == implemented, (
        f"清单与实现不一致\n  仅在清单: {sorted(declared - implemented)}\n"
        f"  仅在实现: {sorted(implemented - declared)}"
    )


def test_manifest_module_paths_match_the_implementation():
    """模块路径必须与代码里实际启动的 `-m` 参数一致。"""
    implemented = _impl_services()
    for entry in MANIFEST["services"]:
        assert entry["module"] == implemented[entry["id"]], (
            f"{entry['id']} 的模块路径与实现不符：清单 {entry['module']}，实现 {implemented[entry['id']]}"
        )


def test_manifest_ports_match_the_implementation():
    """端口必须与代码里的默认端口一致。"""
    implemented = _impl_ports()
    for entry in MANIFEST["services"]:
        assert entry["port"] == implemented[entry["id"]], (
            f"{entry['id']} 端口不符：清单 {entry['port']}，实现 {implemented[entry['id']]}"
        )


def test_port_environment_variable_names_follow_the_convention():
    """端口覆盖变量名必须是 `V3_PORT_<ID 大写、连字符换下划线>`。

    代码侧按同一规则拼变量名（见 `tests/p0_runtime/conftest.port_env_name`），
    名字写错会让"覆盖端口"静默失效——服务仍用默认端口启动，
    而编排以为它换了端口。
    """
    for entry in MANIFEST["services"]:
        expected = "V3_PORT_" + entry["id"].upper().replace("-", "_")
        assert entry["port_env"] == expected, (
            f"{entry['id']} 的端口变量名应为 {expected}，清单写作 {entry['port_env']}"
        )


def test_manifest_owner_matches_the_route_owner():
    """`owner` 是数据主责方，必须与路由归属一致（上游"一数一源"）。"""
    implemented = _impl_owner_by_service()
    for entry in MANIFEST["services"]:
        assert entry["owner"] == implemented[entry["id"]], (
            f"{entry['id']} 的 owner 不符：清单 {entry['owner']}，路由 {implemented[entry['id']]}"
        )


def test_every_service_id_is_unique():
    ids = [entry["id"] for entry in MANIFEST["services"]]
    assert len(ids) == len(set(ids)), "清单里有重复的服务 id"


# ==========================================================================
# 二、编排 ↔ 清单
# ==========================================================================

def test_compose_declares_every_manifest_service():
    declared = {entry["id"] for entry in MANIFEST["services"]}
    in_compose = set(COMPOSE.get("services", {}))
    assert declared == in_compose, (
        f"compose 与清单不一致\n  仅清单: {sorted(declared - in_compose)}\n"
        f"  仅 compose: {sorted(in_compose - declared)}"
    )


def test_compose_uses_the_same_port_variable_names():
    for entry in MANIFEST["services"]:
        assert entry["port_env"] in COMPOSE_TEXT, (
            f"compose 未引用 {entry['port_env']}——端口覆盖会失效"
        )


def test_internal_network_has_no_route_to_the_internet():
    """服务间网络必须 `internal: true`（容器层面没有对外路由）。"""
    networks = COMPOSE.get("networks", {})
    assert "internal" in networks, "compose 未声明 internal 网络"
    assert networks["internal"].get("internal") is True, (
        "internal 网络必须标 internal: true——否则内部服务可以直连外网，绕过出域治理"
    )


def test_only_the_security_gateway_is_published_to_the_host():
    """只有安全网关对外发布端口；其余单元只在内部网络可寻址。"""
    published = [
        name for name, spec in COMPOSE["services"].items() if spec.get("ports")
    ]
    manifest_frontdoor = [
        entry["id"] for entry in MANIFEST["services"] if entry["exposure"] == "frontdoor"
    ]
    assert sorted(published) == sorted(manifest_frontdoor), (
        f"对外发布的单元与清单的 frontdoor 不一致：compose={sorted(published)}，"
        f"清单={sorted(manifest_frontdoor)}"
    )


# ==========================================================================
# 三、不得硬编码凭据（上游 §15 明文）
# ==========================================================================

def _credential_assignments(text: str) -> list[tuple[str, str]]:
    """挑出形如 `KEY: value` / `KEY=value` 且键名像凭据的行。"""
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$", stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip().strip('"\'')
        if CREDENTIAL_HINTS.search(key):
            found.append((key, value))
    return found


@pytest.mark.parametrize(
    "path", sorted(INFRA.rglob("*.yml")) + sorted(INFRA.rglob("Dockerfile*")),
    ids=lambda p: str(p.relative_to(INFRA)),
)
def test_no_hardcoded_credentials_in_infra(path):
    """基础设施文件里**不得出现**凭据字面量。

    允许两种形式：`${VAR}`（无默认）与 `${VAR:-默认}`（默认必须为空）。
    一个非空默认值会让"忘记注入密钥"变成"悄悄用弱凭据跑起来"——
    这正是上游禁止的。
    """
    text = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for key, value in _credential_assignments(text):
        if not value:
            continue
        # 引用变量且默认值为空 ⇒ 合规
        ref = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}", value)
        if ref:
            continue
        # 引用变量但带非空默认 ⇒ 违规
        offenders.append(f"{key} = {value[:40]!r}")
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} 出现硬编码或带非空默认的凭据：\n  " + "\n  ".join(offenders)
    )


def test_compose_credential_references_have_no_non_empty_default():
    """compose 里凭据类变量即便用 `${VAR:-x}` 也不许有非空 `x`。"""
    bad: list[str] = []
    for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}", COMPOSE_TEXT):
        name, default = match.group(1), match.group(2)
        if CREDENTIAL_HINTS.search(name) and default:
            bad.append(f"{name} 的默认值非空")
    assert not bad, "凭据类变量带了非空默认值：\n  " + "\n  ".join(bad)


def test_placeholder_compose_file_for_root_does_not_leak_into_infra():
    """仓库根的 V2.1 `docker-compose.yml` **不在**本门禁范围内，但必须被登记。

    它含硬编码的开发口令（`POSTGRES_PASSWORD`）。这里只断言"我们知道它在那儿"，
    避免它被误当成 V3 的编排；**修正它属单独一步**（见 handoff 的待办）。
    """
    root_compose = REPO_ROOT / "docker-compose.yml"
    if not root_compose.exists():
        pytest.skip("根 compose 已移除——若是有意为之，请同时更新本用例与 handoff")
    assert "industrial123" in root_compose.read_text(encoding="utf-8"), (
        "根 compose 的硬编码口令已变化——请确认它是否已被修正，并更新本用例"
    )
    assert not (INFRA / "compose" / "docker-compose.yml").exists(), (
        "infra/compose 下不应覆盖根 compose；V3 编排用独立文件"
    )


# ==========================================================================
# 四、网络 ACL ↔ 接口分层
# ==========================================================================

def test_acl_covers_the_three_interface_layers_plus_the_health_surface():
    """接口前缀必须覆盖 `/api/v1`、`/internal/v1`、`/edge/v1` 三层。

    `/health`（探活面）单独列出：它**不承载业务数据**，受众是匿名的，
    与三层业务接口不是一类——把它混进"接口层"会让人误以为它也走业务鉴权。
    """
    prefixes = {rule["path_prefix"] for rule in ACL["interface_rules"]}
    assert {"/api/v1", "/internal/v1", "/edge/v1"} <= prefixes, (
        f"缺少业务接口层：{{'/api/v1','/internal/v1','/edge/v1'}} - {prefixes}"
    )
    assert "/health" in prefixes, "探活面未在 ACL 中声明"


def test_internal_interface_is_not_reachable_from_the_user_domain():
    """**内部接口不得从对外的 frontdoor 域可达**（上游：内外接口不同认证受众）。"""
    internal_rules = [r for r in ACL["interface_rules"] if r["path_prefix"] == "/internal/v1"]
    assert internal_rules, "ACL 未声明 /internal/v1 的规则"
    for rule in internal_rules:
        assert rule["domain"] != "frontdoor", (
            "/internal/v1 被划进了对外的 frontdoor 域——内部接口会暴露给用户"
        )
        assert rule["audience"] == "service-account", (
            "/internal/v1 的认证受众必须是服务账号"
        )


def test_each_interface_layer_has_a_distinct_audience():
    """三层接口的认证受众必须两两不同——同一个人群能访问两层就等于没分层。"""
    audiences = [rule["audience"] for rule in ACL["interface_rules"] if rule["path_prefix"] != "/health"]
    assert len(audiences) == len(set(audiences)), f"接口层的认证受众有重复：{audiences}"


def test_declared_domains_match_the_compose_networks():
    domains = {d["name"] for d in ACL["domains"]}
    compose_networks = set(COMPOSE.get("networks", {}))
    assert domains == compose_networks, (
        f"ACL 的域与 compose 的网络不一致\n  仅 ACL: {sorted(domains - compose_networks)}\n"
        f"  仅 compose: {sorted(compose_networks - domains)}"
    )


def test_domains_that_must_not_reach_the_internet_are_marked_so():
    for domain in ACL["domains"]:
        if domain["name"] in ("internal", "edge-uplink"):
            assert domain["reaches_internet"] is False, (
                f"{domain['name']} 域被标为可出网——内部服务出网会绕过出域治理"
            )


def test_forbidden_list_covers_the_three_structural_risks():
    """反例清单必须显式声明三类结构性风险。"""
    joined = {(item["from"], item["to"]) for item in ACL["forbidden"]}
    assert ("frontdoor", "/internal/v1") in joined, "未禁止用户域访问内部接口"
    assert ("internal", "internet") in joined, "未禁止内部服务直连外网"
    assert ("edge-uplink", "/internal/v1") in joined, "未禁止现场侧直调中心内部接口"


def test_egress_is_denied_by_default_and_only_from_the_security_gateway():
    egress = ACL["egress"]
    assert egress["denied_by_default"] is True
    assert egress["allowed_sources"] == ["security-gateway"], (
        "出域来源必须只有安全网关——否则模型调用会绕过治理与审计"
    )


# ==========================================================================
# 五、清单的诚实性
# ==========================================================================

def test_manifest_declares_unfreeze_and_lists_blockers():
    """未冻结时必须**列出阻塞项**，不得只写一句"待定"。"""
    assert MANIFEST["frozen"] is False, (
        "若已冻结，请同时更新 blocked_by 并说明依据——外部依赖未到位之前不应标为已冻结"
    )
    blockers = MANIFEST.get("blocked_by") or []
    assert blockers, "清单标为未冻结，却没有列出任何阻塞项"
    for item in blockers:
        assert item.get("item") and item.get("need") and item.get("blocks"), (
            f"阻塞项信息不全（需要 item/need/blocks）：{item}"
        )


def test_acl_declares_its_own_unfrozen_state():
    assert ACL.get("frozen") is False
    assert ACL.get("blocked_by"), "ACL 未列出阻塞项"


def test_manifest_lists_required_env_without_secrets():
    """必需环境变量只列**名字与来源**，不得出现值。"""
    required = MANIFEST["required_env"]
    assert required["secret"], "未声明凭据类变量"
    for name in required["non_secret"] + required["secret"]:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), f"环境变量名不规范：{name}"
    # 清单里不应出现任何"看起来是值"的字段
    text = (INFRA / "deployment" / "manifest.yml").read_text(encoding="utf-8")
    for key, value in _credential_assignments(text):
        assert not value or value.startswith("$"), f"manifest 里 {key} 带了值"
