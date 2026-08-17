# 本地 → 生产 实时同步部署手册（Git Push 自动部署）

> 对应方案 ②：本地改动 `git push` 即自动同步到 prod 并重启后端。
> 配套脚本均在 `deploy/git-sync/`。

## 架构

```
本地开发机  H:\制造业 AI 员工平台全量开发
   │  git push prod
   ▼
PROD <PROD_DOMAIN_REMOVED>
   /www/wwwroot/<站点目录>.git        (bare 仓库，仅收推送)
        │  hooks/post-receive
        ▼
   /www/wwwroot/<站点目录>            (工作树)
        git checkout -f main
        ├─ 仅 backend/ 变更 → systemctl restart ai-factory-backend ai-factory-worker
        └─ 语法预检失败    → 保留旧服务运行，不重启
```

- **同步范围**：仅 `backend/` 代码自动同步并重启。前端（3800+ 文件，需 `npm build`）走 `deploy-prod.sh` 独立发布，不进自动同步。
- **安全机制**：推送前 `py_compile` 语法预检，坏代码不会把线上打挂；`.env` / `venv` / `*.db` 被 `.gitignore` 排除，不会污染生产。
- **可回滚**：`git revert HEAD && git push prod main` 即可一键回退。

---

## 一、前置条件（一次性）

1. **SSH 免密**：本地能直接 `ssh root@<PROD_DOMAIN_REMOVED>` 无需密码。
   ```bash
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
   ssh-copy-id root@<PROD_DOMAIN_REMOVED>
   ```
2. **生产整盘备份**（极重要，首次推送会用本地代码覆盖 prod 工作树）：
   ```bash
   ssh root@<PROD_DOMAIN_REMOVED> "cp -a /www/wwwroot/<站点目录> /www/wwwroot/<站点目录>.bak.\$(date +%s)"
   ```

---

## 二、生产端：创建 bare 仓库 + 钩子（在服务器执行一次）

> **注意**：`setup-prod.sh` / `post-receive` 这些脚本此刻只存在于本地仓库（首次部署时服务器上还没有 git 仓库）。不要直接在服务器终端里 `bash deploy/...`，那样会报 `No such file or directory`。推荐用下面的**本地一键引导**。

### 方式 A（推荐）：本地一键引导

在**本地开发机**（仓库根目录）执行，脚本会自动上传 git-sync 目录并在服务器上运行 setup-prod.sh：

```bash
bash deploy/git-sync/bootstrap-prod.sh root@<PROD_DOMAIN_REMOVED> /www/wwwroot/<站点目录> main
```

### 方式 B：手动上传后在服务器执行

```bash
# 本地开发机：上传脚本目录
scp -r deploy/git-sync root@<PROD_DOMAIN_REMOVED>:/root/git-sync

# 服务器：执行 setup-prod.sh
bash /root/git-sync/setup-prod.sh /www/wwwroot/<站点目录> main
```

脚本会：
- `git init --bare /www/wwwroot/<站点目录>.git`
- 安装 `hooks/post-receive`（已带语法预检与「仅后端变更才重启」逻辑）
- 写入 `deploy-config.sh`（工作树路径 / 分支 / venv python / 服务名）

> 注意：prod 现有代码不是 git 仓库。`post-receive` 用 `checkout -f` 只覆盖 **受版本管理的文件**；`venv/`、`.env`、`*.db`、`logs/` 均为 untracked/ignored，不会被触碰。

---

## 三、本地端：初始化 + 首推（在开发机执行）

```bash
# 仅初始化并提交（不加远程）：
bash deploy/git-sync/setup-local.sh

# 确认 .gitignore 已排除敏感文件后，关联远程并首推：
bash deploy/git-sync/setup-local.sh prod root@<PROD_DOMAIN_REMOVED>:/www/wwwroot/<站点目录>.git
```

首推触发生产 `post-receive` → 自动 `checkout` + 重启后端。验证：
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<PROD_DOMAIN_REMOVED>/health   # 期望 200
ssh root@<PROD_DOMAIN_REMOVED> "tail -n 3 /www/wwwroot/<站点目录>/deploy.log"
```

---

## 四、日常使用（从此之后）

```bash
# 改完后端代码，一条命令上线：
git add -A
git commit -m "feat: xxx"
git push prod main

# 单文件紧急热修（不走整仓 git，可用既有脚本）：
bash deploy/sync-rag-fixes.sh --mode remote --host root@<PROD_DOMAIN_REMOVED> --code-dir /www/wwwroot/<站点目录>
```

部署日志统一在 prod 的 `deploy.log`，每次推送一行记录。

---

## 五、回滚

```bash
# 方式一：撤销最近一次提交并回推（最常用）
git revert HEAD
git push prod main

