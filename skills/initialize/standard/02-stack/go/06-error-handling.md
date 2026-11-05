## 六、错误处理

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】按约定用 error 类型返回详细错误，而不是仅返回失败标志

库例程经常要向调用方返回错误指示。Go 的多返回值使"在正常返回值旁同时返回详细错误描述"很容易，应当用好这一特性：`os.Open` 失败时不只返回 nil 指针，还返回描述问题所在的 error。按约定错误类型为内建接口：

```go
type error interface {
    Error() string
}
```

库实现者可以在底层实现该接口的更丰富模型，从而不仅报告错误、还能携带上下文。`os.Open` 在出问题时返回 `os.PathError`：

```go
// PathError records an error and the operation and
// file path that caused it.
type PathError struct {
    Op string    // "open", "unlink", etc.
    Path string  // The associated file.
    Err error    // Returned by the system call.
}

func (e *PathError) Error() string {
    return e.Op + " " + e.Path + ": " + e.Err.Error()
}
```

生成的字符串形如 `open /etc/passwx: no such file or directory`，包含问题文件名、操作和触发它的系统错误，即使在离调用点很远处打印也比光秃秃的 "no such file or directory" 有用得多。关心精确错误细节的调用方可用类型 switch 或类型断言查找特定错误并提取细节，例如检查内部 `Err` 字段判断是否可恢复：

```go
for try := 0; try < 2; try++ {
    file, err = os.Create(filename)
    if err == nil {
        return
    }
    if e, ok := err.(*os.PathError); ok && e.Err == syscall.ENOSPC {
        deleteTempFiles()  // Recover some space.
        continue
    }
    return
}
```

（EG《Errors》）

### 【强制】错误字符串：不大写、不以标点结尾、能识别来源

错误字符串通常会被拼接在其他上下文之后打印，因此不应大写开头（专有名词或首字母缩略词除外），也不应以标点结尾：用 `fmt.Errorf("something bad")` 而不是 `fmt.Errorf("Something bad")`，这样 `log.Printf("Reading %s: %v", filename, err)` 才不会在消息中间冒出莫名的大写字母。日志不受此限（日志天然按行组织、不会嵌入其他消息中间）。（CRC《Error Strings》）在可行的前提下，错误字符串应标明来源，例如以产生它的操作或包名作前缀：`image` 包中未知格式解码错误的字符串表示是 `"image: unknown format"`。（EG《Errors》）

### 【强制】不得用 `_` 丢弃错误

只要函数返回 error，就必须检查它以确保函数成功，然后处理、返回，或在真正特殊的场景下 panic——不得用空白标识符丢弃错误值。（CRC《Handle Errors》）为忽略错误而丢弃 error 返回值是极糟糕的实践。EG 同样给出反例：

```go
// Bad! This code will crash if path does not exist.
fi, _ := os.Stat(path)
if fi.IsDir() {
    fmt.Printf("%s is a directory\n", path)
}
```

空白标识符的正确用法是丢弃"确实不关心"的其他值（如只关心 error 时 `if _, err := os.Stat(path); ...`）。（EG《The blank identifier in multiple assignment》）

### 【强制】按"是否需要匹配 × 静态还是动态"选择错误的声明方式

（Uber《Error Types》）

| 调用方需要匹配错误？ | 错误消息 | 指导 |
|---|---|---|
| 否 | 静态 | `errors.New` |
| 否 | 动态 | `fmt.Errorf` |
| 是 | 静态 | 顶层 `var` + `errors.New`（导出为 `ErrXxx` 供 `errors.Is` 匹配） |
| 是 | 动态 | 自定义 `error` 类型（供 `errors.As` 匹配） |

```go
// 不需要匹配
func Open() error { return errors.New("could not open") }

// 需要匹配：导出错误变量
var ErrCouldNotOpen = errors.New("could not open")
func Open() error { return ErrCouldNotOpen }
// 调用方： errors.Is(err, foo.ErrCouldNotOpen)

// 动态且需要匹配：自定义类型
type NotFoundError struct{ File string }
func (e *NotFoundError) Error() string {
    return fmt.Sprintf("file %q not found", e.File)
}
// 调用方： var notFound *NotFoundError; errors.As(err, &notFound)
```

