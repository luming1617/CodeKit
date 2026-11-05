## 十二、格式化输出（fmt）

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【参考】fmt 家族比 C 的 printf 更丰富、更通用

格式化打印函数位于 `fmt` 包，名称大写：`fmt.Printf`、`fmt.Fprintf`、`fmt.Sprintf` 等；字符串函数（`Sprintf` 等）返回字符串而非填充传入缓冲。可以不传格式串：`Printf`、`Fprintf`、`Sprintf` 各另有成对函数如 `Print`、`Println`——它们为每个参数生成默认格式；`Println` 版本还在参数间插入空格并在输出末尾追加换行，`Print` 版本仅当两侧操作数都不是字符串时才加空格。以下每行产生相同输出：

```go
fmt.Printf("Hello %d\n", 23)
fmt.Fprint(os.Stdout, "Hello ", 23, "\n")
fmt.Println("Hello", 23)
fmt.Println(fmt.Sprint("Hello ", 23))
```

`fmt.Fprint` 等格式化函数的第一个参数接受任何实现 `io.Writer` 接口的对象（`os.Stdout`、`os.Stderr` 即常见实例）。与 C 的分岔：数字格式如 `%d` 不接受符号性或大小的 flag，打印例程按参数的类型决定这些属性：

```go
var x uint64 = 1<<64 - 1
fmt.Printf("%d %x; %d %x\n", x, x, int64(x), int64(x))
// 18446744073709551615 ffffffffffffffff; -1 -1
```

（EG《Printing》）

### 【参考】常用动词：%v / %+v / %#v / %q / %x / %T

只想要默认转换（如整数十进制）用万能格式 `%v`，结果与 `Print`/`Println` 完全相同，且可打印任何值——数组、slice、struct、map 都行；对 map，`Printf` 等按键的字典序排序输出（`map[CST:-21600 EST:-18000 MST:-25200 PST:-28800 UTC:0]`）。打印 struct 时 `%+v` 会标注字段名；`%#v` 以完整 Go 语法打印值：

```go
t := &T{7, -2.35, "abc\tdef"}
fmt.Printf("%v\n", t)   // &{7 -2.35 abc   def}
fmt.Printf("%+v\n", t)  // &{a:7 b:-2.35 c:abc     def}
fmt.Printf("%#v\n", t)  // &main.T{a:7, b:-2.35, c:"abc\tdef"}
```

引号字符串格式 `%q` 适用于 string 或 []byte，`%#q` 在可能时用反引号；`%q` 也适用于整数和 rune（产生单引号 rune 常量）。`%x` 除整数外也作用于字符串、字节数组和字节 slice，生成长十六进制串；格式中留空格（`% x`）则在字节间加空格。`%T` 打印值的类型（如 `map[string]int`）。（EG《Printing》）

### 【强制】自定义类型的 String 方法不得无限递归

要控制自定义类型的默认打印格式，只需为该类型定义签名为 `String() string` 的方法（如 `func (t *T) String() string { return fmt.Sprintf("%d/%g/%q", t.a, t.b, t.c) }`，`%v` 即输出 `7/-2.35/"abc\tdef"`；若还要打印 T 的值而非指针，`String` 的接收者必须是值类型）。打印例程是完全可重入的、可以这样包装，但有一个重要细节：**不要让 String 方法中的 Sprintf 调用再次落到自己的 String 方法上**，这会无限递归——

```go
type MyString string

func (m MyString) String() string {
    return fmt.Sprintf("MyString=%s", m) // Error: will recur forever.
}
```

修复也很容易：把参数转换成没有该方法的基本 string 类型：

```go
func (m MyString) String() string {
    return fmt.Sprintf("MyString=%s", string(m)) // OK: note conversion.
}
```

（EG《Printing》；避免同类递归的另一技巧见 EG《Conversions》——Sequence 的 String 方法先 `fmt.Sprint([]int(s))` 转换类型再打印）

### 【参考】把打印参数直接转发给另一个打印例程

`Printf` 的签名用 `...interface{}` 作末参数（任意数量、任意类型）；函数体内 `v` 表现为 `[]interface{}` 变量，但传给另一个可变参数函数时表现为普通参数列表。`log.Println` 的实现即把参数直接传给 `fmt.Sprintln`：

```go
// Println prints to the standard logger in the manner of fmt.Println.
func Println(v ...interface{}) {
    std.Output(2, fmt.Sprintln(v...))  // Output takes parameters (int, string)
}
```

嵌套调用 `Sprintln(v...)` 中的 `...` 告诉编译器把 `v` 当作参数列表，否则只会作为单个 slice 参数传入。`...` 参数也可以是具体类型，如 `func Min(a ...int) int`。（EG《Printing》）

### 【强制】与 fmt 相关的 Uber 补充规则

在 Printf 风格函数之外声明的格式串必须是 `const` 值（帮助 `go vet` 对格式串做静态分析）：`const msg = "unexpected values %v, %v\n"`。（Uber《Format Strings outside Printf》）自定义 Printf 风格函数的命名规则与 `go vet -printfuncs` 见第四章《Printf 风格函数命名》。（Uber《Naming Printf-style Functions》）
