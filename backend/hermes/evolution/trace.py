"""
hermes/evolution/trace.py —— 执行轨迹采集（Phase 0 地基）

Hermes 自进化的前提是"有轨迹可学"。本模块提供一个轻量的 TraceRecorder，
通过 contextvars 在当前请求/任务流内传递，让深层调用的 Tool 也能把步骤回写到同一条轨迹，
无需改动每个 Agent 的函数签名。

用法：
    rec = TraceRecorder(company_id, agent_name, session_id, task_input)
    set_recorder(rec)
    ... 业务执行（期间 ToolCenter.execute 会 record_tool）...
    rec.set_output(result)
    store.save_trace(rec.to_dict())
    clear_recorder()
"""
import time
import uuid
from contextvars import ContextVar
from typing import Optional

# 当前执行流内的轨迹记录器（无则 None）
_trace_ctx: ContextVar[Optional["TraceRecorder"]] = ContextVar("hermes_trace", default=None)


def get_recorder() -> Optional["TraceRecorder"]:
    return _trace_ctx.get()


def set_recorder(rec: "TraceRecorder"):
    _trace_ctx.set(rec)


def clear_recorder():
    _trace_ctx.set(None)


class TraceRecorder:
    """单条 Agent 执行轨迹的累加器。"""

    def __init__(self, company_id: int, agent_name: str,
                 session_id: Optional[str] = None, task_input: str = ""):
        self.company_id = company_id
        self.agent_name = agent_name
        self.session_id = session_id or uuid.uuid4().hex[:16]
        self.signature: Optional[str] = None              # 任务类型签名
        self.task_input = task_input or ""
        self.steps: list = []
        self.final_output: str = ""
        self.quality_score: Optional[float] = None
        self.error: Optional[str] = None

    def record_tool(self, tool_name: str, params: dict,
                    result: dict, latency_ms: float) -> None:
        """记录一次 Tool 调用步骤。"""
        self.steps.append({
            "tool": tool_name,
            "params": params,
            "ok": bool(result.get("ok")) if isinstance(result, dict) else None,
            "result_excerpt": _excerpt(result),
            "latency_ms": round(latency_ms, 1),
        })

    def set_output(self, text: str) -> None:
        self.final_output = text or ""

    def set_quality(self, score: float) -> None:
        self.quality_score = score

    def set_error(self, err: str) -> None:
        self.error = err

    def to_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "signature": self.signature,
            "task_input": self.task_input,
            "steps_json": self.steps,
            "final_output": self.final_output,
            "quality_score": self.quality_score,
            "tool_count": len(self.steps),
            "error": self.error,
            "created_at": time.time(),
        }


def _excerpt(result, limit: int = 600) -> str:
    """把 Tool 结果压缩成可入轨迹的摘要。"""
    if not isinstance(result, dict):
        return str(result)[:limit]
    if result.get("ok") is False:
        return f"ERROR: {result.get('error', '')}"[:limit]
    data = result.get("data")
    text = str(data if data is not None else result)[:limit]
    return text
