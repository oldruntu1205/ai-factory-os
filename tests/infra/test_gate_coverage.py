"""门禁覆盖面的守卫：任何测试文件**要么被跑、要么被显式豁免**。

## 这条防的是什么

门禁的步骤是**手工清单**。新写一个测试文件，如果没人记得加进清单，
它就**永远不会执行**——而门禁照样全绿。**静默跳过比没测试更危险**：
它给人一种"已经验过"的错觉。

门禁里已有反向覆盖（`_unreferenced_test_files()` 把 `tests/` 下未引用的文件
补进 assertions 阶段）。本守卫补的是**它扫不到或说不清**的部分：

| 缺口 | 本守卫怎么处理 |
| --- | --- |
| `backend/tests/` **不在**反向覆盖的扫描面内 | 逐个核对，并要求**显式豁免 + 理由** |
| 豁免可能悄悄过期（文件删了、改名了） | 断言每个豁免**确实还存在** |
| 新文件可能无人登记 | 断言集合差异为空 |

## ⚠️ 为什么"豁免要写理由"

`backend/tests/` 下的 6 个文件属 **V2.1 后端**（FastAPI TestClient、RabbitMQ、
DeepSeek API key、真实数据库），**不属 V3.0 门禁范围**——不跑它们是对的。

但**"不跑"与"明确记为不跑"是两件事**：前者是静默跳过，后者是可核对的决定。
所以这里把它们列成带理由的豁免表，而不是靠"没人注意"。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import gate  # noqa: E402

BACKEND_TESTS = REPO_ROOT / "backend" / "tests"

#: 明确的**不跑**决定 → 理由。**每条都必须说清为什么不在 V3.0 门禁范围内。**
BACKEND_TESTS_EXEMPT: dict[str, str] = {
    "backend/tests/test_api.py": (
        "V2.1 后端全链路测试（FastAPI TestClient + 注册/登录/RAG/CRM/报价/驾驶舱），"
        "依赖 V2.1 在产依赖与运行时；V3.0 对应的验收在 `tests/p0_runtime` 等真实多进程套件"
    ),
    "backend/tests/test_channels.py": (
        "V2.1 外部渠道专项（企业微信 / WhatsApp / 邮件自动化），依赖外部渠道凭据；"
        "V3.0 的渠道语义在 `tests/p2_integrations`"
    ),
    "backend/tests/test_hermes.py": (
        "V2.1 Hermes Runtime 全链路（含 Agent 注册/发现/工具权限），"
        "依赖 V2.1 装配；V3.0 对应面在 `tests/p2_runtime` 与 `tests/p3_runtime`"
    ),
    "backend/tests/test_llm_gateway.py": (
        "LLM 网关连通性（**需要真实 DeepSeek 路由与 API key**）——"
        "把需要外网凭据的测试放进门禁，会让门禁在离线环境假红"
    ),
    "backend/tests/test_marketplace.py": (
        "V2.1 Agent 商城（**需要 RabbitMQ**）。本机无 docker、无 broker；"
        "V3.0 的事件面用 `tests/p15_events` 的 journal 语义验收，不需要 broker"
    ),
    "backend/tests/test_tool_call_parser.py": (
        "V2.1 工具调用解析器单测（`USE_SQLITE=1`），"
        "属 V2.1 单体内部实现；V3.0 的工具契约在 `packages/contracts/schemas/tool-spec.json`"
        " 与 `tests/contracts` 校验"
    ),
}


def _gate_referenced_tests() -> set[str]:
    """门禁步骤里引用到的测试路径（文件或目录，均以仓库相对 POSIX 形式）。"""
    referenced: set[str] = set()
    for steps in gate.STAGE_STEPS.values():
        for _name, argv, _note in steps:
            for token in argv:
                if token.startswith("tests/") or token.startswith("backend/tests/"):
                    referenced.add(token.rstrip("/"))
    return referenced


def test_no_test_file_is_silently_skipped():
    """**每个**测试文件要么被门禁引用，要么在豁免表里。

    这条是本文件的核心：静默跳过正是"写了却永不执行"的成因。
    """
    referenced = _gate_referenced_tests()
    silently_skipped: list[str] = []

    for root in (REPO_ROOT / "tests", BACKEND_TESTS):
        if not root.exists():
            continue
        for path in sorted(root.rglob("test_*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            directory = path.parent.relative_to(REPO_ROOT).as_posix()
            if relative in referenced or directory in referenced:
                continue
            if relative in BACKEND_TESTS_EXEMPT:
                continue
            silently_skipped.append(relative)

    assert not silently_skipped, (
        "以下测试文件**既没被门禁引用、也不在豁免表里**（静默跳过）：\n  "
        + "\n  ".join(silently_skipped)
        + "\n\n请二选一：① 加进门禁步骤；② 加进 BACKEND_TESTS_EXEMPT 并**写清为什么**。"
    )


def test_every_exemption_still_exists():
    """豁免不得**过期**——否则豁免表会积累早已不存在的东西，读者分不清哪些还在用。"""
    stale = [key for key in BACKEND_TESTS_EXEMPT if not (REPO_ROOT / key).is_file()]
    assert not stale, (
        f"豁免表里的文件已不存在：{stale}——请清理，否则豁免表会退化成历史账"
    )


def test_every_exemption_states_a_reason():
    """豁免必须**逐条说明理由**，不能只列文件名。"""
    thin = [key for key, reason in BACKEND_TESTS_EXEMPT.items() if len(reason.strip()) < 20]
    assert not thin, f"以下豁免的理由过于简略，等于没写：{thin}"


def test_backend_tests_are_outside_the_reverse_coverage_scan():
    """**能力自证 + 记录事实**：证明 `backend/tests/` 确实**不在**反向覆盖的扫描面内。

    这条同时是给未来读者的路标：如果哪天反向覆盖扩到了 `backend/`，
    本用例会失败，提醒"豁免表可以退休了"。
    """
    unscanned = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in BACKEND_TESTS.rglob("test_*.py")
    )
    assert unscanned, "backend/tests 下应有测试文件，否则本守卫失去对象"

    auto_covered = set(gate._unreferenced_test_files())
    overlap = [path for path in unscanned if path in auto_covered]
    assert not overlap, (
        f"反向覆盖**已经**扫到了 backend/tests 下的文件：{overlap}\n"
        "说明扫描面已扩大，BACKEND_TESTS_EXEMPT 的部分条目可以退休了。"
    )


def test_the_exemption_table_is_not_a_catch_all():
    """豁免表不得变成"什么都往里塞"的垃圾桶。

    判据：豁免数必须**小于** `backend/tests` 下的测试文件总数——
    若相等，说明整个目录被一句话豁免掉了，那与静默跳过没有区别。
    """
    total = len(list(BACKEND_TESTS.rglob("test_*.py")))
    assert len(BACKEND_TESTS_EXEMPT) < total, (
        f"豁免表覆盖了 backend/tests 下全部 {total} 个文件——"
        "那是整目录豁免，等于没做逐个判断"
    )
