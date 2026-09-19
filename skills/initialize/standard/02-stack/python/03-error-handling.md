## 三、异常处理规范

### 【强制】必须注册全局异常处理器

禁止在每个路由中写 `try/except`。通过 `@app.exception_handler` 注册全局处理器，统一拦截 `RequestValidationError`、数据库异常、业务自定义异常。

```python
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"code": 422, "msg": "参数校验失败"})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"code": 500, "msg": "服务内部错误"})
```

### 【强制】禁止将内部异常堆栈返回给客户端

生产环境的 500 响应只返回通用错误信息，详细堆栈仅记录在服务端日志中。

### 【强制】数据库完整性异常必须映射为正确的 HTTP 状态码

| 数据库异常 | HTTP 状态码 | 说明 |
|-----------|------------|------|
| UNIQUE 约束冲突 | 409 Conflict | 资源已存在 |
| FOREIGN KEY 约束 | 400 Bad Request | 引用资源不存在 |
| 通用 DatabaseError | 500 | 数据库内部错误 |

参考实现：
```python
@app.exception_handler(IntegrityError)
async def integrity_handler(request, exc):
    if "UNIQUE" in str(exc):
        return JSONResponse(409, {"detail": "Resource already exists"})
    elif "FOREIGN KEY" in str(exc):
        return JSONResponse(400, {"detail": "Referenced resource not found"})
    return JSONResponse(500, {"detail": "Data integrity error"})
```

### 【推荐】定义业务异常体系

创建继承自 `Exception` 的业务异常基类（如 `BusinessError`），每个子类携带默认 HTTP 状态码和错误码，统一在处理器中转换为标准响应。
