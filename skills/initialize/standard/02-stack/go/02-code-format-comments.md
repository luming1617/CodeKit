## 二、代码格式、注释与声明组织

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】所有代码必须经 gofmt 格式化，格式分歧以 gofmt 输出为准

- 用 gofmt（或按包操作的 `go fmt`）自动修掉绝大多数机械式风格问题；野外的 Go 代码几乎全部使用 gofmt，它处理不了的非机械风格点由其余规约约束（CRC《Gofmt》）。
- 遇到新的布局情况拿不准，直接跑 gofmt；结果不满意就改程序结构（或给 gofmt 提 bug），不要绕过它（EG《Formatting》）。
- 标准库全部代码都用 gofmt 格式化过；结构体字段注释对齐交给 gofmt 做，不必手工对齐（EG 示例：`name string // name of the object` 会被 gofmt 自动列对齐）。
- 一个例外是缩进：Go 用制表符缩进，gofmt 默认输出制表符，非不得已不要用空格（EG《Formatting》）。
- gofmt 的超集 `goimports` 会按需增删 import 行，推荐用它（CRC《Gofmt》；Uber《Introduction》建议编辑器保存时运行 goimports）。

### 【强制】不手写分号；控制语句左花括号不得另起一行

Go 的词法器在"能结束语句的词元"（标识符、字面量、`break continue fallthrough return ++ -- ) }`）后遇换行自动插入分号，因此源码基本不写分号；分号只出现在 for 循环子句和多语句一行中。推论：`if/for/switch/select` 的左花括号必须与语句头同行——

```go
if i < f() {    // 正确
    g()
}

if i < f()      // 错误：左花括号另起一行
{               // 错误
    g()
}
```

另起一行的 `{` 会因分号插入产生非预期效果（EG《Semicolons》）。

### 【强制】行宽：无硬性限制，避免不舒服的长行，软上限 99 字符

- Go 没有行长限制；一行感觉太长就折行并额外加一个制表符缩进（EG《Formatting》）。
- Uber 建议软性行宽上限 **99 字符**，作者应尽量在达到前折行，但这不是硬限制，允许超出（Uber《Avoid overly long lines》）；避免需要横向滚动或"摇头"阅读的行。
- 折行应因语义而断，不因行长而断；为缩短行长而给本就可读的重复性长行加换行是错的。若折行显得"不自然"（在函数调用/声明中间断开），多半是参数太多、名字太长，改名字或改语义（CRC《Line Length》）。
- 函数长度同理：没有"不得超过 N 行"的规则，但确实存在过长函数和过多琐碎小函数，解法是调整函数边界而不是数行数（CRC《Line Length》）。

### 【强制】说明性注释写成完整句子，以被说明物名开头、句号结尾

文档注释是 Go 包/命令的主要文档，应写成完整句子以便 godoc 提取展示：

```go
// Request represents a request to run a command.
type Request struct { ... }

// Encode writes the JSON encoding of req to w.
func Encode(w io.Writer, req *Request) { ... }
```

（EG《Commentary》；CRC《Comment Sentences》）

### 【强制】所有顶层导出名要有文档注释；非平凡的未导出类型/函数也要

所有顶层导出名应有文档注释；不平凡的未导出类型或函数声明同样要写（CRC《Doc Comments》）。紧邻声明（中间无空行）的注释即为该声明的 doc comment（EG《Commentary》）。

### 【强制】包注释紧贴 package 语句、无空行；package main 有专用格式

```go
// Package math provides basic constants and mathematical functions.
package math
```

多行说明可用 `/* ... */` 块注释紧贴 package。对 `package main`，二进制名之后的注释风格不限（且可大写开头），如 `// Binary seedgen ...`、`// Command seedgen ...`、`// The seedgen program ...`、`// Seedgen ..` 等；以小写词开头不可接受——包注释是公开可见内容，须用规范英语，句首单词大写（即使与命令行调用拼写不完全一致）。（CRC《Package Comments》）

