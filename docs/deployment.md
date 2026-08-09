# 部署说明

本文档说明如何把中文电商智能售后客服 Agent 打包成 Docker 镜像，并部署到支持 Docker Web Service 的云平台。

## 为什么需要 Docker

本地运行时，你需要自己安装 Python、依赖包，再启动 uvicorn。

Docker 的作用是把这些运行条件打包在一起：

```text
Python 版本
依赖包
项目代码
启动命令
环境变量入口
```

这样云平台只需要构建镜像并运行容器即可。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 定义如何构建容器镜像 |
| `.dockerignore` | 排除不应该进入镜像的本地文件 |
| `.env.example` | 环境变量模板，不包含真实密钥 |
| `render.yaml` | Render Blueprint 部署配置 |

## Dockerfile 解释

```dockerfile
FROM python:3.13-slim
```

使用 Python 3.13 的轻量 Linux 镜像作为基础环境。

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8012
```

- `PYTHONDONTWRITEBYTECODE=1`：不生成 `.pyc` 缓存文件。
- `PYTHONUNBUFFERED=1`：让日志实时输出，方便云平台查看日志。
- `PORT=8012`：本地默认端口，云平台也可以覆盖这个变量。

```dockerfile
WORKDIR /app
```

容器里的工作目录是 `/app`，后续命令都在这里执行。

```dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```

先复制依赖文件并安装依赖。这样 Docker 构建时可以利用缓存：如果只改业务代码，没有改依赖，就不用重复安装依赖。

```dockerfile
COPY . .
```

复制项目代码到容器。

```dockerfile
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8012}"]
```

容器启动后运行 FastAPI 服务。

这里必须使用：

```text
--host 0.0.0.0
```

不能用 `127.0.0.1`。因为云平台需要从容器外部访问服务，监听 `127.0.0.1` 只允许容器内部访问。

## 本地 Docker 测试

构建镜像：

```powershell
docker build -t customer-support-agent .
```

运行容器：

```powershell
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

打开：

```text
http://127.0.0.1:8012/
```

健康检查：

```text
http://127.0.0.1:8012/health
```

## SQLite 在云部署里的注意点

容器文件系统通常是临时的。服务重新部署或重启后，容器内部写入的文件可能丢失。

所以项目支持配置数据库路径：

```env
DATABASE_PATH=/var/data/customer_support.db
```

云平台需要把 `/var/data` 配成持久化磁盘挂载路径。这样 SQLite 数据库文件写到持久化磁盘中，工单、会话、pending task 和 feedback 才能保留。

本地开发不配置也可以，默认路径是：

```text
data/customer_support.db
```

## Render 部署

项目提供了 `render.yaml`。它会告诉 Render：

```text
使用 Docker 部署
健康检查路径是 /health
持久化磁盘挂载到 /var/data
SQLite 数据库写到 /var/data/customer_support.db
LLM_API_KEY 在控制台手动配置，不写进代码
```

部署步骤：

1. 把项目推送到 GitHub。
2. 登录 Render。
3. 新建 Blueprint 或 Web Service。
4. 选择这个仓库。
5. 选择 Docker 部署。
6. 在环境变量里配置真实 `LLM_API_KEY`。
7. 确认 `DATABASE_PATH=/var/data/customer_support.db`。
8. 部署完成后访问 Render 分配的 URL。

注意：Render 的普通文件系统是临时的；只有挂载盘路径下的数据才会跨重启和重新部署保留。

## 部署后的验证

部署成功后访问：

```text
/health
/info
/
/docs
/tickets
```

如果 `/health` 返回：

```json
{
  "success": true
}
```

说明服务已经启动。

如果 `/info` 里：

```json
{
  "has_llm_key": true
}
```

说明云平台已经读取到智谱 API Key。
