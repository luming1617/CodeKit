## 四、函数与方法

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】用多返回值替代"带内错误"返回值

Go 函数和方法可以返回多个值，用于改进 C 语言中 `-1` 表示 EOF、通过地址修改出参等笨拙惯用法。例如 `os` 包 `Write` 方法的签名同时返回计数和错误：

```go
func (file *File) Write(b []byte) (n int, err error)
```

当 `n != len(b)` 时返回非 nil 的 error。同样可以省去"传指针模拟引用出参"的写法：

```go
func nextInt(b []byte, i int) (int, int) {
    for ; i < len(b) && !isDigit(b[i]); i++ {
    }
    x := 0
    for ; i < len(b) && isDigit(b[i]); i++ {
        x = x*10 + int(b[i]) - '0'
    }
    return x, i
}
```

（EG《Multiple return values》）不要用 `-1`、`null` 这类带内错误值通知调用方失败（`Parse(Lookup(key))` 会把"键不存在"错报成"解析失败"）；应额外返回一个值（error，或无需解释时返回 bool）来标明其他返回值是否有效，并把它放在**最后**一个返回值位置：

```go
// Lookup returns the value for key or ok=false if there is no mapping for key.
func Lookup(key string) (value string, ok bool)
```

这样 `Parse(Lookup(key))` 会直接编译报错。该规则同样适用于未导出函数。`nil`、`""`、`0`、`-1` 只有在它们是函数的合法结果（调用方无需区别对待）时才可以直接返回；标准库 `strings` 包是返回带内错误值的例外，它简化了字符串操作代码但要求程序员更谨慎。（CRC《In-Band Errors》）

### 【强制】不要滥用命名返回值：以 godoc 视角衡量

命名返回值不是必需的，但名字相当于文档，且会被初始化为对应类型的零值、与 naked return 绑定后可简化代码（如 `func nextInt(b []byte, pos int) (value, nextPos int)`，以及 `io.ReadFull` 的 `func ReadFull(r Reader, buf []byte) (n int, err error) { ... return }`）。（EG《Named result parameters》）

考虑命名返回值在 godoc 中的样子：像 `func (n *Node) Parent1() (node *Node)`、`func (n *Node) Parent2() (node *Node, err error)` 这样的签名在文档里是重复啰嗦的，应写成 `func (n *Node) Parent1() *Node`、`func (n *Node) Parent2() (*Node, error)`。但当函数返回两个以上同类型参数、或返回值含义从上下文看不出来时，命名是有用的：

```go
// Location returns f's latitude and longitude.
// Negative values mean south and west, respectively.
func (f *Foo) Location() (lat, long float64, err error)
```

不要仅仅为了省掉函数内的一个变量声明而命名返回值——那是用不必要的 API 冗余换取微小的实现简洁。（CRC《Named Result Parameters》）

### 【强制】naked return 只用于几行长的短函数

不带参数的 `return` 返回命名返回值，称为 "naked" return：

```go
func split(sum int) (x, y int) {
    x = sum * 4 / 9
    y = sum - x
    return
}
```

函数只有几行长时 naked return 可以接受；一旦是中等大小的函数，就必须显式写明返回值。推论：不值得仅仅因为命名了返回值就使用 naked return——文档的清晰永远比在函数里省一两行重要。例外：有些场景必须在闭包 `defer` 中修改命名返回值，这种命名总是允许的。（CRC《Named Result Parameters》《Naked Returns》）

### 【强制】方法接收者命名：一两个字母的类型缩写，禁止 me/this/self

接收者名字应体现其身份：通常是类型名的一两个字母缩写（如 `Client` 用 `c` 或 `cl`）。不要使用 "me"、"this"、"self" 这类面向对象语言中带特殊含义的通用名——Go 的接收者只是另一个参数。接收者名不必像参数名那样具描述性（它几乎出现在该类型的每一行方法代码里，熟悉度允许其简短），但要保持一致：一个方法里叫 `c`，另一个方法里就不要叫 `cl`。（CRC《Receiver Names》）

### 【强制】接收者用值还是指针：按下表判断，拿不准用指针，且不混用

选择值接收者还是指针接收者可参考以下准则（CRC《Receiver Type》）：