### 【参考】注释形态：行注释为主流，块注释用于包注释和表达式内

Go 提供 C 风格 `/* */` 与 C++ 风格 `//`；行注释是常态，块注释主要作包注释，也可用于表达式内部或临时屏蔽大段代码。（EG《Commentary》）

### 【强制】相似的声明归组书写

`import`、`const`、`var`、`type` 都支持括号归组（`import ("a"; "b")` 代替两条 `import`）。只归组相关声明，无关声明不得混入同一组（反例：枚举 `iota` 组里塞 `EnvVar = "MY_ENV"`）。归组不限于顶层，函数内也可用。例外：函数内相邻声明的变量即使不相关也应归入同一个 `var` 组。（Uber《Group Similar Declarations》）

### 【强制】import 分组：标准库一组，其余一组，空行分隔

两组导入（标准库在前，其余在后）是 goimports 的默认分组，禁止全部混在一组（Uber《Import Group Ordering》；CRC《Imports》：标准库永远在第一组）。

### 【强制】import 别名受限使用

- 包名与导入路径最后一个元素不一致时，必须使用别名（如 `client "example.com/client-go"`）（Uber《Import Aliasing》）。
- 其他场景除非导入之间有直接命名冲突，避免别名（Uber）；除规避冲突外不要重命名导入，好的包名不需要改名；发生冲突时优先重命名更局部/项目专属的那个导入（CRC《Imports》）。反例：给 `runtime/trace` 起 `runtimetrace` 别名。

### 【强制】blank import（`import _ "pkg"`）只允许出现在 main 包和确实需要的测试中

（CRC《Import Blank》）为副作用导入包（如 `net/http/pprof` 在 init 中注册调试 handler）时，把包名重命名为 `_` 表明只取副作用。（EG《Import for side effect》）

### 【强制】`import .` 仅允许一种例外：因循环依赖无法放在被测包内的测试文件

除该例外不要在程序中使用 `import .`：它使 `Quux` 这类名字看不出属于当前包还是导入包，严重损害可读性。（CRC《Import Dot》；EG《Package names》也要求不要使用 import .，仅运行于包外的测试可简化）

### 【参考】开发中用空标识符暂时屏蔽未使用的导入/变量

导入包或声明变量而未使用是编译错误；开发中途可用 `var _ = fmt.Printf` / `var _ io.Reader` / `_ = fd` 临时屏蔽。惯例：这类全局声明紧跟 import 之后并加注释（"For debugging; delete when done."），便于查找并提醒事后清理；空赋值标记的是半成品代码，最终必须使用或删除。（EG《Unused imports and variables》）

### 【强制】顶层变量声明用标准 var，能推断就不写类型

`var _s = F()`，不写 `var _s string = F()`——表达式已给出类型时不再重复；仅当表达式类型与期望类型不同才显式指定（如 `var _e error = F()`，F 返回 `myError` 而希望声明为 `error`）。（Uber《Top-level Variable Declarations》）

### 【强制】局部变量有明确初值时用 `:=`

`s := "foo"` 而非 `var s = "foo"`；但当 `var` 更能体现默认值语义时用 `var`（如声明空切片 `var filtered []int`，见第八章）。（Uber《Local Variable Declarations》）

### 【强制】Printf 用的格式串若在字面量之外声明，必须声明为 const

顶层格式串写成 `const msg = "unexpected values %v, %v\n"`，`go vet` 才能对格式串做静态分析。（Uber《Format Strings outside Printf》）

### 【强制】风格冲突时以一致性优先

本文件与其他章节的规约存在客观项也有情境项，最高原则是**保持一致**：一致的代码更易维护、更少认知负担；同一代码库内混用多种风格会降低速度、加剧评审摩擦并引入 bug。落地这套规约时改动以包（或更大）为单位执行，不要在包内引入第二种风格。（Uber《Be Consistent》）
