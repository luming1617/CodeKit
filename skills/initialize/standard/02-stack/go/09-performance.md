## 九、性能

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】性能准则只适用于热路径（hot path）

本节性能相关的指南只对热路径生效。（Uber《Performance》）

### 【推荐】基本类型与字符串互转优先用 strconv

将基本类型与字符串互转时，`strconv` 比 `fmt` 快：

```go
s := fmt.Sprint(rand.Int())    // BenchmarkFmtSprint-4  143 ns/op  2 allocs/op
s := strconv.Itoa(rand.Int())  // BenchmarkStrconv-4    64.2 ns/op 1 allocs/op
```

（Uber《Prefer strconv over fmt》）

### 【推荐】不要反复做 string 到 []byte 的转换

不要每次迭代都从固定字符串创建字节 slice；转换一次、保存结果再复用：

```go
// Bad：BenchmarkBad-4  50000000  22.2 ns/op
for i := 0; i < b.N; i++ {
  w.Write([]byte("Hello world"))
}

// Good：BenchmarkGood-4  500000000  3.25 ns/op
data := []byte("Hello world")
for i := 0; i < b.N; i++ {
  w.Write(data)
}
```

（Uber《Avoid repeated string-to-byte conversions》）

### 【推荐】尽可能预指定容器容量

尽可能指定容器容量，让内存在初始化时一次分配到位，减少添加元素时反复拷贝扩容带来的后续分配。（Uber《Prefer Specifying Container Capacity》）

- **map 容量提示**：`make(map[T1]T2, hint)` 尽量提供提示。与 slice 不同，map 容量提示**不保证**完整预分配，只用于估算所需 hash bucket 数——因此即使未超过指定容量，添加元素时仍可能发生分配。先取数据再带提示创建（`m := make(map[string]os.DirEntry, len(files))`），而不是无提示创建后填充。
- **slice 容量**：`make([]T, length, capacity)` 中容量不是提示——编译器按容量足额分配，此后 `append` 在长度达到容量之前**零分配**，超出后才扩容：

```go
data := make([]int, 0)          // Bad： BenchmarkBad-4   100000000  2.48s
data := make([]int, 0, size)    // Good：BenchmarkGood-4  100000000  0.21s
```

（Uber《Specifying Map Capacity Hints》《Specifying Slice Capacity》）

### 【参考】defer 的开销极小，不要在未证明前回避

`defer` 有极小的开销，只有当你能证明函数执行时间在纳秒量级时才应避免使用。使用 defer 带来的可读性收益远值得其微小成本——对包含简单内存访问之外更多计算的大方法尤其如此。（Uber《Defer to Clean Up》）

### 【参考】不要为微优化过早选择值接收者

值接收者可减少垃圾（传值调用时可用栈上拷贝代替堆分配，编译器会尽量优化但不总成功）；不要在没有先做 profile 的情况下以此为理由选择值接收者。（CRC《Receiver Type》）

### 【参考】用通信与缓冲通道实现免锁复用（leaky buffer）

并发工具也能让非并发思想更易表达。EG 给出一个从 RPC 包抽象出的例子：客户端用带缓冲通道 `freeList` 表示空闲链表（空则 `select`+`default` 分配新缓冲），服务端处理后 `select` 尝试把缓冲放回 freeList，放不进（满了）就直接丢弃交给 GC——几行代码就构建了依赖缓冲通道与垃圾回收记账的"漏桶"式 free list，全程没有互斥锁。（EG《A leaky buffer》）