注意：从包中导出的错误变量或类型会成为包的公共 API 的一部分。（Uber《Error Types》）

### 【强制】错误传播：原样返回、%w 加上下文或 %v 加上下文

调用失败时传播错误有三种选择（Uber《Error Wrapping》）：

- **原样返回**底层错误：没有额外上下文可加时采用，保留原类型与消息；底层消息信息已足够定位来源时很适合。
- **`fmt.Errorf` + `%w`**：调用方应能匹配/提取底层原因时用。这是多数包装场景的良好默认，但要注意调用方可能开始依赖此行为——被包装错误是已知 `var` 或类型时，应把它作为函数契约的一部分写入文档并测试。
- **`fmt.Errorf` + `%v`**：需要隐匿底层错误时用（调用方无法匹配，未来仍可切换到 `%w`）。

添加上下文时保持简洁，避免"failed to"这类不言自明、且随错误向上传递不断堆叠的措辞：

```go
// Bad： failed to x: failed to y: failed to create new store: the error
return fmt.Errorf("failed to create new store: %w", err)

// Good： x: y: new store: the error
return fmt.Errorf("new store: %w", err)
```

但当错误将发送到其他系统时，消息应能明确是错误（如日志中的 `err` 标签或 "Failed" 前缀）。（Uber《Error Wrapping》）

### 【强制】错误命名：全局变量用 Err/err 前缀，自定义类型用 Error 后缀

（Uber《Error Naming》，此规则优先于第一章提到的"未导出全局量加 `_` 前缀"）

```go
var (
    // 导出：让包用户可用 errors.Is 匹配
    ErrBrokenLink   = errors.New("link is broken")
    ErrCouldNotOpen = errors.New("could not open")

    // 不导出：不作为公共 API，但包内仍可用 errors.Is
    errNotFound = errors.New("not found")
)

// 自定义错误类型用 Error 后缀
type NotFoundError struct { File string }   // 导出，供 errors.As
type resolveError   struct { Path string }  // 未导出，包内供 errors.As
```

### 【强制】每个错误只处理一次

调用方收到被调方错误后可以匹配（`errors.Is`/`errors.As`）分支处理、记日志并优雅降级、返回定义良好的领域错误、或（包装后）原样上抛——但通常应**只处理一次**。不要"记了日志又 return err"：它的上层调用方也可能要处理同一错误，层层重复记录会在日志里制造大量无价值噪音。（Uber《Handle Errors Once》）

```go
// Bad：记日志又返回，上层多半还会重复同样动作
u, err := getUser(id)
if err != nil {
  log.Printf("Could not get user %q: %v", id, err)
  return err
}

// Good：包装并返回，上层用 errors.Is/As 仍可匹配
if err != nil {
  return fmt.Errorf("get user %q: %w", id, err)
}

// Good：错误可恢复时，记日志并优雅降级（如指标写出失败不应中断应用）
if err := emitMetrics(); err != nil {
  log.Printf("Could not emit metrics: %v", err)
}

// Good：匹配被调方契约中的特定错误并降级，其余包装上抛
tz, err := getUserTimeZone(id)
if err != nil {
  if errors.Is(err, ErrUserNotFound) {
    tz = time.UTC
  } else {
    return fmt.Errorf("get user %q: %w", id, err)
  }
}
```

### 【强制】类型断言必须用 "comma, ok" 形式

单返回值形式的类型断言在类型不符时会 panic，因此始终使用 "comma ok" 惯用法：

```go
// Bad
t := i.(string)

// Good
t, ok := i.(string)
if !ok {
  // handle the error gracefully
}
```

断言失败时 `t` 仍存在且为 string 类型，但值为零值（空字符串）。（Uber《Handle Type Assertion Failures》；EG《Interface conversions and type assertions》）