# 方式二：回退到指定提交
git push prod <旧commitSHA>:main

# 极端情况：整盘回滚（首次推送前的备份）
ssh root@<PROD_DOMAIN_REMOVED> "systemctl stop ai-factory-backend ai-factory-worker; \
  rm -rf /www/wwwroot/<站点目录>; \
  mv /www/wwwroot/<站点目录>.bak.<时间戳> /www/wwwroot/<站点目录>; \
  systemctl start ai-factory-backend ai-factory-worker"
```

---

## 六、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 推送后服务没重启 | 本次提交只动了前端/文档 | 正常，仅 backend/ 变更会重启 |
| 推送后 `deploy.log` 记「语法检查失败」 | 本地漏跑 `py_compile` | 修语法后重新 commit+push；旧服务仍在跑 |
| `Permission denied (publickey)` | 未配置 SSH 免密 | 回看「前置条件 1」 |
| `Could not resolve hostname <PROD_DOMAIN_REMOVED>` | 域名未解析/未指向生产机 | 用公网 IP 替换主机名：服务器上 `curl -s ifconfig.me` 获取公网 IP |
| 首次推送后页面异常 | 本地与 prod 代码差异大 | 用「五、回滚」整盘回退，再比对差异 |
| 想同步前端 | 前端不在自动同步范围 | 走 `deploy-prod.sh` 独立发布 |

---

## 七、与既有脚本的关系

- `deploy/sync-rag-fixes.sh`：单/少量文件热修，rsync + 重启，**与本文 git 流程互不冲突**，按需选用。
- `deploy/git-sync/`：全量后端代码「push 即上线」的自动通道，纳入版本管理与回滚。

---

## 八、方案 ③（可选叠加）：本地监听自动同步（保存即上线）

在方案 ② 基础上叠加一个**本地常驻守护进程**：`backend/` 下任意文件保存后，守护进程在 8 秒防抖后**自动 `git commit` + `git push prod`**，无需任何手动操作，实现真正的「零操作实时同步」。生产端逻辑与 ② 完全一致（仅后端变更才重启、语法预检兜底）。

> 仅建议**单人开发/演示**场景使用。多人协作时请走 ② 的手动 `git push`，避免每个人的保存互相触发推送造成提交混乱。

### 8.1 启动 / 停止

```bash
# 后台启动监听守护（日志 watch-sync.log，PID watch-sync.pid）
bash deploy/git-sync/start-watch.sh start

# 查看状态 / 停止 / 重启
bash deploy/git-sync/start-watch.sh status
bash deploy/git-sync/start-watch.sh stop
bash deploy/git-sync/start-watch.sh restart
```

### 8.2 行为说明

- **防抖 8s**：连续保存会合并为一次推送，避免每个按键都触发部署。
- **范围过滤**：仅 `backend/` 下的改动会自动推送；改 `frontend/`、`docs/` 等不会触发（前端走 `deploy-prod.sh`）。如需扩大范围：
  ```bash
  GIT_SYNC_SCOPE="backend,deploy/git-sync" bash deploy/git-sync/start-watch.sh start
  ```
- **空 diff 跳过**：没有实际改动不会提交。
- **未配置远程则只本地提交**：若还没执行「三、首推」配置 `prod` 远程，守护仅做本地自动提交，不报错退出。
- **推送失败不退出**：prod 不可用时记录错误日志并继续监听，恢复后下次保存会重试。
- **依赖**：纯 Python 标准库即可；若装了 `watchdog`（`pip install watchdog`）则用事件驱动更省 CPU，否则自动回退为 `git status` 轮询（默认 5s）。

### 8.3 常用环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GIT_SYNC_REMOTE` | `prod` | 推送目标远程名 |
| `GIT_SYNC_BRANCH` | `main` | 推送分支 |
| `GIT_SYNC_SCOPE` | `backend` | 自动同步路径前缀（逗号分隔） |
| `GIT_SYNC_DEBOUNCE` | `8` | 防抖秒数 |
| `GIT_SYNC_POLL` | `5` | 轮询模式间隔秒数 |

### 8.4 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 保存后没上线 | 改动不在 `backend/` 范围 | 检查 `GIT_SYNC_SCOPE`；或手动 `git push prod main` |
| 日志显示「未配置远程」 | 还没跑「三、首推」 | 先配置 `prod` 远程 |
| 日志显示「推送失败」 | prod 不可达/SSH 失效 | 查 `ssh root@<PROD_DOMAIN_REMOVED>`；恢复后下次保存自动重试 |
| CPU 占用偏高 | 回退到了轮询模式 | `pip install watchdog` 后重启守护 |

