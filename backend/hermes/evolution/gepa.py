"""
hermes/evolution/gepa.py —— GEPA 进化引擎（Genetic-Pareto Prompt Evolution）

Hermes 官方自进化核心的本地实现。四阶段闭环：
  read   读取待优化目标当前文本（Soul 文件 / Tool 覆盖 / 技能 workflow）
  mutate 基于真实执行轨迹（尤其失败样本）反思式生成 N 个候选变体（外科手术式，非随机）
  evaluate 用 evaluator 离线回放每条历史 trace，给候选打分并过护栏
  select 选最优且通过全部护栏的变体写 evolution_candidates（status=pending，不自动生效）

所有进化结果必须经人工审批（service.approve_candidate）才会 apply 写回，绝不自动提交。
"""
import logging
import re

from hermes.evolution import store, evaluator

logger = logging.getLogger(__name__)

# Mock 模式下的通用"改进点"模板（保证无 LLM 时也能演示进化闭环）
_MOCK_IMPROVEMENTS = [
    "（GEPA 进化建议）补充：分析前先核对 MES/ERP 数据时间戳与计量单位，再按步骤下结论，避免口径不一致。",
    "（GEPA 进化建议）增加边界：当样本量不足或数据缺失时，明确标注置信度低并建议补数。",
    "（GEPA 进化建议）强化结构：结论统一按「异常发现 / 可能根因 / 可行动建议」三段输出，便于人工复核。",
]

# Phase 4：允许做"代码进化"的工具白名单（仅纯函数演示工具，安全可控）。
# 不在白名单的工具拒绝代码进化，避免改到会触碰外部系统的实现。
TOOL_CODE_EVOLVABLE = {
    "erp.get_stock", "mes.get_production", "dashboard.snapshot",
}


def _builtin_tool_desc(name: str) -> str:
    """回退读取 hermes/tool/tools.py 中内置的 Tool 描述。"""
    try:
        from hermes.tool.tools import BUILTIN_TOOLS
    except Exception:
        return ""
    v = BUILTIN_TOOLS.get(name)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("description", "")
    if hasattr(v, "description"):
        return v.description
    return ""


def _failure_context(traces: list, n: int = 5) -> str:
    """抽取低分/出错的轨迹作为"失败样本"，喂给 mutate 找根因。"""
    bad = [t for t in traces
           if (getattr(t, "error", None)) or (getattr(t, "quality_score", None) is not None
                                              and t.quality_score < 0.5)]
    bad = bad[:n]
    if not bad:
        bad = traces[:n]
    blocks = []
    for i, t in enumerate(bad, 1):
        inp = (t.task_input or "")[:200]
        out = (t.final_output or "")[:300]
        err = (t.error or "")[:200]
        blocks.append(f"[失败样本{i}] 输入:{inp}\n输出:{out}\n错误:{err}")
    return "\n---\n".join(blocks) if blocks else "（暂无失败样本，使用常规样本）"


