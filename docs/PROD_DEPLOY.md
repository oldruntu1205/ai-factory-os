# 制造业 AI 员工平台 —— 生产环境（prod）全量部署手册

> 适用版本：V1.0 / V2.1（Hermes Agent Runtime）
> 配套脚本与配置均已落地在仓库，本手册给出**从开发机到生产机的绝对路径映射**与分步操作。

---

## 1. 架构总览

```
[ 浏览器 ]
   │  https
   ▼
[ 宿主 Nginx  (TLS 终止) ]           部署位置: /etc/nginx/conf.d/industrial-ai.conf
   ├── app.yourdomain.com  ──▶  frontend 容器  (127.0.0.1:3000)  ── /api ─▶ backend 容器
   └── api.yourdomain.com  ──▶  backend  容器  (127.0.0.1:8000)
                                                        │
              ┌──────────────┬─────────────┬───────────┼────────────┐
              ▼              ▼             ▼           ▼            ▼
          postgres:5432  redis:6379  rabbitmq:5672  qdrant:6333  hermes-worker
        （仅 docker 内网，不暴露公网端口）
```

- 前端 `baseURL: '/api'`（相对路径），由 frontend 容器 nginx 反代到 backend，无需构建期注入变量。
- 数据库用 **PostgreSQL**（生产），不再走 SQLite。

---

## 2. 路径约定（绝对路径）

| 角色 | 路径 |
|---|---|
| 开发机（Windows）源码根 | `C:\Users\Administrator\WorkBuddy\制造业 AI 员工平台全量开发\` |
| 生产机（Linux）部署根 | `/opt/industrial-ai/` |
| 生产机 compose 项目根 `.env` | `/opt/industrial-ai/.env`（由 `deploy/prod.env` 复制） |
| 生产机宿主 Nginx 配置 | `/etc/nginx/conf.d/industrial-ai.conf`（内容同 `deploy/nginx.conf`） |
| 生产机数据卷（Docker 托管） | `pg_data` / `redis_data` / `rabbitmq_data` / `qdrant_data` / `backend_uploads` / `backend_output` |

> ⚠️ 本手册所有命令均使用上述绝对路径，请用你服务器的真实路径替换 `/opt/industrial-ai`。

---

## 3. 生产机前置条件

```bash
# 操作系统：Ubuntu 22.04 LTS（推荐）
# 安装 Docker + compose 插件
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
docker --version
docker compose version
```

---

## 4. 把代码搬到生产机

**方式 A：Git（推荐）**
```bash
# 在生产机
sudo mkdir -p /opt/industrial-ai
cd /opt/industrial-ai
git clone <你的仓库地址> .
```

**方式 B：从开发机 scp（无 git 时）**
```powershell
# 在开发机 PowerShell，整目录拷贝（排除 node_modules / .db / 镜像缓存）
$src = "C:\Users\Administrator\WorkBuddy\制造业 AI 员工平台全量开发"
$dst = "user@your-server-ip:/opt/industrial-ai"
scp -r -o StrictHostKeyChecking=no "$src" "$dst"
```
> 生产机收到后，确保目录结构含：`backend/`、`frontend/`、`deploy/`、`docker-compose.yml`。

---

## 5. 准备生产环境变量

```bash
# 在生产机 /opt/industrial-ai 下
cd /opt/industrial-ai
cp deploy/prod.env /opt/industrial-ai/.env
```

编辑 `/opt/industrial-ai/.env`，至少修改：

```bash
# 1) JWT 密钥（必须改，生成方式）
SECRET_KEY=$(openssl rand -hex 32)      # 把输出填进去

# 2) DeepSeek Key（platform.deepseek.com 获取）
LLM_API_KEY=sk-你的真实key

# 3) 公网地址（邮件追踪 / Webhook 回调基址）
APP_PUBLIC_URL=https://api.yourdomain.com

# 4) 域名邮箱
SMTP_FROM=sales@yourdomain.com
```

> 数据库 `DATABASE_URL` 已在 `docker-compose.yml` 内写死（指向 postgres 容器），无需在此填。

---

## 6. 一键部署

```bash
cd /opt/industrial-ai
bash deploy/deploy-prod.sh
```

脚本会自动：拉代码 → 校验 `.env` → `docker compose build` → `up -d` → 等后端 `/health` → `alembic upgrade head` → 输出访问地址。

如需手动分步：

```bash
cd /opt/industrial-ai
docker compose build
docker compose up -d
# 等后端就绪
curl -s http://127.0.0.1:8000/health      # 期望 {"status":"ok"}
# 同步迁移基线（幂等，可重复执行）
docker compose exec -T backend alembic upgrade head
```

---

## 7. 宿主 Nginx + SSL

```bash
# 把仓库提供的配置放到宿主 Nginx
sudo cp /opt/industrial-ai/deploy/nginx.conf /etc/nginx/conf.d/industrial-ai.conf
# 把 api.yourdomain.com / app.yourdomain.com 的证书放到配置里写的路径
# （宝塔用户：网站 → SSL → Let's Encrypt 一键申请，再套用本配置）
sudo nginx -t && sudo systemctl reload nginx
```

SSL 证书路径示例（按你实际调整）：
```
/etc/nginx/ssl/api.yourdomain.com/fullchain.pem
/etc/nginx/ssl/api.yourdomain.com/privkey.pem
/etc/nginx/ssl/app.yourdomain.com/fullchain.pem
/etc/nginx/ssl/app.yourdomain.com/privkey.pem
```

---

## 8. 验证部署

```bash
# 后端健康
curl -s https://api.yourdomain.com/health
# 期望: {"status":"ok"}

