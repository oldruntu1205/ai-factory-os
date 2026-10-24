"""ERP 接入层（上游 §15 integrations/erp/）。**状态：mock**。

真实字段/单位/时区属外部依赖（§16.1），未冻结前不猜。
对接清单见 `mock.REQUIRED_FROM_ERP`。
"""

from .adapter import *  # noqa: F401,F403
