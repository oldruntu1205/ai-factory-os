r"""V3.0 运行单元 `manufacturing-core`（manufacturing owner）。P0 骨架。

启动：
    backend\.venv\Scripts\python.exe -m services.manufacturing-core

绑定地址与端口来自登记表 `runtime_deployment`（P0 用 registry.mock.json）。
生产部署形态、独立 UID 与密钥挂载属部署 manifest，待冻结。
"""

from services._kernel.service import main

SERVICE_ID = "manufacturing-core"

if __name__ == "__main__":
    main(SERVICE_ID)
