## 二、可观测性规范

### 【强制】每个请求必须生成并传递 `X-Request-ID`

在中间件中为每个请求生成 UUID，注入到日志上下文和响应头中，下游服务需透传该 Header。

```python
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    bind_contextvars(request_id=request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
```

### 【强制】生产环境日志必须使用结构化格式（JSON）

禁止使用 `print()`。使用 `structlog` 或标准库 `logging` 的 JSON Formatter，确保日志可被 ELK、Loki 等系统解析。

### 【推荐】日志中必须包含的字段

| 字段 | 说明 |
|------|------|
| `request_id` | 全链路追踪标识 |
| `method` + `path` | 请求方法+路径 |
| `status_code` | 响应状态码 |
| `duration_ms` | 处理耗时（毫秒） |
| `user_id` | 当前用户标识（如已认证） |

### 【推荐】集成 OpenTelemetry 实现链路追踪

对于微服务架构，使用 `opentelemetry-instrumentation-fastapi` 自动埋点。设置 `OTEL_SERVICE_NAME` 确保服务名一致且唯一。

**采样策略**：生产环境使用 `parentbased_traceidratio`，采样率建议 10%（`OTEL_TRACES_SAMPLER_ARG=0.1`），高优先级请求可通过自定义采样器保留。

### 【推荐】暴露 Prometheus 指标

使用 `prometheus_fastapi_instrumentator` 或手写 Counter/Histogram，至少覆盖：请求总数、请求耗时分布、活跃请求数。
