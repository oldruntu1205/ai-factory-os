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

```bash
# 把本仓库 deploy/git-sync/ 传到服务器，或直接在该目录执行
bash deploy/git-sync/setup-prod.sh /www/wwwroot/<站点目录> main
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
| 首次推送后页面异常 | 本地与 prod 代码差异大 | 用「五、回滚」整盘回退，再比对差异 |
| 想同步前端 | 前端不在自动同步范围 | 走 `deploy-prod.sh` 独立发布 |

---

## 七、与既有脚本的关系

- `deploy/sync-rag-fixes.sh`：单/少量文件热修，rsync + 重启，**与本文 git 流程互不冲突**，按需选用。
- `deploy/git-sync/`：全量后端代码「push 即上线」的自动通道，纳入版本管理与回滚。
