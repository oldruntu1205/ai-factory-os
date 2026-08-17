"""
agents/quality.py —— AI 质量分析 Agent（多步工作流版）

进化点（自进化 Phase 1→2 衔接）：
- 从「单步 LLM」升级为「先调 MES/经营 Tool 取实时数据，再 LLM 分析」的多步工作流；
- tools 来自 HermesRuntime 注入的 ToolCenter（runtime.execute 已埋点），
  每次 Tool 调用都会自动写进执行轨迹，GEPA 才有真实多步信号可学。
- 兼容旧签名 quality_agent(data, company_id)，tools 缺省时退化为原单步行为。
"""
from ai.llm import get_llm
from hermes.registry.soul import read_soul


def _gather_quality_data(tools, company_id: int) -> str:
    """多步：先查 MES 实时生产状态、经营快照、ERP 物料库存，拼成实时数据块。"""
    parts = []
    try:
        mes = tools.execute("mes.get_production", {"order_id": ""}, company_id)
        if mes.get("ok"):
            parts.append(f"[MES 实时生产状态] {mes.get('data')}")
    except Exception as e:
        parts.append(f"[MES 调用失败] {e}")
    try:
        snap = tools.execute("dashboard.snapshot", {}, company_id)
        if snap.get("ok"):
            parts.append(f"[经营快照] {snap.get('data')}")
    except Exception as e:
        parts.append(f"[经营快照调用失败] {e}")
    try:
        stock = tools.execute("erp.get_stock", {"product_id": ""}, company_id)
        if stock.get("ok"):
            parts.append(f"[ERP 物料库存] {stock.get('data')}")
    except Exception as e:
        parts.append(f"[ERP 库存调用失败] {e}")
    return "\n".join(parts)


def quality_agent(data: dict, company_id: int = None, tools=None) -> str:
    """data: 质量数据 dict; company_id: 用于读取 Soul; tools: ToolCenter（多步用）"""
    llm = get_llm()
    soul = read_soul(company_id or 1, "quality_agent")

    payload = str(data)
    if tools:
        live = _gather_quality_data(tools, company_id or 1)
        if live:
            payload = payload + "\n\n# 实时工业数据（MES / 经营系统 / ERP）\n" + live

    prompt = f"""
你是制造企业质量分析专家。
质量数据：

{payload}

分析：
1. 是否存在异常（对比实时产线状态）
2. 可能根因（材料/工艺/设备/环境，结合物料库存判断）
3. 整改建议（可落地）
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    return result.content


def run_quality(state: dict) -> dict:
    state["result"] = quality_agent(state.get("quality_data", state["message"]),
                                   state.get("company_id"))
    return state
