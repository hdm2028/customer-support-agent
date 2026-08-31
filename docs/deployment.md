# 部署说明

## 运行方式

FastAPI 入口为 `main:app`：

```powershell
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

容器入口：

```text
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8012}
```

## 配置

生产环境通过环境变量配置：

- `LLM_API_KEY`
- `DATABASE_BACKEND`
- `MYSQL_DSN` 或 `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`
- `REDIS_URL` 或 `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD`
- `MQ_BACKEND`

本机 MySQL 默认端口为 `3306`。本机 Redis 默认端口为 `6379`。

## MySQL

表结构参考 [mysql_schema.sql](mysql_schema.sql)。

推荐配置：

```env
DATABASE_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=customer_support
MYSQL_PASSWORD=change-me
MYSQL_DATABASE=customer_support
SEED_DEMO_DATA=false
```

## Redis

Redis 用于会话状态、缓存、退款锁和退款幂等结果。

```env
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
```

## Docker

```powershell
docker build -t customer-support-agent .
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

开发依赖容器：

```powershell
docker compose -f docker-compose.dev.yml up -d
```

## 健康检查

```text
GET /health
GET /info
```
