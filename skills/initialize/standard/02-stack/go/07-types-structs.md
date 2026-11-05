## 七、类型、结构体与接口

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】分清 new 与 make：new 只清零、make 才初始化

Go 有两个分配内建原语，用途不同：`new(T)` 为类型 `T` 的新变量分配**清零**的存储并返回其地址 `*T`（它不初始化内存，只置零；自 Go 1.26 起 `new` 也可接收值表达式作为初始值，如 `new(int64(300))`）。由于 `new` 返回的是零值，设计数据结构时应让每个类型的零值无需进一步初始化即可使用：`bytes.Buffer` 的文档声明"Buffer 的零值是可直接使用的空缓冲"；`sync.Mutex` 没有显式构造函数或 Init 方法，其零值就是未加锁的互斥锁；该性质可传递——含 `sync.Mutex` 与 `bytes.Buffer` 字段的 `SyncedBuffer`，无论 `p := new(SyncedBuffer)` 还是 `var v SyncedBuffer` 都立即可用。（EG《Allocation with new》）

`make(T, args)` 与 `new(T)` 目的不同：它**只用于 slice、map、通道**，返回的是已初始化（非清零）的值 `T`（不是 `*T`）。因为这三者底层是对数据结构的引用，使用前必须初始化内部结构（slice 是含指针、长度、容量的三项描述符，未初始化前为 nil）。`new([]int)` 返回指向零值 slice 结构（即 nil slice）的指针，几乎无用；`v := make([]int, 100)` 才是惯用法。记住：`make` 只适用于 map、slice、通道且返回非指针；需要显式指针时用 `new` 或对变量取地址。（EG《Allocation with make》）

### 【强制】构造函数用复合字面量；返回局部变量地址是合法的

零值不够用时需要初始化构造函数。逐字段赋值的 `f := new(File); f.fd = fd; ...` 样板代码多，应使用复合字面量（每次求值创建新实例）：

```go
return &File{fd: fd, name: name}
```

与 C 不同，返回局部变量的地址完全没问题——变量的存储在其函数返回后依然存活；对复合字面量取地址每次求值都会分配新实例。不带标签的复合字面量按顺序且必须包含全部字段；显式标注 `field: value` 后可任意顺序、缺省字段留零值。字面量为空时创建该类型的零值，`new(File)` 与 `&File{}` 等价。数组、slice、map 也可用复合字面量创建，"字段标签"分别是索引或键：

```go
a := [...]string   {Enone: "no error", Eio: "Eio", Einval: "invalid argument"}
s := []string      {Enone: "no error", Eio: "Eio", Einval: "invalid argument"}
m := map[int]string{Enone: "no error", Eio: "Eio", Einval: "invalid argument"}
```

（EG《Constructors and composite literals》）

### 【强制】初始化结构体几乎总要写字段名

这已被 `go vet` 强制：`k := User{"John", "Doe", true}` 为坏例，应写成 `k := User{FirstName: "John", LastName: "Doe", Admin: true}`。例外：表驱动测试中字段数不超过 3 时可省略字段名。（Uber《Use Field Names to Initialize Structs》）

### 【强制】省略零值字段；全零值时用 var 声明

用字段名初始化结构体时，省略具有零值的字段（除非它们提供有意义的上下文），让 Go 自动置零：`user := User{FirstName: "John", LastName: "Doe"}` 而不是显式写 `MiddleName: "", Admin: false`——默认上下文中省略默认值能减少读者的噪音。测试用例等场景若字段名本身提供有意义上下文，则可以保留零值命名（`{give: "0", want: 0}`）。（Uber《Omit Zero Value Fields in Structs》）

当声明中省略了结构体的全部字段时，用 `var` 形式：`var user User` 而不是 `user := User{}`——这与 map 初始化和空 slice 的区分方式一致，能把零值结构与有非零字段的结构区分开。（Uber《Use var for Zero Value Structs》）

### 【强制】结构体指针用 `&T{}`，不用 `new(T)`

初始化结构体引用时使用 `&T{}` 而非 `new(T)`，与结构体初始化风格保持一致：

```go
sval := T{Name: "foo"}

// Bad: inconsistent
sptr := new(T)
sptr.Name = "bar"

// Good
sptr := &T{Name: "bar"}
```

（Uber《Initializing Struct References》）

### 【强制】枚举通常从 1 开始

