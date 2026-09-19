## 四、单元测试规范

### 【强制】API 端点必须使用 TestClient / AsyncClient 覆盖

所有路由至少有一个测试用例覆盖成功场景和至少一个失败场景。

### 【强制】测试隔离：禁止使用生产数据库

使用内存 SQLite 或独立的测试数据库，通过 `dependency_overrides` 替换 `get_db` 依赖。

```python
@pytest.fixture
def test_db():
    Base.metadata.create_all(bind=engine)
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = override_get_db
    yield
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.clear()
```

### 【推荐】遵循测试金字塔

| 层级 | 占比 | 说明 |
|------|------|------|
| 单元测试 | ~60% | Services、Utils 的纯逻辑测试，执行时间 < 100ms |
| 集成测试 | ~30% | API 端点 + 真实数据库，执行时间 < 1s |
| E2E 测试 | ~10% | 关键用户旅程 |

### 【推荐】异步端点使用 `httpx.AsyncClient`

```python
@pytest.mark.asyncio
async def test_async_endpoint():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/async-endpoint")
        assert response.status_code == 200
```

### 【推荐】Mock 外部依赖

所有外部 HTTP 调用、消息队列、第三方 SDK 在单元测试中必须 Mock。使用 `unittest.mock.patch` 或 `pytest-mock`。

### 【强制】测试覆盖率核心模块 ≥ 80%，关键路径 ≥ 90%
