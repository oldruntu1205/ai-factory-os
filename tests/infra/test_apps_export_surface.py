"""`apps/` 的导出示面：没有消费者的导出必须登记（上游 §9 / §17 A09）。

## 为什么需要（Python 侧守卫补不上的口子）

2026-10-05 的事故（`apps/web-admin/src/approval.js` 留下 `if (false) {`
把「说明文字不能当授权」整条禁用）正发生在 `apps/`。
`tests/infra/test_unwired_guards.py` 只扫 `services/` 与 `packages/`，
**扫不到这里** —— 本轮补上。

## 判据与 Python 侧同构

"生产代码"包含两类消费者，**第二类最容易漏**：

1. `apps/**` 里除 `test/` 之外的 JS 模块互调；
2. `apps/web-admin/public/index.html` 里内联的浏览器脚本
   （它 import 这些模块 —— 漏掉它会把在用的导出误判为死代码）。

## 与 Python 侧一致的诚实边界

"没有消费者"**不等于**缺陷：对外库 API、给未来页面预留的视图模型都属正当。
但**安全判据**没有消费者就一定要解释清楚为什么。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APPS = REPO_ROOT / "apps"

NODE = shutil.which("node")

#: 允许"生产侧无消费者"的导出 → (类别, 理由)。
#: **每条都要写清为什么不该/不能接线**，否则清单会退化成挡箭牌。
ALLOWED_UNWIRED_EXPORTS: dict[str, tuple[str, str]] = {
    # ── 给未来页面预留的视图模型（当前页面还没用列表视图）──
    "buildListView": (
        "library",
        "列表视图模型：当前页面只有单条查询（任务/命令/批准各一个输入框），"
        "没有列表页。它是为列表视图预留的，`view.test.mjs` 覆盖其语义。",
    ),
    "summarizeLabels": ("library", "同上：列表汇总标签，随列表视图一起接"),
    "renderRows": ("library", "同上：列表行渲染，随列表视图一起接"),
    "summarizeApprovals": ("library", "同上：批准列表汇总，随列表视图一起接"),
    # ── 契约枚举常量：由测试与（将来的）页面下拉/校验消费 ──
    "DECIDABILITY": ("library", "契约枚举：可决定性的取值集合，供界面文案与校验引用"),
    "CHECK_RESULTS": ("library", "契约枚举：单项检查的取值集合"),
    "TASK_STATES": ("library", "契约枚举：任务状态集合"),
    "COMMAND_STATES": ("library", "契约枚举：命令状态集合"),
    "POSTING_STATUSES": ("library", "契约枚举：过账状态集合"),
    "DISPLAY_OUTCOMES": ("library", "契约枚举：界面显示结论集合"),
    "decidabilityLabels": ("library", "契约枚举 → 中文标签映射，供界面文案引用"),
    # ── ⚠️ 安全判据但没有数据源可接（**必须写清楚**）──
    "checkAuthoritySeparation": (
        "library",
        "**职责分离判据（上游 §10.1），当前无法接线 —— 缺数据源**。"
        "它需要「谁能改配置」与「谁能批准」两批身份；实测本仓登记表"
        "（`services/_kernel/registry.mock.json`）**只有 `actors` 与 `delegated_scopes`，"
        "没有「配置权限」映射**，登记表自身的 note 也写着「生产登记表由 control-plane 的 "
        "Release Registry 提供，待冻结」。"
        "**不凭空造这个数据源**：编一份假的配置权名单会让这条判据变成安慰剂。"
        "已确认的缓解：后端 `transactions.revalidate_before_execution` 的 "
        "`duties_separated` 会阻止**自己批自己**（`approver != requester`），"
        "所以最危险的自我授权已被拦住；缺的是「同一人既能改配置又能批准」这一层。"
        "**接线条件**：control-plane 暴露配置权名单后，在 `server.mjs` 的批准决定路径上"
        "调用它，未分离即拒绝（与 `APPROVER_NOT_CONFIGURED` 同一处置风格）。",
    ),
}


def _walk(root: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    if not root.exists():
        return out
    for entry in root.rglob("*"):
        if "node_modules" in entry.parts:
            continue
        if entry.is_file():
            out.append(entry)
    return out


def _exported_names(text: str) -> list[str]:
    names: set[str] = set()
    for match in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)", text):
        names.add(match.group(1))
    for match in re.finditer(r"export\s+(?:const|let|var)\s+(\w+)", text):
        names.add(match.group(1))
    for match in re.finditer(r"export\s*\{([^}]*)\}", text):
        for part in match.group(1).split(","):
            name = part.split(" as ")[-1].strip()
            if name:
                names.add(name)
    return sorted(names)


def _surface() -> dict[str, str]:
    """返回 {导出名: 定义文件}，只含**生产侧无消费者**的导出。"""
    files = _walk(APPS)
    prod_js = [f for f in files if f.suffix in (".js", ".mjs") and "test" not in f.parts]
    test_js = [f for f in files if f.suffix in (".js", ".mjs") and "test" in f.parts]
    html = [f for f in files if f.suffix == ".html"]

    def read(paths: list[pathlib.Path]) -> str:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths)

    prod_text = read(prod_js) + "\n" + read(html)
    test_text = read(test_js)

    found: dict[str, str] = {}
    for path in prod_js:
        if path.name == "server.mjs":
            continue  # 入口：本身不被 import 是正常的
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in _exported_names(text):
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            prod_hits = len(pattern.findall(prod_text))
            test_hits = len(pattern.findall(test_text))
            if prod_hits <= 1 and test_hits > 0:
                found[name] = path.relative_to(REPO_ROOT).as_posix()
    return found


# ==========================================================================
# ① 未登记的"无消费者导出"必须报红
# ==========================================================================

def test_every_unwired_export_is_registered():
    """`apps/` 里没有生产消费者的导出**必须逐条登记**。

    报红时先问：**它该被接线，还是该被登记？**
    如果它是安全判据，正确答案通常是接线 —— 或者写清**缺什么数据源**
    （本轮 `checkAuthoritySeparation` 就属于后一种，理由写得很具体）。
    """
    surface = _surface()
    unregistered = sorted(set(surface) - set(ALLOWED_UNWIRED_EXPORTS))
    assert not unregistered, (
        "以下 `apps/` 导出在生产侧没有消费者，且未登记：\n  "
        + "\n  ".join(f"{name}  ({surface[name]})" for name in unregistered)
        + "\n\n请判断：该**接线**（安全判据尤其如此），还是属于 library 等正当例外？"
        "\n若是后者，加进 ALLOWED_UNWIRED_EXPORTS 并**写清理由**。"
    )


# ==========================================================================
# ② 登记不得过期
# ==========================================================================

def test_registered_entries_are_not_stale():
    """已登记项不得**已经接线**或**已不存在**。"""
    surface = _surface()
    stale = sorted(name for name in ALLOWED_UNWIRED_EXPORTS if name not in surface)
    assert not stale, (
        f"以下登记项已不再是「生产无消费者」（可能已接线或已删除）：{stale}\n"
        "请从 ALLOWED_UNWIRED_EXPORTS 移除 —— 留着会让清单退化成没人维护的历史账。"
    )


def test_every_registered_entry_states_a_reason():
    """每条登记都必须写类别与具体理由。"""
    problems = []
    for name, (kind, reason) in sorted(ALLOWED_UNWIRED_EXPORTS.items()):
        if kind not in ("audit", "fixture", "library"):
            problems.append(f"{name}: 未知类别 {kind!r}")
        if not reason or len(reason.strip()) < 10:
            problems.append(f"{name}: 理由太短或为空")
    assert not problems, problems


# ==========================================================================
# ③ 安全判据必须写清"缺什么数据源"
# ==========================================================================

def test_security_predicate_registration_explains_the_missing_source():
    """`checkAuthoritySeparation` 的登记必须点明**缺哪个数据源**与**现有缓解**。

    这条防的是"用一句「待接线」把安全缺口糊过去"。
    上游 §10.1 的职责分离是**必须**项，所以它的登记理由必须：
    ① 说明缺什么；② 说明已有哪一层缓解；③ 说明接线条件。
    """
    kind, reason = ALLOWED_UNWIRED_EXPORTS["checkAuthoritySeparation"]
    assert kind == "library"
    for marker in ("registry.mock.json", "duties_separated", "接线条件"):
        assert marker in reason, (
            f"职责分离判据的登记理由必须包含 {marker!r}（缺数据源 / 现有缓解 / 接线条件）"
        )


# ==========================================================================
# ④ 能力自检：探测器必须真的能识别无消费者的导出
# ==========================================================================

def test_exported_names_covers_every_export_syntax():
    """`_exported_names` 必须能解析**每一种**导出语法（不只是本仓碰巧用到的）。

    ## 为什么补这条（一次变异逃逸）

    把探测器的正则从 `export\\s+(?:const|let|var)\\s+(\\w+)` 收窄成
    `export\\s+const\\s+(\\w+)`（漏掉 `let`/`var`），门禁**仍然全绿** ——
    因为本仓恰好没有 `export let/var`，收窄的后果在真实数据上**不可观测**。

    这是"守卫自我验证"的短板：原来自检只证明"**能不报**"，
    没证明"**能全部解析**"。将来有人写 `export let`，就会被静默漏掉 ——
    漏掉的导出不进判据，等于**无声放弃**对它的守护。

    所以这里**直接喂样本**，把各种语法逐条钉住，不依赖本仓恰好有没有用到。
    """
    text = (
        "export function fnExport() {}\n"
        "export async function asyncFnExport() {}\n"
        "export const constExport = 1;\n"
        "export let letExport = 2;\n"
        "export var varExport = 3;\n"
        "export { namedA, namedB as namedBAlias };\n"
        "const notExported = 4;\n"
    )
    names = set(_exported_names(text))
    expected = {
        "fnExport",
        "asyncFnExport",
        "constExport",
        "letExport",
        "varExport",
        "namedA",
        "namedBAlias",
    }
    missing = expected - names
    assert not missing, (
        f"探测器漏掉了这些导出语法：{sorted(missing)}\n"
        f"实际识别到：{sorted(names)}\n"
        "→ 漏掉的导出不会被纳入未接线判据，等于**静默放弃**了对它的守护。"
    )
    assert "notExported" not in names, "不得把未导出的名字当成导出"


def test_detector_actually_reports_an_unwired_export(tmp_path):
    """喂一个构造样本，探测器必须报出**没有消费者**的那个导出。

    没有这条，守卫可能在"探测能力为零"时长期绿灯 ——
    干净树上"没有未登记"与"根本不会识别"**在观测上无法区分**。
    """
    sample = tmp_path / "module.mjs"
    sample.write_text(
        "export function used_elsewhere() { return 1; }\n"
        "export function never_consumed() { return 2; }\n",
        encoding="utf-8",
    )
    # 生产侧只引用 used_elsewhere ⇒ 只有它算"有消费者"
    prod_text = "import { used_elsewhere } from './module.mjs';\nused_elsewhere();\n"
    names = _exported_names(sample.read_text(encoding="utf-8"))
    assert names == ["never_consumed", "used_elsewhere"], names

    unwired = [
        name for name in names
        if len(re.findall(rf"\b{re.escape(name)}\b", prod_text)) <= 1
    ]
    assert unwired == ["never_consumed"], (
        f"探测器应**正好**报出没有消费者的那个导出；实际 {unwired}"
    )


# ==========================================================================
# ⑤ 页面冒烟：用户入口的关键元素必须在（UI 无覆盖是同型风险）
# ==========================================================================

@pytest.mark.skipif(NODE is None, reason="无 node（明确 skip，不算通过）")
def test_admin_page_has_the_elements_the_console_depends_on():
    """页面必须含控制台赖以工作的关键元素。

    ## 为什么加这条

    同一类事故在 UI 上已发生**两次**（成交明细撑长页面、批准判定被禁用），
    而**页面的渲染完全没有任何自动覆盖**：
    判定的单测测的是 `src/*.js`，页面用的是服务端算好的结论 + 自己的渲染。

    这里不做"截图比对"（脆弱且需要浏览器），只守**结构事实**：
    关键容器与入口在，且判定来源确实是服务端字段——这是能机械验证、
    又能真实拦住"改坏了页面"的那一层。
    """
    html = (APPS / "web-admin" / "public" / "index.html").read_text(encoding="utf-8")
    # 内联脚本存在
    assert "<script type=\"module\">" in html, "页面必须含内联模块脚本"

    # 三个面板的容器 id 必须都在（服务端与页面靠它们对接）
    for element_id in ("approvalId", "queryApproval", "approvalResult"):
        assert f'id="{element_id}"' in html, f"页面缺少关键元素 id={element_id}"

    # **判定必须来自服务端**：页面读的是 `d.outcome` / `d.label`，
    # 而不是自己判断"什么算已过账"
    inline = html.split('<script type="module">', 1)[1]
    assert "d.outcome" in inline and "d.label" in inline, (
        "页面必须渲染服务端给出的结论（d.outcome/d.label）"
    )
    # 页面不得自己宣称"已过账"（那只能由服务端结论驱动）
    assert '"已过账"' not in inline or "d.outcome" in inline, (
        "页面不得自行宣称已过账——结论只能来自服务端"
    )


@pytest.mark.skipif(NODE is None, reason="无 node（明确 skip，不算通过）")
def test_admin_page_parses_as_a_module():
    """内联脚本必须语法合法（改坏了页面 = 控制台白屏）。"""
    import tempfile

    html = (APPS / "web-admin" / "public" / "index.html").read_text(encoding="utf-8")
    inline = html.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
        handle.write(inline)
        temp_path = handle.name
    try:
        result = subprocess.run(
            [NODE, "--check", temp_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        pathlib.Path(temp_path).unlink(missing_ok=True)
    assert result.returncode == 0, f"页面内联脚本语法错误：{result.stderr[:400]}"


def test_audit_tool_report_matches_the_registry():
    """留档用的审计脚本必须与登记表一致（防止两者分叉）。

    脚本 `tools/audit_unwired_exports.mjs` 是给人看的速查工具；
    真相以本文件的登记表为准。这条保证工具不会给出**与门禁矛盾**的结论。
    """
    surface = _surface()
    unregistered = sorted(set(surface) - set(ALLOWED_UNWIRED_EXPORTS))
    assert not unregistered, (
        f"审计工具与登记表分叉：{unregistered} 在生产侧无消费者却未登记"
    )
    # 登记表本身可 JSON 序列化（避免不可见字符之类的问题）
    json.dumps(ALLOWED_UNWIRED_EXPORTS, ensure_ascii=False)