# Swagger 文档
curl -s -o /dev/null -w "%{http_code}\n" https://api.yourdomain.com/docs
# 期望: 200

# 前端首页
curl -s -o /dev/null -w "%{http_code}\n" https://app.yourdomain.com/
# 期望: 200

# 数据库连通（在容器内）
docker compose exec backend python -c "from database import engine; print(engine.connect().execute(__import__('sqlalchemy').text('select 1')))"
```

浏览器打开 `https://app.yourdomain.com/` → 登录（超管 `platform@demo.com / Platform@123`，企业 admin `admin@demo.com / 123456`）→ 平台管理 → 模型设置，确认可切换大模型。

---

## 9. 数据迁移（SQLite → PostgreSQL）

生产是**全新 PostgreSQL**，默认 `SEED_DEMO=0`，只有空表结构。若需把开发机现有 SQLite 业务数据（轮播图、多语言、模型配置等）搬过来：

```bash
# 在生产机（已装 psycopg2），参考 docs/POSTGRES_UPGRADE_CHECKLIST.md 第 1~10 步
# 关键脚本：backend/migrate_to_postgres.py
cd /opt/industrial-ai/backend
python migrate_to_postgres.py --dry-run     # 先预览
python migrate_to_postgres.py               # 正式迁移（需 PostgreSQL 可连）
```

> 全新部署不迁移也行；迁移后再 `docker compose restart backend` 使其加载数据。

---

## 10. 备份

```bash
# PostgreSQL
docker compose exec postgres pg_dump -U industrial industrial_ai > /opt/industrial-ai/backups/db_$(date +%F).sql

# 上传文件卷
docker run --rm -v industrial-ai_backend_uploads:/data -v /opt/industrial-ai/backups:/backup alpine tar czf /backup/uploads_$(date +%F).tar.gz -C /data .

# 或用现成脚本
bash /opt/industrial-ai/deploy/backup.sh
```

定时备份（crontab，每天 03:00）：
```bash
0 3 * * * cd /opt/industrial-ai && docker compose exec postgres pg_dump -U industrial industrial_ai > backups/db_$(date +%F).sql
```

---

## 11. 常用运维

```bash
cd /opt/industrial-ai

docker compose ps                     # 查看状态
docker compose logs -f backend       # 后端日志
docker compose logs -f hermes-worker # Worker 日志
docker compose restart backend       # 重启后端
docker compose down                  # 停止（不删数据卷）
docker compose down -v               # 停止并删数据卷（危险！）

# 改了 .env 后重新加载
docker compose up -d                 # 自动重建受影响的服务

# 升级代码
git pull && bash deploy/deploy-prod.sh
```

---

## 12. 上线检查清单

- [ ] `/opt/industrial-ai/.env` 中 `SECRET_KEY` 已替换为随机强密钥
- [ ] `LLM_API_KEY`（DeepSeek）已配置且有效
- [ ] `SEED_DEMO=0`（生产不注入演示数据）
- [ ] HTTPS 已生效（SSL 证书有效）
- [ ] 宿主防火墙只放行 22/80/443（postgres/redis/rabbitmq/qdrant 不暴露公网）
- [ ] `APP_PUBLIC_URL` 指向真实域名
- [ ] 数据库自动备份任务已建
- [ ] RabbitMQ 默认密码 `hermes123` 已在生产环境修改（docker-compose 内 `RABBITMQ_DEFAULT_PASS`）
- [ ] PostgreSQL 密码 `industrial123` 已在生产环境修改（docker-compose 内 `POSTGRES_PASSWORD`）
- [ ] 浏览器登录后台，模型设置可正常切换并保存

---

## 13. 本次新增/修改的文件清单

| 文件 | 说明 |
|---|---|
| `frontend/Dockerfile` | **新增**：前端多阶段构建镜像（node→nginx） |
| `frontend/nginx.conf` | **新增**：前端容器 nginx（托管 dist + 反代 /api /static /output /docs） |
| `backend/.dockerignore` | **新增**：排除 `.env`/`*.db`/缓存，防密钥与本地库进镜像 |
| `frontend/.dockerignore` | **新增**：排除 node_modules / dist |
| `docker-compose.yml` | **重写**：backend healthcheck + 卷持久化 + 注入 SECRET_KEY/SEED_DEMO/APP_PUBLIC_URL；基础设施仅内网 |
| `deploy/prod.env` | **新增**：生产环境变量模板 |
| `deploy/deploy-prod.sh` | **新增**：一键生产部署脚本（绝对路径） |
| `deploy/nginx.conf` | **更新**：app 域名反代到前端容器 3000，api 反代 backend 8000 |
| `backend/config.py` | **修复**：`DATABASE_URL` 环境变量优先级高于 `USE_SQLITE`，避免容器误用 SQLite |
| `backend/.env.example` | **更新**：默认不再带 `USE_SQLITE=1` |
