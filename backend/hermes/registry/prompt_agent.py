"""
hermes/registry/prompt_agent.py —— 自定义 Agent 的 prompt 驱动编排（注册即可用）

自定义注册的 Agent（带 system_prompt）执行流程：
1. 组装提示词：系统提示词 + 可用工具清单（名称/描述/权限）+ 用户任务
2. LLM 输出 JSON 工具调用序列：[{"tool": "crm.get_customer", "params": {...}}, ...]
3. 逐个执行工具（ToolCenter 内已做 RBAC 权限校验 + 轨迹记录）
4. 把工具结果回填给 LLM 生成最终答复

特点：
- 无需改代码：注册时填 system_prompt + tools 即可定义新 AI 员工
- 工具调用由 LLM 决策，工具执行走统一 ToolCenter（权限/轨迹天然继承）
- 失败容错：工具调用解析失败/执行失败不中断，最终仍由 LLM 汇总
"""
import json
import logging

logger = logging.getLogger(__name__)

TOOL_CALL_PROMPT = """
你是一个智能体，可以使用下列工具完成任务。
工具清单（名称 + 说明 + 所需权限）：
{tools}

任务：
{message}

执行要求：
1. 判断是否需要调用工具：需要时，输出严格的 JSON 数组（不要 Markdown 代码块、不要多余文字）：
   [{{"tool": "工具名", "params": {{...参数...}}}}]
2. 不需要工具时，直接回答。
3. 只允许调用上方工具清单里的工具，不得编造工具名。
"""


def _tool_desc_block(tools_desc: list) -> str:
    if not tools_desc:
        return "（当前 Agent 未配置任何工具）"
    lines = []
    for t in tools_desc:
        perms = ",".join(t.get("permissions", [])) or "无"
        lines.append(f"- {t['name']}: {t.get('description', '')}（权限: {perms}）")
    return "\n".join(lines)


def _extract_json_calls(content: str) -> list:
    """从 LLM 输出中提取工具调用 JSON 数组（容忍 markdown 包裹/前后说明文字）"""
    content = (content or "").strip()
    # 去掉 markdown 代码块围栏
    if content.startswith("```"):
        content = content.split("```", 2)[1] if content.count("```") >= 2 else content
        content = content.strip()
    start = content.find("[")
    end = content.rfind("]")
    if start >= 0 and end > start:
        try:
            arr = json.loads(content[start:end + 1])
            if isinstance(arr, list):
                return [c for c in arr if isinstance(c, dict) and c.get("tool")]
        except Exception:
            return []
    return []


def prompt_agent_handler(message: str, runtime_ctx: dict) -> str:
    """自定义 Agent 执行入口：LLM 决策工具调用 → 执行 → 汇总答复"""
    manifest = runtime_ctx.get("manifest")
    if not manifest:
        return "⚠️ Agent 运行时上下文缺少 manifest，无法执行"

    system_prompt = getattr(manifest, "system_prompt", "") or \
        "你是企业 AI 员工，根据任务调用可用工具完成任务。"
    tool_center = runtime_ctx.get("tools")

    # 组装工具描述（仅该 Agent 声明的工具）
    allowed = set(getattr(manifest, "tools", []) or [])
    tools_desc = []
    if tool_center:
        all_tools = tool_center.list_tools()
        for t in all_tools:
            if not allowed or t["name"] in allowed:
                tools_desc.append(t)

    llm_prompt = (
        f"{system_prompt}\n\n---\n\n" +
        TOOL_CALL_PROMPT.format(tools=_tool_desc_block(tools_desc), message=message)
    )

    from ai.llm import get_llm
    llm = get_llm()
    first = llm.invoke(llm_prompt)
    content = first.content or ""

    calls = _extract_json_calls(content)
    if not calls:
        return content  # LLM 直接回答，无需工具

    # 执行工具调用序列
    results = []
    for call in calls:
        tool_name = call.get("tool")
        params = call.get("params") or {}
        try:
            if tool_center is None:
                results.append({"tool": tool_name, "ok": False, "error": "ToolCenter 未配置"})
                continue
            # ToolCenter.execute 内部做权限校验 + 轨迹记录
            result = tool_center.execute(
                tool_name, params, company_id=runtime_ctx["company_id"],
                agent_name=getattr(manifest, "name", None))
            results.append(result)
        except PermissionError as e:
            results.append({"tool": tool_name, "ok": False, "error": f"权限不足: {e}"})
        except Exception as e:
            results.append({"tool": tool_name, "ok": False, "error": str(e)})

    # 把工具结果交给 LLM 生成最终答复
    summary_prompt = (
        f"{system_prompt}\n\n---\n\n"
        f"任务：{message}\n\n"
        f"你已调用工具，结果如下（JSON）：\n{json.dumps(results, ensure_ascii=False, default=str)}\n\n"
        "请根据工具结果回答任务；若工具失败请说明原因。"
    )
    final = llm.invoke(summary_prompt)
    return final.content or content
