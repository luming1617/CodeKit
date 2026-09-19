## 五、项目结构规范

### 【强制】按功能模块纵向拆分，禁止按技术类型横向堆叠

推荐目录结构（遵循分层架构）：

```
fastapi_project/
├── main.py                 # 入口：仅注册路由、中间件、异常处理器
├── core/
│   ├── config.py           # Pydantic Settings 管理配置
│   ├── security.py         # JWT、密码哈希、权限依赖
│   └── dependencies.py     # 全局依赖（get_db、get_current_user）
├── db/
│   ├── session.py          # 引擎、SessionLocal、get_db
│   └── models/             # SQLAlchemy ORM 模型
├── schemas/                # Pydantic 入参/出参模型
├── crud/                   # 数据库操作封装
├── services/               # 业务逻辑层
├── routers/                # 路由层（按模块拆分）
│   ├── __init__.py         # api_router 汇总
│   ├── user.py
│   └── order.py
├── utils/                  # 通用工具（logger、文件、时间）
└── tests/                  # 测试
```

### 【强制】路由层禁止写业务逻辑

路由函数只负责：接收参数 → 调用 Service → 返回响应。所有业务规则放在 `services/`，数据库操作放在 `crud/`。

### 【推荐】配置使用 Pydantic Settings

通过环境变量覆盖默认值，实现开发/生产配置隔离：

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./dev.db"
    SECRET_KEY: str
    DEBUG: bool = False
    class Config:
        env_file = ".env"

settings = Settings()
```