Go 引入枚举的标准方式是声明自定义类型加带 `iota` 的 `const` 组。由于变量默认值为 0，枚举通常应从非零值开始（`Add Operation = iota + 1` → Add=1, Subtract=2, Multiply=3）。零值合理的场景是零值本身即期望的默认行为：

```go
type LogOutput int

const (
  LogToStdout LogOutput = iota  // 作为默认输出目标是期望行为
  LogToFile
  LogToRemote
)
```

（Uber《Start Enums at One》）

### 【参考】常量是编译期的；iota 可构造复杂值集与自动格式化标量类型

常量在编译期创建（即使在函数内定义为局部量），只能是数字、字符（rune）、字符串或布尔值；定义它们的表达式必须是编译器可求值的常量表达式（`1<<3` 可以，`math.Sin(math.Pi/4)` 不行）。Go 的枚举常量用 `iota`  enumerator 构造；`iota` 可参与表达式、且表达式可被隐式重复，因此容易构建精巧的值集：

```go
type ByteSize float64

const (
    _           = iota // ignore first value by assigning to blank identifier
    KB ByteSize = 1 << (10 * iota)
    MB
    GB
    TB
    PB
    EB
    ZB
    YB
)
```

为任意用户定义类型挂上 `String` 方法的能力，让任意值能自动格式化输出——该技巧不只适用于 struct，也适用于 `ByteSize` 这样的浮点标量类型（`YB` 打印为 `1.00YB`，`ByteSize(1e13)` 打印为 `9.09TB`）。这里用 `Sprintf` 实现 `String` 之所以安全（不会无限递归），不是因为类型转换，而是因为它以 `%f` 调用：`Sprintf` 只在需要字符串时才调用 `String` 方法，而 `%f` 要的是浮点值。（EG《Constants》）

### 【强制】会被序列化的结构体字段必须加 tag

任何会被 marshal 成 JSON、YAML 或支持基于标签字段命名格式的结构体字段，都应标注相应 tag：

```go
type Stock struct {
  Price int    `json:"price"`
  Name  string `json:"name"`
  // Safe to rename Name to Symbol.
}
```

序列化后的结构是系统间的契约，改动序列化形式（包括字段名）会破坏契约。在 tag 中显式指定字段名使契约明确，防止重构或重命名字段时意外破坏。（Uber《Use field tags in marshaled structs》）

### 【强制】几乎不需要指向接口的指针

接口值由两个字段构成：指向类型信息的指针与数据指针（存储的是指针则直接存，存储的是值则存其地址）。因此接口应作为**值**传递——底层数据仍可是指针。若需要接口方法修改底层数据，必须使用指针。（Uber《Pointers to Interfaces》）

### 【推荐】在编译期验证接口实现

下列场景应显式做编译期接口合规检查：作为 API 契约一部分必须实现特定接口的导出类型；实现同一接口的类型集合中的导出或未导出类型；其他违反接口会破坏使用者的情况：

```go
var _ http.Handler = (*Handler)(nil)
```

若 `*Handler` 不再匹配 `http.Handler`，该行将编译失败。赋值右侧应是被断言类型的零值：指针类型、slice、map 用 `nil`（如 `(*Handler)(nil)`），结构体类型用空结构体（如 `var _ http.Handler = LogHandler{}`）。（Uber《Verify Interface Compliance》）

### 【强制】接口定义在使用方；实现方返回具体类型

Go 的接口通常属于**使用**该接口值的包，而不是实现它的包。实现包应返回具体类型（通常为指针或结构体），这样就能为实现添加新方法而不要求大规模重构。不要在 API 实现方"为了 mock"而定义接口——应设计成可通过真实实现的公共 API 来测试的 API。也不要在接口被使用之前就先定义：没有实际使用示例时，很难判断接口是否必要、该包含哪些方法。反例（producer 侧定义 `Thinger` 接口并让 `NewThinger() Thinger`）应改为返回具体类型 `Thinger` struct，由消费方去 mock 生产方的实现。（CRC《Interfaces》）

### 【参考】纯接口实现类型可只导出接口

若一个类型存在的唯一目的就是实现某接口、且不会有超出该接口的导出方法，则不必导出该类型本身：只导出接口能明确"该值除接口所述之外没有其他有趣行为"，也避免每个公共方法的文档重复。此时构造函数应返回接口值而非实现类型——如哈希库的 `crc32.NewIEEE` 与 `adler32.New` 都返回 `hash.Hash32`，替换算法只需改构造调用；`crypto/cipher` 的 `NewCTR(block Block, iv []byte) Stream` 对 `Block` 与 `Stream` 接口的任意实现通用，把 CTR 换成其他分组模式是局部改动。（EG《Generality》）该惯用法面向"多种可替换实现共享接口"的库设计，与上一节面向一般实现包返回具体类型的建议各有适用场景，同一代码库内应保持一致。（CRC《Interfaces》；Uber《Be Consistent》）

