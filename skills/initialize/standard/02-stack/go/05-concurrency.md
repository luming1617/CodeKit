## 五、并发

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【推荐】通过通信共享内存

Go 鼓励在通道上传递共享值、而不是让多个执行线程主动共享数据的方式：任一时刻只有一个 goroutine 访问该值，从设计上杜绝数据竞争。口号是：**Don't communicate by sharing memory; share memory by communicating.**（不要通过共享内存来通信；而应通过通信来共享内存。）这种方式不应走极端——例如引用计数用互斥锁包住一个整数变量可能更合适；但作为高层策略，用通道控制访问更容易写出清晰正确的程序。该模型源于 Hoare 的 CSP（Communicating Sequential Processes），也可看作 Unix 管道的类型安全泛化。（EG《Share by communicating》）

### 【参考】goroutine 与通道的语言事实

- goroutine 轻量，成本仅约为分配栈空间；栈起始很小并按需增长；多个 goroutine 复用（multiplex）到多个 OS 线程上，一个阻塞（如等 I/O）不影响其他运行。`go` 前缀启动新 goroutine，调用完成时 goroutine **静默退出**。Go 的函数字面量是闭包，被引用的变量存活期自动延长。（EG《Goroutines》）
- 通道与 map 一样用 `make` 分配，返回值是底层数据的引用；可选整型参数设置缓冲大小，默认 0 即无缓冲（同步）通道。接收方总是阻塞到有数据可收；无缓冲通道的发送方阻塞到接收方取走值；有缓冲的发送方仅阻塞到值被拷入缓冲，缓冲满时须等有接收方取走。（EG《Channels》）
- 通道是一等值，可分配并像普通值一样传递，常用于实现安全的并行多路分发（请求对象内嵌 `resultChan`，各客户端自备应答路径）。（EG《Channels of channels》）
- 区分并发（concurrency，把程序组织为独立执行的组件）与并行（parallelism，多 CPU 上并行计算以求效率）：Go 是并发语言而非并行语言，并非所有并行问题都适合 Go 模型。若需尊重用户对资源的要求，用 `runtime.GOMAXPROCS(0)` 而非硬编码核数（它默认等于 `runtime.NumCPU()`，可被环境变量或调用覆盖）。（EG《Parallelization》）

### 【强制】通道容量通常取 1 或 0，其他容量必须经过严格审视

通道大小通常应为 1，或者无缓冲（默认，大小为 0）。任何其他容量都必须经过高标准的审视：这个大小是怎么定的、负载下什么能阻止通道写满阻塞写入者、写满了会发生什么。（Uber《Channel Size is One or None》）

```go
// Ought to be enough for anybody!
c := make(chan int, 64)         // Bad

c := make(chan int, 1)          // Good: Size of one
c := make(chan int)             // Good: Unbuffered channel, size of zero
```

无缓冲通道可以把通信（值的交换）和同步（保证两个 goroutine 处于已知状态）结合在一起；有缓冲通道也可以用作信号量限制并发吞吐（用带容量的通道在 `process` 前后收发值），或固定数量的 handler goroutine 从请求通道读取以限制并行度并良好管理资源。（EG《Channels》）

### 【强制】不要 fire-and-forget goroutine：必须有可预期的退出方式

