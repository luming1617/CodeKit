## 三、控制流

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】控制结构与 C 的差异：无 do/while，if/for/switch 无括号、体必须有大括号

Go 没有 `do` 或 `while` 循环，只有泛化的 `for`；`switch` 更灵活；`if` 和 `switch` 接受类似 `for` 的可选初始化语句；`break`/`continue` 可带标签；另有类型 switch 与多路通信复用器 `select`。语法上条件表达式不加圆括号，但语句体必须始终用大括号包围。（EG《Control structures》）

### 【强制】用短 if（初始化语句）收紧临时变量作用域

`if` 接受的初始化语句常用于声明局部变量：

```go
if err := file.Chmod(0664); err != nil {
    log.Print(err)
    return err
}
```

尽可能缩小变量与常量的作用域：函数调用结果只在 `if` 内使用时，必须写成短 if 形式（`if err := os.WriteFile(...); err != nil`），不要先在外层声明 `err`。但若结果确实需要在 `if` 之外使用，就不要强行收紧（反例：把主逻辑塞进 `if err == nil { ... } else { return err }`）。常量除非被多个函数/文件使用或属于包的对外契约，也不必放全局。（Uber《Reduce Scope of Variables》）

### 【强制】if 体以 return/break/continue 结尾时省略多余的 else

库代码惯例：当 `if` 语句不会流入下一条语句（体以 `break`、`continue`、`goto` 或 `return` 结束）时，省略不必要的 `else`。错误序列的守卫代码应当让成功流程一路向下、逐个消灭错误分支，因此无需 `else`：

```go
f, err := os.Open(name)
if err != nil {
    return err
}
codeUsing(f)
```

（EG《If》）若 if/else 两个分支只是给同一变量赋值，也应去掉 else：先赋默认值，再在 if 中覆盖（`a := 10; if b { a = 100 }`）。（Uber《Unnecessary Else》）

### 【强制】先处理错误/特殊条件并提前返回，减少嵌套

代码应尽可能减少嵌套：先处理错误情况与特殊条件并提前 `return` 或 `continue`，压缩多层嵌套的代码量（配合上一条"缩短变量作用域"冲突时，以保证正确性为先）。（Uber《Reduce Nesting》）

### 【强制】`:=` 重复声明规则：同作用域内 err 复用是合法的

`d, err := f.Stat()` 中的 `err` 若已在同一作用域由前一条 `:=` 声明，则这里是重新赋值而非新声明。允许的前提：① 与已有声明同作用域（外层作用域已声明的会创建新变量）；② 对应初值可赋给该变量；③ 同一语句至少声明一个新变量。此务实设计让长 if-else 链可复用同一个 `err`。注意：函数参数与返回值的作用域与函数体相同。（EG《Redeclaration and reassignment》）

### 【强制】for 的三种形式与 range

```go
for init; condition; post { }   // 类 C for
for condition { }               // 类 C while
for { }                         // 类 C for(;;)
```

用短声明把下标变量声明在循环内（`for i := 0; i < 10; i++`）。遍历 array/slice/string/map/通道时用 `range`：只需要 key/下标就省略第二项；只需要值就用空标识符丢弃第一项（`for _, value := range array`）。（EG《For》）

### 【参考】range 遍历字符串按 Unicode 码点解码

对 string 使用 range 会解析 UTF-8 逐个产出码点；非法编码消耗一个字节并产出替换符 U+FFFD。`rune` 是 Go 对单个 Unicode 码点的术语。（EG《For》）

### 【强制】多变量并行推进用并行赋值

Go 没有逗号运算符，`++`/`--` 是语句不是表达式；要在 for 中同时推进多个变量应使用并行赋值（此时不能用 `++`/`--`）：

```go
for i, j := 0, len(a)-1; i < j; i, j = i+1, j-1 {
    a[i], a[j] = a[j], a[i]
}
```

（EG《For》）

### 【强制】switch：表达式不限类型、自上而下匹配、无自动穿透

`switch` 的表达式不必是常量甚至不必是整数；case 自上而下求值直到命中；无表达式时相当于对 `true` 做 switch，因此惯用 `switch { case cond1: ... }` 替代 if-else-if 链。没有自动 fall through，需要时用逗号并列多个 case。`break` 可提前跳出 switch；要跳出外层循环则给循环打标签后 `break Label`。`continue` 同样接受标签但只用于循环。（EG《Switch》）

### 【强制】类型 switch 中按分支复用变量名

用 `switch t := t.(type)` 探测接口动态类型时，惯用法是每个 case 复用同一变量名，使其在各分支具有对应类型；`default` 分支配合 `%T` 报告意外类型。（EG《Type switch》）

### 【强制】用 defer 清理文件、锁等资源

对必须释放的资源（解锁互斥量、关闭文件等）一律用 `defer`。多返回路径下手写 `Unlock`/`Close` 极易遗漏；`defer` 保证释放不依赖返回路径，且让 close 紧挨 open 写在函数开头，比放在函数尾部更清晰。（Uber《Defer to Clean Up》；EG《Defer》）

### 【参考】defer 的求值时机与 LIFO 顺序

deferred 函数在其所在函数即将返回前执行；参数（方法则含接收者）在 `defer` 语句执行时求值，而非调用发生时，因此一个 defer 点可登记多次执行（如 `defer fmt.Printf("%d ", i)` 在循环中按 LIFO 打印 `4 3 2 1 0`，以及 `defer un(trace("a"))` 追踪出入）。`defer` 基于函数而非块，是其最强大之处的来源。defer 开销极小，除非能证明函数执行时间在纳秒量级，否则可读性收益始终值得使用它。（EG《Defer》；Uber《Defer to Clean Up》）

### 【参考】不要给不需要并发的场景制造异步

（内容见第四章"同步函数"。）