### 【强制】生产代码不要 panic

生产环境中运行的代码必须避免 panic——panic 是级联故障（cascading failure）的重要来源。出错时函数必须返回 error，让调用方决定如何处理；不能用 panic 做常规错误处理，应使用 error 与多返回值。（Uber《Don't Panic》；CRC《Don't Panic》）真正的库函数应避免 panic：问题能被掩盖或绕过时，让程序继续运行永远比掀翻整个进程好。（EG《Panic》）

panic/recover 不是错误处理策略。程序只应在发生不可恢复之事（如 nil 解引用）时 panic；例外是程序初始化阶段：启动时发生的、应当中止程序的坏情况可以 panic——EG《Panic》指出真正的库函数应避免 panic，但"库确实无法完成自我初始化"时 panic 是合理的（示例：`init()` 中 `if user == "" { panic("no value for $USER") }`）；Uber 给出的正例是 `var _statusTemplate = template.Must(template.New("name").Parse("_statusHTML"))`。即使在测试中，也用 `t.Fatal`/`t.FailNow` 而非 panic 来确保测试被标记为失败。（Uber《Don't Panic》）

### 【参考】recover 的语义与包内使用边界

`panic` 被调用（包括越界索引、断言失败等隐式运行时错误）时立即停止当前函数执行，开始展开（unwind）该 goroutine 的栈并依次运行 defer 函数；若展开到 goroutine 栈顶，程序死亡。内建函数 `recover` 可停止展开并返回 panic 的参数；由于展开期间只有 defer 函数内的代码会运行，`recover` 只在 defer 函数中有用。一个应用是让服务器中某个出错 goroutine 干净退出而不波及其他 goroutine：

```go
func safelyDo(work *Work) {
    defer func() {
        if err := recover(); err != nil {
            log.Println("work failed:", err)
        }
    }()
    do(work)
}
```

`recover` 除非直接在 defer 函数内调用，否则总返回 nil，因此 defer 代码可安全调用同样使用 panic/recover 的库例程。defer 函数可以修改命名返回值（如 idealized `regexp.Compile` 在恢复块中置 `regexp = nil`、用 `e.(Error)` 断言把内部 panic 转成 error，断言失败即"re-panic"继续展开）。此类 panic/recover 模式应**只在包内**使用：Parse 把内部 panic 调用转成 error 值，不向客户端暴露 panic——这是值得遵循的好规则。（EG《Recover》）

### 【强制】os.Exit / log.Fatal 只能在 main() 中调用，且尽量只调一次

Go 程序用 `os.Exit` 或 `log.Fatal*` 立即退出（panic 不是好的退出方式）。`os.Exit` 或 `log.Fatal*` 的调用只应出现在 `main()` 中，其他所有函数都应返回 error 表示失败。理由：任何函数都能退出程序会让控制流难以推理；会连带退出正在测试它的测试进程、跳过后续测试；会跳过已排队的 `defer` 清理。可能的话，`main()` 中最多调用一次 `os.Exit`/`log.Fatal`：把多分支失败逻辑放进单独的 `run()` 函数并向上返回 error（或返回退出码），使 `main()` 保持短小、业务逻辑可测试：

```go
func main() {
  if err := run(); err != nil {
    log.Fatal(err)
  }
}

func run() error {
  args := os.Args[1:]
  if len(args) != 1 {
    return errors.New("missing file")
  }
  name := args[0]

  f, err := os.Open(name)
  if err != nil {
    return err
  }
  defer f.Close()

  b, err := io.ReadAll(f)
  if err != nil {
    return err
  }
  // ...
}
```

`run()` 的命名、签名和组织方式有灵活性（可接收已解析参数、可用自定义 error 携带退出码、可把业务逻辑放到 `package main` 之外的层）；本条要求只是：**实际退出进程的职责集中在 `main()` 的一个位置**。（Uber《Exit in Main》《Exit Once》）
