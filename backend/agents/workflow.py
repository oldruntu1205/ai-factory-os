"""
agents/workflow.py —— LangGraph Agent 工作流（第十章）

router -> (customer | sales | quotation)
"""
from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.router import intent_router
from agents.customer import run_customer
from agents.sales import run_sales
from agents.quotation import run_quote

# LangGraph 节点要求可调用对象，这里用状态里的 intent 字段直接路由
def _router_node(state: dict) -> dict:
    state["intent"] = intent_router(state)
    return state


def _route(state: dict) -> str:
    return state.get("intent", "customer")


def build_workflow():
    workflow = StateGraph(AgentState)

    workflow.add_node("router", _router_node)
    workflow.add_node("customer", run_customer)
    workflow.add_node("sales", run_sales)
    workflow.add_node("quotation", run_quote)

    workflow.set_entry_point("router")
    workflow.add_conditional_edges(
        "router",
        _route,
        {
            "customer": "customer",
            "sales": "sales",
            "quotation": "quotation",
        },
    )
    workflow.add_edge("customer", END)
    workflow.add_edge("sales", END)
    workflow.add_edge("quotation", END)

    return workflow.compile()


app = build_workflow()
