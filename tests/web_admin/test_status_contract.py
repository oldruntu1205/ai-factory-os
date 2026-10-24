"""管理后台状态契约的门禁包装（上游 §6 / §17 A14）。

判定逻辑本身用 Node 内置的 `node:test` 测（`apps/web-admin/test/*.test.mjs`），
**零外部依赖**——不需要 npm 安装任何东西。

这里做的事只有一件：**把那个套件接进主门禁**，
让"后台显示口径"的回归与其它阶段一起报红，而不是散落在另一个命令里没人跑。

## 缺 Node 时的处理

明确 `skip` 并写明原因，**不算通过**。这与本项目其它外部依赖的处理一致：
"没验证"必须与"验证通过"可区分——静默通过比不测更危险。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "web-admin"

NODE = shutil.which("node")


def test_web_admin_app_exists_and_declares_the_status_contract():  # A14
    """后台应用必须存在，且入口模块与测试都在位。

    这条**不依赖 Node**，所以在没有 Node 的机器上也能守住"文件别被误删"。
    """
    assert APP_DIR.exists(), "apps/web-admin 缺失（上游 §15 布局要求）"
    for relative in (
        "src/status.js",
        "src/view.js",
        "test/status.test.mjs",
        "test/view.test.mjs",
        "package.json",
    ):
        assert (APP_DIR / relative).exists(), f"缺失 {relative}"


def test_package_json_declares_a_test_script_without_dependencies():  # A14
    """包声明必须能被 `npm test` 直接跑，且**不引入运行时依赖**。

    引入依赖就等于把"判断逻辑能不能验证"绑定到 registry 可用性上——
    而我们已确认本机 registry 连通性未定。判定逻辑必须**不依赖任何包**即可验证。
    """
    manifest = json.loads((APP_DIR / "package.json").read_text(encoding="utf-8"))
    assert manifest["scripts"]["test"] == "node --test"
    assert manifest.get("dependencies", {}) == {}, (
        "后台判定逻辑不得引入运行时依赖——它必须在无 registry 的环境里也能验证"
    )
    assert manifest.get("devDependencies", {}) == {}


def test_status_module_does_not_import_any_package():  # A14
    """`status.js` 与 `view.js` 的 import 必须只指向相对路径。

    这是"零依赖"的机械判据：出现裸模块名（如 `import React from "react"`）
    就意味着构建需要外部包。
    """
    import re

    for name in ("src/status.js", "src/view.js"):
        source = (APP_DIR / name).read_text(encoding="utf-8")
        specifiers = re.findall(r'^\s*import\s[^;]*?from\s+["\']([^"\']+)["\']', source, re.MULTILINE)
        for specifier in specifiers:
            assert specifier.startswith(".") or specifier.startswith("node:"), (
                f"{name} 引入了外部模块 {specifier!r}——判定层必须零依赖"
            )


@pytest.mark.skipif(NODE is None, reason="本机无 node：后台判定逻辑未在本轮验证（不算通过）")
def test_node_suite_passes():  # A14
    """实际跑 `node --test`，把它接进主门禁。"""
    result = subprocess.run(
        [NODE, "--test"],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = "\n".join((result.stdout or "").splitlines()[-25:])
    assert result.returncode == 0, (
        "管理后台状态契约套件失败\n" f"--- node --test 输出（尾部）---\n{tail}"
    )
    # 输出里必须真的跑了用例（防止"0 个用例也算通过"）
    assert "# fail 0" in (result.stdout or ""), f"未看到通过计数，可能没跑用例：\n{tail}"
    assert "# pass 0" not in (result.stdout or ""), f"一个用例都没通过：\n{tail}"


@pytest.mark.skipif(NODE is None, reason="本机无 node：A14 在后台侧的验证未执行（不算通过）")
def test_node_suite_covers_the_critical_counterexamples():  # A14
    """门禁必须**真的**包含那几条反例——否则套件名存实亡。

    这里直接读测试文件的源码，断言几条关键判据在场：
    没有它们，"只有权威回执能说已过账"这条压倒性原则就没有机器守护。
    """
    source = (APP_DIR / "test" / "status.test.mjs").read_text(encoding="utf-8")
    view_source = (APP_DIR / "test" / "view.test.mjs").read_text(encoding="utf-8")
    required = (
        "编排已完成但没有回执",
        "未经权威验证",
        "UNKNOWN",
        "状态来源与更新时间",
    )
    missing = [item for item in required if item not in source]
    assert not missing, f"状态契约套件缺少关键反例：{missing}"
    # 列表页那条（"已过账"的数字不得含仅编排成功）在 view 套件里
    assert "仅编排成功" in view_source, (
        "列表页套件缺少关键反例：『已过账』的计数不许包含仅编排成功的任务"
    )


@pytest.mark.skipif(NODE is None, reason="本机无 node：批准页判定未在本轮验证（不算通过）")
def test_approval_suite_covers_the_critical_counterexamples():  # A06
    """批准页的门禁必须**真的**包含四条关键判据。

    没有它们，"不把自然语言当授权"与"缺数据说缺"就没有机器守护——
    而这两条一旦失守，界面上会把**未管控**显示成**已管控**。
    """
    approval_source = (APP_DIR / "test" / "approval.test.mjs").read_text(encoding="utf-8")
    required = (
        "不构成授权",                    # 自然语言不得冒充授权
        "不得当作可批准",                # 缺数据的口径
        "没有任何输入能得出 DECIDABLE",   # 诚实性：数据未就位就不许显示可决定
        "事实追加不可变",                # 已决定的不得再显示可决定
    )
    missing = [item for item in required if item not in approval_source]
    assert not missing, f"批准页套件缺少关键反例：{missing}"
