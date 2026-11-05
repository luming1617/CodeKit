## 十、单元测试

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】代码必须能通过 golint 与 go vet

所有代码在通过 `golint` 与 `go vet` 运行时应无错误。推荐编辑器配置：保存时运行 `goimports`，并运行 `golint` 与 `go vet` 检查错误。（Uber《Introduction》）

### 【强制】一致性 lint：至少启用 errcheck、goimports、golint、govet、staticcheck

比"选哪套受祝福的 linter"更重要的是：**在整个代码库上一致地 lint**。推荐至少使用以下 linter（它们能抓住最常见的问题，并在不过度规定的前提下树立高质量门槛）（Uber《Linting》）：

- [errcheck](https://github.com/kisielk/errcheck)：确保错误被处理
- [goimports](https://pkg.go.dev/golang.org/x/tools/cmd/goimports)：格式化代码与管理 import
- [golint](https://github.com/golang/lint)：指出常见风格错误
- [govet](https://pkg.go.dev/cmd/vet)：分析常见错误
- [staticcheck](https://staticcheck.dev)：各类静态分析检查

lint 运行器推荐 [golangci-lint](https://github.com/golangci/golangci-lint)（在大型代码库上的性能、可同时配置使用多个规范 linter）；uber-go/guide 仓库附有推荐的 [.golangci.yml](https://github.com/uber-go/guide/blob/master/.golangci.yml) 示例配置。以上述集合为基线，鼓励各团队再添加适合自身项目的 linter。（Uber《Linting》）

### 【推荐】多输入/输出条件的逻辑用表驱动测试 + subtest

当被测系统需要在**多种条件**下测试、且部分输入输出会变化时，应使用表驱动测试（table-driven test）配合 [subtests](https://go.dev/blog/subtests) 减少重复、提升可读性：

```go
tests := []struct{
  give     string
  wantHost string
  wantPort string
}{
  {give: "192.0.2.0:8000", wantHost: "192.0.2.0", wantPort: "8000"},
  {give: ":8000", wantHost: "", wantPort: "8000"},
}

for _, tt := range tests {
  t.Run(tt.give, func(t *testing.T) {
    host, port, err := net.SplitHostPort(tt.give)
    require.NoError(t, err)
    assert.Equal(t, tt.wantHost, host)
    assert.Equal(t, tt.wantPort, port)
  })
}
```

约定：slice 命名为 `tests`，每个用例命名为 `tt`；用 `give` 与 `want` 前缀表明每个用例的输入与输出。表测试便于为错误信息添加上下文、减少重复逻辑、增加新用例。（Uber《Test Tables》）

### 【强制】表测试内部不要放条件断言等复杂分支

当 subtest 内部需要条件断言或其他分支逻辑（即在 `for` 循环体内有复杂逻辑）时，**不要**使用表驱动测试。庞大复杂的表测试会让读者难以定位失败原因，应拆成多个表或多个独立的 `Test...` 函数（例如把 `shouldCallX`/`shouldCallY` 等 mock 分支各自写成单独的测试）。追求的方向（Uber《Avoid Unnecessary Complexity in Table Tests》）：

- 聚焦最窄的行为单元；
- 最小化"测试深度"（同一测试中，后一断言依赖前一断言成立的层层关系，类似圈复杂度），避免条件断言；
- 保证表中所有字段在所有用例中都被使用；
- 保证所有测试逻辑对所有用例都执行。

具体而言，表里塞多个分支路径（`shouldError`、`expectCall` 等）、用许多 `if` 组织 mock 期望、或在表中放函数（`setupMocks func(*FooMock)`）都会让表难以读懂。但当被测行为只随输入变化时，把相似用例归入一表更能展示行为随全部输入的变化，优于把可比用例拆开。若测试体短小直白，允许保留"成功/失败"单一分支路径（如 `shouldErr` 字段）。（Uber《Avoid Unnecessary Complexity in Table Tests》）

### 【强制】并行测试必须显式固定循环变量

`t.Parallel()` 等并行测试与派生 goroutine、捕获引用的循环一样，必须在迭代作用域内显式声明循环变量，否则多数或全部测试会拿到意外值、或读到随运行变化的值：

```go
for _, tt := range tests {
  tt := tt // for t.Parallel
  t.Run(tt.give, func(t *testing.T) {
    t.Parallel()
    // ...
  })
}
```

（Uber《Test Tables》子节《Parallel Tests》）

### 【强制】测试失败信息必须有用：什么输入、实际得到什么、期望什么

测试失败时应给出有帮助的消息，说明哪里错了、用了什么输入、实际得到了什么、期望是什么。可以写一堆 assertFoo 辅助函数，但要确保它们产生有用的错误信息。**假定调试你失败测试的人不是你、也不是你的队友**。典型的 Go 失败写法：

```go
if got != tt.want {
    t.Errorf("Foo(%q) = %d; want %d", tt.in, got, tt.want) // or Fatalf, if test can't test anything more past this point
}
```

注意顺序是 **actual != expected**，消息也用这个顺序；一些测试框架鼓励反着写（`0 != x`、"expected 0, got x"），Go 不这样。嫌手写量大可以用表驱动测试；用同一个测试辅助函数跑不同输入时，把每个调用方包进不同的 `TestFoo` 函数（`func TestSingleValue(t *testing.T) { testHelper(t, []int{80}) }`），失败时即以该名字报出。无论如何，让未来调试你代码的人看到有帮助的失败信息是你的责任。（CRC《Useful Test Failures》）

### 【推荐】新增包时提供可运行的 Example

新增包时，包含预期用法的示例：一个可运行的 `Example`，或一个演示完整调用序列的简单测试。可参考 [testable Example() functions](https://go.dev/blog/examples)。（CRC《Examples》）标准库的包源码本身就意在作为语言用法示例；很多包带有可直接运行的、自包含的可执行示例。（EG《Examples》）

### 【参考】测试相关的其他约定

- 测试函数命名可有下划线用于归组相关用例（`TestMyFunction_WhatIsBeingTested`），这是函数 MixedCaps 约定的例外（Uber《Function Names》）；表测试中字段数 ≤3 时可省略结构体字段名（Uber《Use Field Names to Initialize Structs》）。
- 测试中不要用 panic 标记失败，优先 `t.Fatal`/`t.FailNow`（Uber《Don't Panic》）。
- 可能派生 goroutine 的包，用 go.uber.org/goleak 测试 goroutine 泄漏（Uber《Don't fire-and-forget goroutines》）。
