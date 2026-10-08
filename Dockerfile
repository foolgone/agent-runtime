# 前端构建产物和后端跑在同一个镜像里：这个后端是有状态的（事件日志要落盘、
# SSE 要长连接），拆成两个服务只会凭空多出一层 CORS 和跨域缓冲，
# 换不来任何东西。单容器、单进程、单域名 —— 界面由后端自己挂在 /。

# ---------------------------------------------------------------- 阶段 1：构建前端
FROM node:22-slim AS web

WORKDIR /web

# 先只拷依赖清单：改源码不会让 npm ci 这一层缓存失效
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
# package.json 里的 build 是 "tsc --noEmit && vite build"：
# 类型不过就构建失败，跟 CI 是同一道闸
RUN npm run build

# ---------------------------------------------------------------- 阶段 2：运行时
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED 不是可选项：容器里 stdout 被重定向成管道，
# 不带这个的话日志会攒在缓冲区里，出事时看不到
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# README.md 得在：pyproject 的 readme 字段指向它，缺了打包直接失败
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 前端产物（已经在阶段 1 构建过，这里只搬）
COPY --from=web /web/dist ./web/dist

# AGENT_WEB_DIR 必须显式给：默认值是「从 config.py 往上三层找 web/dist」，
# 而装在 site-packages 里的 config.py 往上三层是 Python 安装目录，不是仓库根。
#
# AGENT_DATA_DIR 指向挂载点。没挂卷时它也能跑，只是容器一换会话就没了。
ENV AGENT_WEB_DIR=/app/web/dist \
    AGENT_DATA_DIR=/data \
    AGENT_PROVIDER=openai

VOLUME ["/data"]

EXPOSE 8000

# 用 python 而不是 curl：slim 镜像里没有 curl，为一个健康检查再装个包不值。
# /healthz 不碰模型，所以这是个便宜而且不会误报的检查。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# 不降权：没挂卷时无所谓，但线上要挂持久卷，而 Fly 的卷挂进来是 root 属主，
# 非 root 进程写不进去。降权需要入口脚本先 chown，为了这个再加一层
# 入口脚本不划算 —— 这是个单进程、只监听内网的应用，攻击面是模型输出和工具参数。
CMD ["uvicorn", "agent_runtime.api.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
