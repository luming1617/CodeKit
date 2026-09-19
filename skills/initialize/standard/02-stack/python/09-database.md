## 九、数据库规范

### 【强制】使用 SQLAlchemy 2.x + 异步驱动

生产环境使用 `asyncpg`（PostgreSQL），开发环境可使用 `aiosqlite`。

### 【强制】使用 Alembic 管理数据库迁移

禁止手动改表结构。迁移在 CI/CD 流水线中作为独立步骤执行，确认成功后再部署新版本代码。

### 【强制】连接池必须显式配置

```python
engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,            # 常规连接数
    max_overflow=10,         # 峰值额外连接（总上限 30）
    pool_pre_ping=True,      # 使用前验证连接有效性
    pool_recycle=3600,       # 1 小时回收
)
```

**多 Pod 部署公式**：总连接数 = `pods × workers × pool_size × 1.2`。

### 【推荐】按场景配置连接池参数

| 场景 | pool_size | max_overflow |
|------|-----------|--------------|
| Web API | 20 | 10 |
| 后台 Worker | 5 | 5 |
| Serverless | 1 | 0（配合 pgBouncer） |

### 【强制】数据库 Session 通过依赖注入获取，禁止全局共享

```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
```

使用 `Depends(get_db)`，FastAPI 保证每个请求独立 Session 且请求结束后自动关闭。

### 【推荐】高频查询字段必须建立索引

在 ORM 模型中通过 `index=True` 声明，或通过 Alembic 迁移显式创建。定期审查慢查询日志。
