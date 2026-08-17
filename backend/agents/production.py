"""
agents/production.py —— AI 生产计划 Agent（多步工作流版）

进化点（自进化 Phase 1→2 衔接）：
- 从「单步 LLM」升级为「先查 MES 产线/工单、ERP 库存，再排产分析」的多步工作流；
- tools 来自 HermesRuntime 注入的 ToolCenter（runtime.execute 已埋点），
  每次 Tool 调用都会自动写进执行轨迹，GEPA 才有真实多步信号可学。
- 兼容旧签名 production_agent(data, company_id)，tools 缺省时退化为原单步行为。
"""
from ai.llm import get_llm
from hermes.registry.soul import read_soul


def _gather_production_data(tools, company_id: int) -> str:
    """多步：先查 MES 产线/工单、ERP 库存，拼成实时数据块。"""
    parts = []
    try:
        mes = tools.execute("mes.get_production", {"order_id": ""}, company_id)
        if mes.get("ok"):
            parts.append(f"[MES 产线/工单状态] {mes.get('data')}")
    except Exception as e:
        parts.append(f"[MES 调用失败] {e}")
    try:
        stock = tools.execute("erp.get_stock", {"product_id": ""}, company_id)
        if stock.get("ok"):
            parts.append(f"[ERP 库存] {stock.get('data')}")
    except Exception as e:
        parts.append(f"[ERP 库存调用失败] {e}")
    return "\n".join(parts)


def production_agent(data: dict, company_id: int = None, tools=None) -> str:
    """data: 生产数据 dict; company_id: 用于读取 Soul; tools: ToolCenter（多步用）"""
    llm = get_llm()
    soul = read_soul(company_id or 1, "production_agent")

    payload = str(data)
    if tools:
        live = _gather_production_data(tools, company_id or 1)
        if live:
            payload = payload + "\n\n# 实时工业数据（MES / ERP）\n" + live

    prompt = f"""
你是制造企业生产计划分析专家。
生产数据：

{payload}

分析：
1. 是否存在延期风险（结合实时产线进度）
2. 生产瓶颈在哪条产线（结合库存判断）
3. 优化建议（可落地）
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    return result.content
