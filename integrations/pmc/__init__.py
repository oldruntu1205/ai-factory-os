"""PMC 接入层（上游 §15 integrations/pmc/）。**状态：mock**。

真实字段/单位/时区属外部依赖（§16.1），未冻结前不猜。
对接清单见 `mock.REQUIRED_FROM_PMC`。
"""

from .adapter import *  # noqa: F401,F403