class GEPAEngine:
    def read_target(self, company_id: int, agent_name: str,
                    target_type: str, target_key: str) -> str:
        """读取待优化目标的当前文本。"""
        if target_type == "soul":
            from hermes.registry.soul import read_soul_base
            # 进化只读取/改进「底座层」，绝不触碰企业覆盖层，避免互相覆盖
            return read_soul_base(company_id, target_key or agent_name)
        if target_type == "skill":
            rec = store.get_skill_by_key(company_id, agent_name, target_key)
            return rec.workflow_md if rec else ""
        if target_type == "tool":
            return store.get_tool_override(company_id, target_key) or _builtin_tool_desc(target_key)
        if target_type == "tool_code":
            from hermes.tool.tools import BUILTIN_TOOLS
            t = BUILTIN_TOOLS.get(target_key)
            if not t or not hasattr(t, "_func"):
                return ""
            try:
                import inspect
                return inspect.getsource(t._func)
            except Exception:
                return getattr(t._func, "__name__", "") or ""
        raise ValueError(f"未知 target_type: {target_type}")

    # ---------- mutate ----------

    def mutate(self, company_id: int, agent_name: str, target_type: str,
               current_text: str, traces: list, n_variants: int = 3) -> list:
        """生成 N 个候选变体，返回 [(candidate_text, rationale), ...]。"""
        from ai.llm import get_llm, is_mock
        if is_mock():
            return self._mutate_mock(current_text, target_type, n_variants)

        try:
            llm = get_llm()
            fails = _failure_context(traces)
            type_hint = {
                "soul": "系统提示（Markdown 章节结构，保持 ## 标题）",
                "skill": "SKILL 工作流（保持 ## Trigger / ## Workflow 结构）",
                "tool": "Tool 一句话描述（中文，不超过 500 字）",
            }.get(target_type, "文本")
            prompt = (
                f"你是制造业 AI 平台的 GEPA 进化器。请基于该员工的历史失败样本，"
                f"对其 {type_hint} 做**针对性改进**（外科手术式，不要重写整体、不要改变工具名/结构键/原目的）。\n\n"
                f"当前文本:\n{current_text[:2000]}\n\n"
                f"历史失败/低效样本:\n{fails}\n\n"
                f"请生成 {n_variants} 个候选变体。每个必须：保持原语言(中文)、原目的、不改变标识；"
                f"针对失败样本真正修正问题。\n\n"
                "输出格式（严格）：\n"
                f"=== 候选1 ===\n<完整新文本>\n理由: <一句话改动理由>\n"
                f"=== 候选2 ===\n...\n"
            )
            out = llm.invoke(prompt).content
            return self._parse_variants(out, current_text, n_variants)
        except Exception as e:
            logger.warning("GEPA mutate LLM 失败，回退 Mock: %s", e)
            return self._mutate_mock(current_text, target_type, n_variants)

    def _mutate_mock(self, current_text: str, target_type: str,
                     n_variants: int) -> list:
        variants = []
        for i in range(min(n_variants, len(_MOCK_IMPROVEMENTS))):
            if target_type == "tool":
                # Tool 描述受 500 字限制，改用精简替换而非追加
                improved = (current_text[:420] + "（更强调返回字段与单位一致性）").strip()
            else:
                improved = current_text.rstrip() + "\n\n" + _MOCK_IMPROVEMENTS[i]
            variants.append((improved, _MOCK_IMPROVEMENTS[i]))
        return variants

    def _parse_variants(self, text: str, current_text: str,
                        n_variants: int) -> list:
        """解析 LLM 输出的多个候选变体。"""
        parts = re.split(r"===+\s*候选\s*\d+\s*===+", text)
        variants = []
        for p in parts[1:]:
            rat_m = re.search(r"理由\s*[:：]\s*(.+)", p)
            rationale = rat_m.group(1).strip() if rat_m else ""
            body = re.sub(r"理由\s*[:：].*", "", p, flags=re.S).strip()
            if body:
                variants.append((body, rationale))
        if not variants:
            # 退化：整段当 1 个候选
            body = re.sub(r"理由\s*[:：].*", "", text, flags=re.S).strip()
            if body:
                variants.append((body, ""))
        return variants[: max(1, n_variants)]

    # ---------- run ----------

    def run(self, company_id: int, agent_name: str, target_type: str,
            target_key: str = None, n_variants: int = 3) -> dict:
        """执行一次完整进化：read→mutate→evaluate→select→propose(pending)。

        方案3：在 run 启动即捕获 Soul 快照与版本指纹，作为本轮评估的固定基准；
        之后 soul 再变也不会让本轮的 baseline/candidate 比较变成"移动靶"。
        """
        # Phase 4：工具代码进化走独立的「隔离测试」路径（不走文本评分）
        if target_type == "tool_code":
            return self._run_tool_code(company_id, agent_name, target_key, n_variants)

        # 评估基准 Soul 快照（方案3）：锁定本轮人格上下文
        from hermes.registry.soul import read_soul, soul_version
        soul_snapshot = read_soul(company_id, agent_name)
        sv = soul_version(company_id, agent_name)

        if target_type == "soul" and not target_key:
            target_key = agent_name

        current = self.read_target(company_id, agent_name, target_type, target_key)
        if not current.strip():
            return {"ok": False, "reason":
                    f"目标不存在或无内容（{target_type}:{target_key}），请先沉淀技能/配置 Soul"}

        traces = store.list_traces(company_id, agent_name=agent_name, limit=50)
        variants = self.mutate(company_id, agent_name, target_type, current,
                               traces, n_variants)

        best = None
        for cand_text, rationale in variants:
            base, score, gr = evaluator.evaluate_variant(
                company_id, agent_name, target_type, current, cand_text, traces,
                soul_snapshot=soul_snapshot)
            improved = evaluator.is_improvement(base, score)
            logger.info("GEPA 候选评估: agent=%s type=%s base=%.3f cand=%.3f pass=%s",
                        agent_name, target_type, base, score, gr["all_pass"])
            if gr["all_pass"] and improved and (best is None or score > best[3]):
                best = (cand_text, rationale, base, score, gr)

        if best is None:
            return {"ok": False, "reason":
                    "未产生通过护栏且优于基线的候选（可能护栏拦截或分数无提升）",
                    "variants_tried": len(variants)}

        cand_text, rationale, base, score, gr = best
        rec = store.save_candidate(
            company_id=company_id, agent_name=agent_name,
            target_type=target_type, target_key=target_key,
            current_text=current, candidate_text=cand_text,
            rationale=rationale, baseline_score=base,
            eval_score=score, guardrails=gr, soul_version=sv)
        return {
            "ok": True,
            "candidate_id": rec.id,
            "target_type": target_type,
            "target_key": target_key,
            "baseline_score": base,
            "eval_score": score,
            "lift": round(score - base, 4),
            "guardrails_pass": gr["all_pass"],
            "status": "pending",
            "note": "候选已写入待审批队列，需人工 approve 后才生效",
        }

    # ---------- Phase 4: 工具代码进化（Darwinian Evolver 本地版）----------

    def _run_tool_code(self, company_id: int, agent_name: str,
                       tool_name: str, n_variants: int = 3) -> dict:
        """对白名单内纯函数 Tool 做代码进化：读源码→生成候选→隔离测试→propose。"""
        if tool_name not in TOOL_CODE_EVOLVABLE:
            return {"ok": False, "reason":
                    f"工具 [{tool_name}] 不在代码进化白名单（仅 {sorted(TOOL_CODE_EVOLVABLE)}）"}
        current = self.read_target(company_id, agent_name, "tool_code", tool_name)
        if not current.strip():
            return {"ok": False, "reason": f"无法读取工具源码: {tool_name}"}

        traces = store.list_traces(company_id, agent_name=agent_name, limit=50)
        variants = self._mutate_tool_code(current, tool_name, traces, n_variants)

        best = None
        for src, rationale in variants:
            passed, report = self._test_tool_code(src, tool_name)
            report_score = report.get("score", 0.0)
            if passed and (best is None
                           or report_score > (best[2] or {}).get("score", 0.0)):
                best = (src, rationale, report)

        if best is None:
            return {"ok": False, "reason":
                    "未产生通过隔离测试的候选代码（可能语法/签名/返回结构不符）",
                    "variants_tried": len(variants)}

        src, rationale, report = best
        from hermes.registry.soul import soul_version
        sv = soul_version(company_id, agent_name)
        rec = store.save_candidate(
            company_id=company_id, agent_name=agent_name,
            target_type="tool_code", target_key=tool_name,
            current_text=current, candidate_text=src,
            rationale=rationale, baseline_score=report.get("baseline_score", 1.0),
            eval_score=report.get("score", 1.0),
            guardrails={"all_pass": report.get("passed", False),
                        "test_report": report},
            soul_version=sv)
        return {
            "ok": True,
            "candidate_id": rec.id,
            "target_type": "tool_code",
            "target_key": tool_name,
            "eval_score": report.get("score"),
            "guardrails_pass": report.get("passed"),
            "status": "pending",
            "note": "代码进化候选已生成，需人工审批；应用仅产出提案文件，不热替换运行代码",
        }

    def _mutate_tool_code(self, current_src: str, tool_name: str,
                          traces: list, n_variants: int = 3) -> list:
        from ai.llm import get_llm, is_mock
        if is_mock():
            return self._mutate_tool_code_mock(current_src, tool_name, n_variants)
        try:
            llm = get_llm()
            fails = _failure_context(traces)
            prompt = (
                f"你是制造业 AI 平台的 GEPA 代码进化器。下面是一段 Tool 实现（纯函数，"
                f"签名固定为 def _impl(params, company_id) -> dict）。请在不改变函数签名、"
                f"不改变对外返回结构（仍是 dict）的前提下，生成 {n_variants} 个改进实现"
                f"（增强容错/日志/数据校验，或修复已知失败样本中的问题）。\n\n"
                f"当前源码:\n{current_src}\n\n历史失败样本:\n{fails}\n\n"
                f"请输出 {n_variants} 个完整函数，每个以 === 候选N === 起头，"
                f"其后为完整源码，再写 理由: <改进点>\n"
            )
            out = llm.invoke(prompt).content
            return self._parse_variants(out, current_src, n_variants)
        except Exception as e:
            logger.warning("tool_code mutate LLM 失败，回退 Mock: %s", e)
            return self._mutate_tool_code_mock(current_src, tool_name, n_variants)

    def _mutate_tool_code_mock(self, current_src: str, tool_name: str,
                               n_variants: int = 1) -> list:
        improved = (current_src.rstrip()
                    + "\n    # [GEPA 代码进化] 增强容错：统一返回结构并记录来源\n")
        return [(improved, "[GEPA] 增强容错与日志：统一返回结构，便于上层稳定解析")]

    def _test_tool_code(self, src: str, tool_name: str) -> tuple:
        """验证候选 Tool 源码：AST 安全校验（安全策略：不执行 LLM 生成的代码）。

        安全说明：Python 无法对任意代码做可靠沙箱执行（空 namespace 仍会注入 __builtins__），
        因此这里改为静态验证：
        1) ast.parse 校验语法；
        2) AST 节点遍历检查危险调用/导入/属性（防空白/别名变体绕过子串匹配）；
        3) 不执行候选函数，仅返回静态验证结论——候选仍只生成 .patch.md 提案，由人工 review。
        """
        import ast
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            return False, {"passed": False, "score": 0.0, "error": f"语法错误: {e}"}

        # 2) AST 危险模式检查（基于语法树，`open ('x')`、`import  os` 等空白变体无法绕过）
        dangerous_calls = {"open", "eval", "exec", "compile", "input", "__import__"}
        dangerous_modules = {"os", "subprocess", "socket", "shutil", "sys",
                             "pickle", "ctypes", "pathlib", "builtins", "requests"}
        dangerous_attrs = {"system", "popen", "remove", "unlink", "rmdir",
                           "makedirs", "walk", "listdir", "glob", "exec_"}
        try:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in dangerous_calls:
                    return False, {"passed": False, "score": 0.0,
                                   "error": f"包含危险调用: {node.func.id}()"}
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    bad = [a.name.split(".")[0] for a in node.names
                           if a.name.split(".")[0] in dangerous_modules]
                    if bad:
                        return False, {"passed": False, "score": 0.0,
                                       "error": f"包含危险模块导入: {', '.join(bad)}"}
                if isinstance(node, ast.Attribute) and node.attr in dangerous_attrs:
                    return False, {"passed": False, "score": 0.0,
                                   "error": f"包含危险属性访问: .{node.attr}"}
        except Exception as e:
            return False, {"passed": False, "score": 0.0, "error": f"安全检查异常: {e}"}

        fn_match = re.search(r"def\s+(\w+)\s*\(", src)
        if not fn_match:
            return False, {"passed": False, "error": "未找到函数定义"}

        # 3) 不执行代码：静态验证通过即视为可生成提案（运行时行为需人工 review）
        return True, {
            "passed": True, "score": 1.0, "baseline_score": 1.0,
            "new_output": "[静态验证通过，未执行（安全策略）]",
            "note": "候选代码未经运行时执行，应用前请人工 review",
        }
