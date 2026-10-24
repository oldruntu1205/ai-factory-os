"""
hermes/scheduler/jobs.py —— 定时任务定义（AI 员工"主动干活"的地方）

制造业客户不为"你问我答"付钱，为"AI 主动发现异常"付钱。
这里的每条 job = 一个到点自动上班的 AI 员工动作。

job 结构：
    name        任务名（唯一）
    cron        5 字段 cron（可被环境变量覆盖，见 service.load_jobs）
    task        TaskQueue 的任务名（worker 按此名路由 handler）
    agent       由哪个 AI 员工执行
    prompt      给 AI 员工的任务指令
    description 中文说明（管理端展示）

执行链路：
    SchedulerService 到点 → queue.enqueue(task) → Worker 取 task
        → register_worker 注册的 handler → runtime.execute(agent, prompt)
"""
from typing import Dict, List

# 任务名常量（避免字符串散落各处拼错）
TASK_PRODUCTION_DELAY = "scheduler.production_delay_scan"
TASK_QUOTATION_FOLLOWUP = "scheduler.quotation_followup_scan"
TASK_QUALITY_SUMMARY = "scheduler.quality_daily_summary"


JOBS: List[Dict] = [
    {
        "name": "production.delay_scan",
        "task": TASK_PRODUCTION_DELAY,
        "cron": "0 8 * * 1-6",            # 工作日每天 08:00
        "agent": "production_agent",
        "prompt": (
            "执行每日交期巡检：逐条检查在制订单的承诺交期与实际进度，"
            "识别可能延期的订单，列出订单号、客户、当前工序、预计延期天数与建议动作。"
            "只输出结论清单，不要解释过程。"
        ),
        "description": "工作日早 8 点巡检在制订单交期，输出有延期风险的订单清单",
    },
    {
        "name": "quotation.followup_scan",
        "task": TASK_QUOTATION_FOLLOWUP,
        "cron": "30 9 * * 1-5",           # 工作日 09:30
        "agent": "sales_agent",
        "prompt": (
            "执行报价跟进扫描：找出超过 3 天未获客户回复的报价单，"
            "按金额从高到低排序，为每条生成一句跟进话术（中文，礼貌、不催逼）。"
        ),
        "description": "工作日早上扫描超 3 天未回复的报价，生成跟进话术",
    },
    {
        "name": "quality.daily_summary",
        "task": TASK_QUALITY_SUMMARY,
        "cron": "0 18 * * 1-6",           # 工作日每天 18:00
        "agent": "quality_agent",
        "prompt": (
            "执行当日质量汇总：统计今日质量异常记录，按缺陷类型分组，"
            "给出各类型的数量、占比与最可能的一条根因线索，输出为简报格式。"
        ),
        "description": "工作日下班前汇总当日质量异常，输出根因线索简报",
    },
]

JOBS_BY_NAME: Dict[str, Dict] = {j["name"]: j for j in JOBS}


def get_job(name: str) -> Dict:
    return JOBS_BY_NAME.get(name)
