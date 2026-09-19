## 六、API 设计规范

### 【强制】对外 API 使用 URL 路径版本控制

格式：`/api/v1/users`。禁止在 Header 或 Query 中传递版本号（内部服务可酌情使用 Header 版本）。

### 【强制】版本兼容原则：字段只增不删

- 新增字段必须设默认值
- 枚举值只扩不减
- 不得改变已有字段的数据类型或层级结构
- 破坏性变更（删字段、改语义）必须升级大版本号

### 【强制】版本适配层与业务逻辑分离

业务 Service 保持版本无关，在路由层通过适配函数将统一数据转换为各版本响应格式：

```python
# services/user_service.py — 版本无关
async def get_user(user_id: str) -> User: ...

# v1/routes/users.py
@router.get("/{user_id}", response_model=UserResponseV1)
async def get_user_v1(user_id: str, service: UserService = Depends()):
    user = await service.get_user(user_id)
    return UserResponseV1(id=user.id, name=user.full_name)  # 适配
```

### 【推荐】统一响应格式

```json
{
  "code": 200,
  "msg": "请求成功",
  "data": { ... }
}
```

错误响应：
```json
{
  "code": 404,
  "msg": "资源不存在",
  "data": null
}
```
