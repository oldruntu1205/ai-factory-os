# 工业 AI 员工平台 - 前端镜像（多阶段构建）
# 构建产物由 nginx 托管，并把 /api /static /output /docs 反代到 backend 服务

# ---------- 1) 构建阶段 ----------
FROM node:20-alpine AS build
WORKDIR /app

# 优先用 lockfile 精确安装；缺失时退化为 npm install
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY . .
RUN npm run build

# ---------- 2) 运行阶段 ----------
FROM nginx:1.27-alpine
# 前端统一走相对路径 /api（见 src/api.js），无需构建期注入变量
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
