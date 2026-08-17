"""
agents/customer.py —— 客服 Agent（第七章）

结合企业知识库（RAG）回答客户问题。
Soul（人设/系统提示）由企业在后台编辑，注入到 prompt 头部。

检索策略（与工艺助手对齐）：
- 复用 knowledge.hybrid.hybrid_search（稠密向量 + BM25 关键词 + RRF 融合），
  让「型号/规格/材质」类精确查询也能被关键词命中，避免纯语义检索漏召回。
- 返回片段携带来源文件，拼接进 prompt 并在答复末尾给出「参考来源」，强化「不编造」约束。
"""
from knowledge.hybrid import hybrid_search
from ai.llm import get_llm
from hermes.registry.soul import read_soul


def customer_agent(question: str, company_id: int, score_threshold: float = None) -> str:
    """RAG 客服：混合检索知识库 -> 注入上下文 + Soul -> 大模型生成（带引用）"""
    results = hybrid_search(question, company_id, limit=5, score_threshold=score_threshold)
    if results:
        context = "\n\n".join(
            f"[来源: {r.get('source') or '知识库'}]\n{r['text']}" for r in results)
        citations = "\n".join(f"- {r.get('source') or '知识库'}" for r in results)
    else:
        context = "（企业知识库暂无相关资料）"
        citations = ""

    # Soul 作为系统提示（人设/语气/边界）—— 企业可自定义
    soul = read_soul(company_id, "customer_agent")
    # 多语种：检测客户提问语言，用同语言回答（出口场景客户多为外语）
    from utils.i18n import detect_language, LANGUAGE_NAMES
    lang = detect_language(question)
    lang_name = LANGUAGE_NAMES.get(lang, "English")
    prompt = f"""你是企业 AI 客服。
只能根据以下企业资料回答，不能编造资料中没有的信息。
如果资料中没有答案，请礼貌地说明并建议联系人工客服。
用{lang_name}回答客户问题（客户用什么语言提问，就用什么语言回答）。

企业资料：
{context}

客户问题：
{question}
"""
    full_prompt = f"{soul}\n\n---\n\n{prompt}" if soul else prompt

    llm = get_llm()
    result = llm.invoke(full_prompt)
    answer = result.content

    # 末尾追加可点击的参考来源（即使 MockLLM 也能稳定展示出处）
    if citations:
        answer = f"{answer}\n\n参考来源：\n{citations}"
    return answer


def run_customer(state: dict) -> dict:
    """LangGraph 节点：调用客服 Agent 并写回状态"""
    state["result"] = customer_agent(
        state.get("message", ""),
        state.get("company_id"),
        state.get("score_threshold"),
    )
    return state
