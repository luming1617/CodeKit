## 八、权限规范

### 【强制】认证与授权必须通过依赖注入实现

禁止在路由函数体内手写 token 解析逻辑。使用 `Depends(get_current_user)` 和 `Depends(require_roles(...))`。

### 【强制】JWT 验证必须固定算法

```python
# 正确
jwt.decode(token, key, algorithms=["HS256"])

# 错误 —— 攻击者可改为 alg=none 或 RS256->HS256 攻击
jwt.decode(token, key, algorithms=jwt.get_unverified_header(token)["alg"])
```

### 【推荐】权限声明与路由同址（Co-located）

使用装饰器风格将权限声明放在路由定义处，便于代码审查：

```python
@router.get("/authors", dependencies=[Depends(require_roles("get-authors"))])
async def get_authors(): ...

@router.post("/authors", dependencies=[Depends(require_roles("create-author", "admin"))])
async def create_author(): ...
```

**优势**：权限在路由处可见、代码审查时一目了然、重构时权限跟随路由移动。

### 【强制】对象级授权（水平越权防护）

“已登录”不等于“有权访问该资源”。必须在 Service 层校验 `resource.owner_id == current_user.id`。

### 【推荐】角色命名使用人类可读的字符串

如 `"admin-billing"`、`"read-orders"`，而非 `"role-02"`。

### 【强制】生产环境禁用 /docs 和 /openapi.json

```python
app = FastAPI(docs_url=None, redoc_url=None)  # 生产环境
```
