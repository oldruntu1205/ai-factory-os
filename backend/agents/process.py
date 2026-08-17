"""
agents/process.py —— 工艺/图纸助手 Agent（多步工作流版）

定位：面向车间/工艺/质检人员，回答「某型号产品的公差范围、材质标准」，
并主动提示易错点与禁止操作，防范工人误操作。

检索策略（双轨，保证精确度）：
1. 结构化快路径：命中 DrawingParam 表（按产品型号/参数名/值精确查），
   这是「问 XK-205 外径公差」能得到精确值的关键。
2. 混合检索：稠密向量 + BM25 关键词（RRF 融合），覆盖自由描述类问题。

进化点（与 production/quality 一致）：
- tools 来自 HermesRuntime 注入的 ToolCenter（runtime.execute 已埋点），
  多步调用自动进执行轨迹，供 GEPA 自进化学习。
- tools 缺省时退化为「检索 + LLM 合成」单步行为。
"""
import re

from ai.llm import get_llm
from hermes.registry.soul import read_soul
from knowledge.hybrid import hybrid_search, search_drawing_params
from knowledge.cad_parser import extract_product_models


def _gather_live_data(tools, company_id: int) -> str:
    """可选多步：查 MES 工艺参数 / ERP 物料标准，拼成实时数据块。"""
    if not tools:
        return ""
    parts = []
    for tool_name in ("mes.get_process", "erp.get_material_std"):
        try:
            res = tools.execute(tool_name, {}, company_id)
            if res.get("ok"):
                parts.append(f"[{tool_name}] {res.get('data')}")
        except Exception as e:
            parts.append(f"[{tool_name} 调用失败] {e}")
    return "\n".join(parts)


def process_agent(message: str, company_id: int, tools=None, db=None) -> str:
    """message: 用户问题；company_id: 企业隔离（必传，禁止默认值，防止跨企业检索）；
    tools: ToolCenter；db: 可选会话。
    """
    if not company_id:
        raise ValueError("process_agent 需要显式传入 company_id，禁止缺失（防止跨企业检索）")
    if not message or not message.strip():
        return "请描述您要查询的工艺/图纸问题，例如「XK-205 的外径公差和材质标准是什么？」"
    llm = get_llm()
    soul = read_soul(company_id, "process_agent")

    # 1) 结构化快路径：先按问题中出现的型号/关键词精确查
    structured = []
    models_in_query = extract_product_models(message)
    if db is not None:
        # 先按完整问题查（覆盖「公差」「材质」等参数名）
        structured = search_drawing_params(message, company_id, db, limit=20)
        # 再按抽取出的型号逐个补全
        for m in models_in_query:
            extra = search_drawing_params(m, company_id, db, limit=20)
            for row in extra:
                if row["id"] not in {s["id"] for s in structured}:
                    structured.append(row)

    # 2) 混合检索：自由描述类上下文
    rag = hybrid_search(message, company_id, limit=5)
    rag_text = "\n\n".join(
        f"[来源: {r.get('source') or '知识库'}]\n{r['text']}" for r in rag
    ) if rag else "（知识库无相关片段）"

    # 3) 可选实时数据
    live = _gather_live_data(tools, company_id)

    # 组织结构化参数上下文
    if structured:
        param_lines = []
        for s in structured:
            tag = f"[{s['category']}] {s['param_name']}"
            model = f" 型号 {s['product_model']}" if s.get("product_model") else ""
            src = f" (来源: {s['source_file']}{('/'+s['location']) if s.get('location') else ''})"
            param_lines.append(f"- {tag}: {s['value']}{model}{src}")
        param_block = "结构化图纸参数（精确抽取）：\n" + "\n".join(param_lines)
    else:
        param_block = "结构化图纸参数：未匹配到精确参数（已转混合检索）。"

    prompt = f"""
你是制造企业「工艺/图纸助手」，服务车间、工艺与质检人员。
你的首要职责是**防范工人误操作**：必须基于图纸真实参数给出公差范围、材质标准，
不得编造；对易错/禁止操作要明确警示。

# 用户问题
{message}

# 结构化图纸参数（优先采用，来自图纸/PDF/BOM 精确抽取）
{param_block}

# 混合检索上下文（自由描述类补充）
{rag_text}
{('# 实时工业数据（MES / ERP）\n' + live) if live else ''}

# 回答要求
1. 若问具体型号的参数：先给「精确值」（公差范围 / 材质牌号 / 执行标准），并标注来源文件。
2. 主动给出「防误操作提示」：易错点、禁止操作、与该参数相关的装配/加工注意。
3. 若结构化参数与检索上下文冲突，以结构化参数为准，并提示「建议核对原图」。
4. 信息不足时，明确说明「未在已上传图纸中找到该型号此参数」，并建议上传对应图纸/DXF。
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt
    result = llm.invoke(full_prompt)
    return result.content


# 供 LangGraph 节点使用
def run_process(state: dict) -> dict:
    """从 state 取消息/企业，并自取 DB 会话、透传 tools，保证结构化快路径可用。"""
    from database import SessionLocal
    message = state.get("message", "")
    company_id = state.get("company_id")
    if not company_id:
        raise ValueError("run_process 缺少 company_id（工作流必须显式提供企业上下文）")
    tools = state.get("tools")
    db = state.get("db") or SessionLocal()
    try:
        state["result"] = process_agent(message, company_id, tools=tools, db=db)
    finally:
        if not state.get("db"):
            db.close()
    return state
