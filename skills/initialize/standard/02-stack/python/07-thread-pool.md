## 七、线程池规范

### 【强制】区分 I/O 密集型与 CPU 密集型任务

| 任务类型 | 处理方式 | 原因 |
|---------|---------|------|
| I/O 密集（文件、网络、数据库） | `run_in_threadpool` 或 `asyncio.to_thread` | GIL 在 I/O 等待时释放 |
| CPU 密集（计算、图像处理） | `ProcessPoolExecutor` | 绕过 GIL 限制 |

### 【强制】禁止在模块级别创建线程池/进程池实例并反复使用

应在应用启动时创建全局池，通过依赖注入或应用状态传递，避免每次请求创建新池的开销。

### 【推荐】控制线程池并发上限

同步路由的线程池大小由 Starlette 默认控制，但对于手动调用的 `run_in_threadpool`，应评估下游服务的承受能力，避免并发过高打垮依赖服务。

### 【强制】禁止在 `async def` 路由中使用 `concurrent.futures.ThreadPoolExecutor` 直接提交任务

这会绕过 Starlette 的线程池管理，可能导致线程数失控。