goroutine 轻量但不是免费的：至少需要栈内存和调度 CPU；大量无受控生命周期地派生会造成显著性能问题，还可能阻止无用对象被回收、持有已不再使用的资源。因此生产代码中不得泄漏 goroutine；可能派生 goroutine 的包建议用 [go.uber.org/goleak](https://pkg.go.dev/go.uber.org/goleak) 测试泄漏。一般要求：每个 goroutine 要么有可预期的停止时刻，要么有向其发出停止信号的方法；并且两种情况下都必须有代码能阻塞等待其结束。（Uber《Don't fire-and-forget goroutines》）

```go
// Bad：无法停止，一直运行到进程退出
go func() {
  for {
    flush()
    time.Sleep(delay)
  }
}()

// Good：可停止、可等待
var (
  stop = make(chan struct{}) // tells the goroutine to stop
  done = make(chan struct{}) // tells us that the goroutine exited
)
go func() {
  defer close(done)

  ticker := time.NewTicker(delay)
  defer ticker.Stop()
  for {
    select {
    case <-ticker.C:
      flush()
    case <-stop:
      return
    }
  }
}()

// Elsewhere...
close(stop)  // signal the goroutine to stop
<-done       // and wait for it to exit
```

### 【强制】等待 goroutine 退出：WaitGroup 或 done channel

对派生出的 goroutine，必须有等待其退出的手段，两种常用方式（Uber《Wait for goroutines to exit》）：

```go
// 多个 goroutine 时用 sync.WaitGroup
var wg sync.WaitGroup
for i := 0; i < N; i++ {
  wg.Add(1)
  go func() {
    defer wg.Done()
    // ...
  }()
}
wg.Wait()

// 单个 goroutine 时加一个退出时 close 的 chan struct{}
done := make(chan struct{})
go func() {
  defer close(done)
  // ...
}()
<-done
```

### 【强制】init() 中不得启动 goroutine

`init()` 函数不应派生 goroutine（参见第四章《尽量避免 init()》）。若包确需后台 goroutine，必须暴露一个负责管理该 goroutine 生命周期的对象，并提供 `Close`/`Stop`/`Shutdown` 等方法发出停止信号并等待其退出：在 `init()` 里无条件 `go doWork()` 会让用户无法控制也无法停止该 goroutine；正确做法是 `NewWorker(...)` 时按需启动、`Shutdown()` 时 `close(stop)` 并 `<-done` 等待（worker 管理多个 goroutine 时改用 `WaitGroup`）。（Uber《No goroutines in init()》）

### 【强制】明确 goroutine 的生命周期

派生 goroutine 时，要让其退出时机清晰（甚至能否退出都清晰）。goroutine 会因阻塞在通道收发上而泄漏：即使所阻塞的通道已不可达，垃圾回收器也不会终止它。即使没有泄漏，在不再需要后仍让 goroutine 处于飞行状态也会引发难以诊断的问题：向已关闭通道发送会 panic；"结果不需要之后"仍在使用中的输入被修改依然会造成数据竞争；长时间存活的 goroutine 导致内存占用不可预测。尽量把并发代码写得简单到生命周期一目了然；实在做不到，就写文档说明这些 goroutine 何时、为何退出。（CRC《Goroutine Lifetimes》）

### 【强制】注意 goroutine/闭包捕获循环变量

在 1.22 之前的 Go 版本中，`for` 循环变量在所有迭代间共享，把 `req := <-queue` 的循环变量直接捕获进派生的 goroutine 会引入 bug（EG《Channels》对 gate goroutine 示例特别注明了此点）。并行测试（`t.Parallel()`）及类似的捕获引用/派生 goroutine 的循环，必须在循环体内显式声明本轮作用域的变量：

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

### 【强制】sync.Mutex 零值可用，几乎不需要指针，更不得嵌入

`sync.Mutex` 和 `sync.RWMutex` 的零值就是合法状态，因此几乎不需要持有互斥锁指针（`var mu sync.Mutex; mu.Lock()` 而不是 `mu := new(sync.Mutex)`）。若以指针方式使用结构体，mutex 应是其上的**非指针字段**；即使结构体未导出，也不得把 mutex 嵌入结构体——嵌入会让 `Lock`/`Unlock` 意外成为对外 API 的一部分，把锁及其方法作为实现细节隐藏于调用方之外。（Uber《Zero-value Mutexes are Valid》《Embedding in Structs》例外条款）

### 【强制】原子操作使用 go.uber.org/atomic

`sync/atomic` 包对裸类型（`int32`、`int64` 等）操作，很容易忘记某处读写应使用原子操作（如 `f.running == 1` 直接读原子变量即数据竞争）。[go.uber.org/atomic](https://pkg.go.dev/go.uber.org/atomic) 隐藏底层类型、为这些操作提供类型安全，并附带方便的 `atomic.Bool`：

```go
type foo struct {
  running atomic.Bool
}

func (f *foo) start() {
  if f.running.Swap(true) {
     // already running…
     return
  }
  // start the Foo
}

func (f *foo) isRunning() bool {
  return f.running.Load()
}
```

（Uber《Use go.uber.org/atomic》）

### 【强制】避免可变全局变量：用依赖注入

避免修改全局变量，改用依赖注入；对函数指针与其他值同样适用。把 `var _timeNow = time.Now` 在测试里临时替换再还原的写法，应改为结构体持有 `now func() time.Time` 字段、构造函数注入默认值、测试直接替换实例字段——更易测试且无还原遗漏风险。（Uber《Avoid Mutable Globals》）