- 接收者是 map、func 或 chan 时，不要使用它们的指针；接收者是 slice 且方法不重新切片/不扩容时，也不要使用指针。
- 方法需要修改接收者时，接收者必须是指针。
- 接收者是包含 `sync.Mutex` 或类似同步字段的结构体时，必须用指针接收者以避免拷贝。
- 接收者是大型 struct 或数组时，指针接收者更高效。"大"的判断标准：等价于把所有元素作为参数传给方法——若那样显得太大，对接收者来说也太大了。
- 若该接收者可能被并发修改（本方法内或其他方法），值类型会在调用时拷贝一份、外部更新对这份接收者不可见；需要看到原始修改时必须用指针。
- 接收者是 struct/数组/slice 且其中任一元素是指向可能被修改对象的指针时，优先用指针接收者，意图对读者更清晰。
- 接收者是天然是值类型的小数组或 struct（如 `time.Time`）、无可变字段且无指针，或只是 int、string 这类简单基本类型时，值接收者合理。值接收者可减少垃圾产生（传值调用可避免堆分配），但未经 profile 不要为此选值接收者。
- 不要混用接收者类型：所有方法统一选择指针或值。
- 最后，拿不准就用指针接收者。

值方法可以在指针和值上调用，但指针方法只能在指针（或可寻址的值）上调用：语言规定指针方法会修改接收者，对值调用会丢弃修改，因此被禁止； handy 的例外是当值可寻址时，编译器自动插入取地址操作（`b.Write` 被重写为 `(&b).Write`）。例如只有 `*ByteSlice` 满足 `io.Writer`。（EG《Pointers vs. Values》）同理，map 中存放的值不可寻址，因此 `map[int]S` 的值不能调用指针接收者方法（`sVals[1].Write(...)` 编译不过），而 `map[int]*S` 可以同时调用值接收者和指针接收者方法；接口可以由指针满足，即使方法声明为值接收者。（Uber《Receivers and Interfaces》）

### 【强制】不要为省几字节而传指针

不要仅仅为了"省几个字节"就传指针参数。若函数自始至终只以 `*x` 的形式使用参数 `x`，那这个参数就不该是指针。常见误例：传字符串指针（`*string`）或接口指针（`*io.Reader`）——这两种值本身定长，应直接传值。该建议不适用于大型 struct，也不适用于可能长大的小 struct。（CRC《Pass Values》）

### 【强制】优先写同步函数

优先使用同步函数——直接返回结果、或在返回前完成所有回调/通道操作的函数——而非异步函数。同步函数把 goroutine 局限在一次调用内，便于推理其生命周期、避免泄漏和数据竞争，也更容易测试（调用方传输入、查输出，无需轮询或同步）。调用方若需要更多并发，把函数放进单独的 goroutine 调用即可；反过来，在调用方一侧移除不必要的并发非常困难，有时甚至不可能。（CRC《Synchronous Functions》）

### 【强制】Context 作为第一个参数显式传递，不存进 struct

`context.Context` 携带安全凭据、追踪信息、deadline 和取消信号跨越 API 与进程边界；程序应沿完整调用链（从入站 RPC/HTTP 请求到出站请求）显式传递 Context。使用规则（CRC《Contexts》）：

- 大多数使用 Context 的函数应把它作为第一个参数：`func F(ctx context.Context, /* other arguments */) {}`。
- 与请求无关的函数可用 `context.Background()`，但宁可传 Context 也不要武断认为自己不需要；默认是传，直接用 `context.Background()` 必须有很好的理由。
- 不要在 struct 类型上加 Context 成员；应在需要向下传递的每个方法签名上加 ctx 参数。唯一例外是方法签名必须匹配标准库或第三方库接口的情况。
- 不要自定义 Context 类型，也不要在函数签名中使用 `Context` 以外的接口。
- 需要传递应用数据时，放进参数、接收者、全局变量，或确实属于 Context 时再放入 Context 值。
- Context 是不可变的，同一个 ctx 可以安全地传给共享同一 deadline、取消信号、凭据、父 trace 的多个调用。

### 【推荐】文件内函数按调用顺序与接收者分组

函数应按大致调用顺序排列，同一文件中的函数按接收者分组。因此导出函数应排在文件前部（位于 `struct`、`const`、`var` 定义之后）；`newXYZ()`/`NewXYZ()` 出现在类型定义之后、该接收者的其余方法之前；由于函数按接收者归组，纯工具函数应放在文件末尾。（Uber《Function Grouping and Ordering》）

### 【强制】避免裸参数：含义不明的实参加 C 风格注释或改用自定义类型

