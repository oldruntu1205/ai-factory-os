"""交付面工具的门禁包装：覆盖报告生成器与上线前置检查（上游 §14 / §17）。

## 这两个工具各自的价值

| 工具 | 防什么 | 判据 |
| --- | --- | --- |
| `scripts/render_assertion_report.py` | 手写覆盖报告**必然过期**（实测：写着 75 个引用节点，实际 95；A14 缺口写着"UI 面未接入"早已不成立） | 生成幂等 + 与磁盘报告一致 |
| `scripts/preflight_deploy.py` | "阻塞在目标环境信息"**是个不可执行的结论**——既不能验证也不知道进展 | 仓库侧就绪件齐备 + 阻塞逐条列名 |

**手写证据会漂移，而漂移的证据比没有证据更危险：它看起来权威。**
所以报告改为生成物，并由门禁核对是否过期。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# ① 覆盖报告：生成幂等
# ==========================================================================

def test_assertion_report_generation_is_idempotent():
    """同一份库存必须生成**逐字节相同**的报告（否则"一致"没有意义）。

    不幂等意味着报告里混进了时间戳/随机/字典序之类的东西——
    那样 `--check` 永远报红，门禁会被人为绕过。
    """
    renderer = _load("scripts/render_assertion_report.py", "render_assertion_report")
    first = renderer.render()
    second = renderer.render()
    assert first == second, "生成器不幂等：两次生成结果不同"
    assert first.strip(), "生成了空报告"


def test_assertion_report_has_no_wall_clock_dependence():
    """报告不得依赖墙钟（本项目判定函数不得读墙钟的既有约定）。

    判据：报告里不得出现"今天"的日期字面量。
    """
    renderer = _load("scripts/render_assertion_report.py", "render_assertion_report")
    rendered = renderer.render()
    import datetime

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    assert today not in rendered, (
        f"报告里出现了当天日期 {today} —— 那会让生成结果随日期变化（不幂等）"
    )


# ==========================================================================
# ② 上线前置检查：仓库侧就绪 + 阻塞列名
# ==========================================================================

def test_preflight_reports_repo_readiness_and_lists_blockers():
    """检查器必须同时给出"仓库侧是否就绪"与"阻塞有哪些"，两者不得混淆。"""
    preflight = _load("scripts/preflight_deploy.py", "preflight_deploy")
    result = preflight.collect()

    # 仓库侧：本轮已就绪（缺任何一项都说明交付面破了个洞）
    assert result["artifacts_missing"] == [], (
        f"仓库内部署就绪件缺失：{result['artifacts_missing']}"
    )
    assert result["artifacts_present"], "就绪件列表不应为空"

    # 阻塞：必须逐条列名 + 说明为什么仓库里判定不了
    inputs = result["environment_inputs"]
    assert len(inputs) >= 6, f"目标环境输入过少，像是漏列：{inputs}"
    for item in inputs:
        assert item["input"], item
        assert len(item["why"]) >= 8, f"阻塞项 {item['input']} 未说明为什么需要"


def test_preflight_manifest_reflects_unfrozen_state():
    """清单必须**如实**声明未冻结，且写清阻塞原因。

    生产登记表未冻结时若把 `frozen` 写成 true，等于宣称"可以冻结上线"——
    那是把待冻结的外部依赖说成已完成。
    """
    preflight = _load("scripts/preflight_deploy.py", "preflight_deploy")
    manifest = preflight.collect()["manifest"]
    assert manifest["present"], "部署清单缺失"
    assert manifest["frozen"] in ("false", "False"), (
        f"生产登记表未冻结，manifest.frozen 必须为 false；实际 {manifest['frozen']!r}"
    )
    assert manifest["blocked_by"], "未冻结却缺少 blocked_by —— 阻塞原因必须写下来"


def test_preflight_yaml_comment_is_stripped():
    """YAML 行尾注释不得被当成取值。

    清单里写的是 `frozen: false          # 生产冻结状态：**未冻结**（…）`。
    不去注释就会把整段说明当取值，于是误报"不是布尔字面量"——
    **实测踩过**：检查器第一次运行就是被这行注释叫停的。
    """
    preflight = _load("scripts/preflight_deploy.py", "preflight_deploy")
    assert preflight._strip_yaml_comment("false          # 说明文字") == "false"
    assert preflight._strip_yaml_comment("true") == "true"
    assert preflight._strip_yaml_comment("  1.2.3  # pin  ") == "1.2.3"


def test_preflight_cli_json_is_machine_readable():
    """`--json` 必须输出可解析 JSON，并带明确结论（供部署前门禁消费）。"""
    result = subprocess.run(
        [PYTHON, "scripts/preflight_deploy.py", "--json"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "ready_for_environment_inputs", payload
    assert payload["artifacts_missing"] == []
    assert len(payload["environment_inputs"]) >= 6