### 【强制】结构体嵌入：位置、空行与"有切实收益才嵌入"

嵌入的类型应放在字段列表顶部，嵌入字段与普通字段之间必须有一个空行（`Client` 中 `http.Client` 在 `version int` 之前并空行分隔）。嵌入应提供切实收益——以语义合适的方式添加或增强功能，且不产生对用户不利的副作用。嵌入**不应**：纯粹为了外观或省事；让外层类型更难构造或使用；影响外层类型的零值可用性（外层类型若有用零值，嵌入后仍应有用）；以内嵌副作用暴露无关的函数或字段；暴露未导出类型；影响外层类型的拷贝语义；改变外层类型的 API 或类型语义；嵌入内层类型的非规范形式；暴露外层类型的实现细节；允许使用者观察或控制类型内部；通过包装使内层函数一般行为发生令用户意外的变化。（Uber《Embedding in Structs》）

判据一句话："内层的全部导出方法/字段是否都该直接加到外层类型上"——答案是"部分"或"否"就不要嵌入，改用字段。互斥锁例外于一切嵌入规则：不得嵌入 mutex，即使是未导出类型（见第五章）。

### 【强制】公共结构体中不要嵌入类型

在公共结构体中嵌入类型会泄漏实现细节、阻碍类型演化、模糊文档。若多种列表类型共享 `AbstractList` 实现，具体列表实现不要嵌入 `AbstractList`，应手写委托方法：

```go
// Bad
type ConcreteList struct {
  *AbstractList
}

// Good
type ConcreteList struct {
  list *AbstractList
}

// Add adds an entity to the list.
func (l *ConcreteList) Add(e Entity) {
  l.list.Add(e)
}
```

嵌入是继承与组合之间的妥协：外层类型获得内层实例方法的隐式副本（默认委托给内层同名方法），同时获得一个与类型同名的字段——若嵌入类型是公开的，字段也是公开的。为保持向后兼容，外层类型未来的每个版本都必须保留被嵌入类型。无论嵌入 struct 还是接口，都限制演化：给嵌入接口加方法是破坏性变更；从嵌入 struct 删方法是破坏性变更；移除嵌入类型、或换成满足同一接口的替代类型都是破坏性变更。嵌入类型很少是必需的，它只是省去写委托样板的便利；多写委托的代价换来的是隐藏实现细节、保留变化空间、且文档中不必间接跳转才能看到完整接口。（Uber《Avoid Embedding Types in Public Structs》）

### 【参考】嵌入不是子类化：方法提升、接收者归属与名字冲突

Go 不提供类型驱动的子类化，但可通过在 struct 或接口中嵌入类型来"借用"实现的片段。接口中只能嵌入接口（`io.ReadWriter` 即 `Reader` 与 `Writer` 的并集）。struct 嵌入免去手写转发方法：`bufio.ReadWriter` 嵌入 `*Reader` 与 `*Writer` 后同时满足 `io.Reader`、`io.Writer`、`io.ReadWriter`。与子类化的重要区别：嵌入类型的方法成为外层类型的方法，但**调用时方法的接收者是内层类型而非外层**。嵌入也可作纯便利：`type Job struct { Command string; *log.Logger }` 让 Job 免费获得 `Print/Printf/Println`；需要直接引用嵌入字段时用其类型名（去包限定）作字段名（`job.Logger`），可据此精化方法。名字冲突规则：浅层的 X 隐藏更深嵌套的 X（`Job.Command` 支配 `log.Logger` 内的同名项）；同名出现在同一嵌套层级通常是错误，但若该重名在类型定义之外的代码中从未被提及则允许——这对外部嵌入类型的后续变更提供了一定保护。（EG《Embedding》）

### 【强制】跨包拷贝结构体要警惕别名效应

为避免意外别名，拷贝其他包中类型 `T` 的值时要小心：例如 `bytes.Buffer` 内含 `[]byte` slice，拷贝 Buffer 后副本内的 slice 可能与原始数组别名，导致后续方法调用出现意外效果。一般规则：若某类型的方法都挂在指针类型 `*T` 上，就不要拷贝 `T` 类型的值。（CRC《Copying》）
