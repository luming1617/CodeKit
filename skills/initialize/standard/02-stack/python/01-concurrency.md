## 一、并发规范

### 【强制】禁止在 `async def` 路由中执行同步阻塞操作

`async def` 路由运行在事件循环中，任何同步 I/O（如 `requests.get()`、`time.sleep()`、同步 ORM 查询）或 CPU 密集计算都会阻塞整个事件循环，导致所有并发请求被挂起。

**反例**：
```python
@app.get("/bad")
async def bad_endpoint():
    time.sleep(1)  # 阻塞事件循环，整个服务卡死 1 秒
    return {"status": "ok"}
```

**正例**（二选一）：
```python
# 方案一：使用 def 定义同步路由，FastAPI 自动丢入线程池
@app.get("/ok")
def sync_endpoint():
    time.sleep(1)  # 在线程池中阻塞，不影响事件循环
    return {"status": "ok"}

# 方案二：在 async 路由中用 run_in_threadpool / asyncio.to_thread
from fastapi.concurrency import run_in_threadpool

@app.get("/also-ok")
async def async_endpoint():
    result = await run_in_threadpool(blocking_io_call)
    return result
```

### 【强制】CPU 密集任务必须使用进程池，禁止使用线程池

CPU 密集计算受 GIL 限制，线程池无法绕过。必须使用 `ProcessPoolExecutor` 或外部任务队列（Celery、Arq）。

### 【推荐】数据库访问优先使用异步驱动

使用 `asyncpg`（PostgreSQL）、`aiomysql` 等异步数据库驱动，或 SQLAlchemy 2.x 的异步模式，确保数据库 I/O 不阻塞事件循环。

### 【推荐】为同步第三方 SDK 统一封装异步适配层

如果项目依赖的 SDK 只有同步实现（如部分云服务 SDK），应在 `utils/` 或 `services/` 中统一封装为异步函数，避免在路由层散落 `run_in_threadpool` 调用。