函数调用中的裸参数会伤害可读性。当参数含义不明显时，为实参添加 C 风格注释标明参数名：

```go
// func printInfo(name string, isLocal, done bool)

printInfo("foo", true /* isLocal */, true /* done */)
```

更好的做法是用自定义类型替代裸 `bool`，既更易读又获得类型安全，且未来该参数可以支持不止两个状态：

```go
type Region int

const (
  UnknownRegion Region = iota
  Local
)

type Status int

const (
  StatusReady Status = iota + 1
  StatusDone
  // Maybe we will have a StatusInProgress in the future.
)

func printInfo(name string, region Region, status Status)
```

（Uber《Avoid Naked Parameters》）

### 【强制】Printf 风格函数命名：能被 go vet 识别

声明 `Printf` 风格的函数时，确保 `go vet` 能检测到并检查格式串：优先使用 `go vet` 预定义的 Printf family 名称；若不可行，自选名称必须以 `f` 结尾（`Wrapf` 而不是 `Wrap`），这样可以要求 go vet 检查：`go vet -printfuncs=wrapf,statusf`。（Uber《Naming Printf-style Functions》）

### 【推荐】可选参数用 Functional Options 模式

对预见需要扩展的构造函数等公开 API（尤其已有三个以上参数时），使用 functional options：声明一个不透明的 `Option` 类型，把信息记录在内部 struct 上，接受可变数量的 option 并据此作用于内部 struct：

```go
// package db

type Option interface {
  // ...
}

func WithCache(c bool) Option {
  // ...
}

func WithLogger(log *zap.Logger) Option {
  // ...
}

// Open creates a connection.
func Open(
  addr string,
  opts ...Option,
) (*Connection, error) {
  // ...
}
```

调用方只在需要时提供选项：`db.Open(addr)`、`db.Open(addr, db.WithCache(false), db.WithLogger(log))`，而不必像固定参数签名那样总是传满默认值。推荐实现方式：`Option` 接口持有未导出方法 `apply(*options)`，各 option 类型实现 `apply` 把值写入未导出的 `options` struct，构造函数先填默认值再逐个应用 option。（Uber《Functional Options》）

### 【强制】尽量避免 init()；不可避免时满足四条要求

尽可能避免 `init()`。当 `init()` 不可避免或确实合适时，代码应力求（Uber《Avoid init()》）：

1. 完全确定（deterministic），与程序运行环境和调用方式无关。
2. 不依赖其他 `init()` 函数的顺序或副作用。`init()` 的顺序虽众所周知，但代码会变，函数之间的依赖关系会让代码脆弱易错。
3. 不访问或修改全局/环境状态，如机器信息、环境变量、工作目录、程序参数/输入等。
4. 不做 I/O，包括文件系统、网络和系统调用。

不能满足这些要求的代码应作为 helper 在 `main()`（或程序生命周期其他位置）中调用，或直接写进 `main()`。被其他程序使用的库尤其要注意完全确定、不搞 "init 魔法"。反例：在 `init()` 里 `os.Getwd()`（依赖当前目录）+ `os.ReadFile()`（I/O）加载配置——应改为 `loadConfig()` 函数；给 `_defaultFoo` 赋值的 `init()` 也应改为变量声明处初始化或 `defaultFoo()` 函数。合理的使用场景包括：无法用单个赋值表达的复杂表达式、可插拔钩子（如 `database/sql` 方言注册、编码类型注册表）、以及对 Google Cloud Functions 等确定性预计算的优化。（Uber《Avoid init()》）

### 【参考】init 的执行时序与用途

每个源文件都可以定义自己的无参 `init` 函数来建立所需状态（实际上每个文件可以有多个 `init` 函数）。"最后"是真正的最后：`init` 在包内所有变量声明完成初始化求值之后才被调用，而这些求值又发生在所有导入包完成初始化之后。除了无法表达为声明的初始化，`init` 的常见用途是在真正执行开始前校验或修复程序状态的正确性（EG《The init function》），例如：

```go
func init() {
    if user == "" {
        log.Fatal("$USER not set")
    }
    if home == "" {
        home = "/home/" + user
    }
    if gopath == "" {
        gopath = home + "/go"
    }
    // gopath may be overridden by --gopath flag on command line.
    flag.StringVar(&gopath, "gopath", gopath, "override default GOPATH")
}
```
