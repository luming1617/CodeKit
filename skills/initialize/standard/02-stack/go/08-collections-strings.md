## 八、集合与字符串

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】日常序列数据用 slice，不用数组

Go 数组与 C 差异很大：数组是**值**，赋值会拷贝全部元素；传给函数时函数收到的是副本而非指针；数组大小是类型的一部分（`[10]int` 与 `[20]int` 是不同类型）。想要 C 式行为和效率可以传数组指针（`func Sum(a *[3]float64)`），但即便这种风格也不够 Go 惯用——**用 slice**。（EG《Arrays》）

slice 包装数组，提供更通用、强大、便捷的序列接口；除变换矩阵这类有显式维度的数据外，Go 中大多数数组编程都用 slice 完成。注意 slice 持有底层数组的引用：一个 slice 赋值给另一个后二者指向同一数组；函数接收 slice 参数时对元素的修改对调用方可见（相当于传了底层数组指针）。因此 `Read` 类 API 直接收 slice 而非"指针+计数"，slice 的长度就是读取上限：

```go
func (f *File) Read(buf []byte) (n int, err error)

n, err := f.Read(buf[0:32])   // 读取大缓冲的前 32 字节
```

slice 的长度在不超过底层数组时可以通过对自身切片重新赋值改变；内建函数 `cap` 报告可获得的最大长度。由于 slice 结构（指针、长度、容量）按值传递，追加/重切后**必须返回结果 slice**——手写 `Append` 如此，内建 `append` 亦然。（EG《Slices》）

### 【强制】append 的签名与用法

内建 `append` 的模式化签名为：

```go
func append(slice []T, elements ...T) []T
```

它把元素追加到 slice 尾部并返回结果——结果必须被接收（`x = append(x, 4, 5, 6)`），因为底层数组可能因扩容而改变。slice 接 slice 在调用点用 `...`：`x = append(x, y...)`；不写 `...` 会因类型不符（`y` 不是 `int`）而编译失败。（EG《Append》）

### 【强制】声明空 slice 用 `var t []string`，返回空也用 nil

声明空 slice 时优先：

```go
var t []string        // Good: nil slice
```

而非 `t := []string{}`（非 nil 但零长度）。两者功能等价（`len` 与 `cap` 均为 0），但 nil slice 是首选风格。`nil` 是长度为 0 的合法 slice：不要显式返回零长度 slice 而应返回 `nil`；判断 slice 为空永远用 `len(s) == 0`，不要与 `nil` 比较；用 `var` 声明的零值 slice 无需 `make()` 即可直接使用。（CRC《Declaring Empty Slices》；Uber《nil is a valid slice》）

两类例外要注意：其一，非 nil 零长度 slice 更合适的有限场景，如编码 JSON 对象时（`nil` slice 编码为 `null`，`[]string{}` 编码为 `[]`）；设计接口时避免在 nil slice 与非 nil 零长度 slice 之间做区分，否则会引出微妙错误（CRC）；同理，nil slice 与分配出的零长度 slice 并不等价，一个为 nil 一个不是，在序列化等不同场景可能被区别对待（Uber）。其二，需要预分配时用 `make([]T, 0, size)` 并 `append`，见第九章容量准则。（Uber《nil is a valid slice》）

### 【强制】在边界处拷贝 slice 和 map

slice 和 map 持有底层数据的指针，需要拷贝时警惕别名场景（Uber《Copy Slices and Maps at Boundaries》）：

- **接收**：作为参数收到的 map/slice 若被存了引用，调用方仍可修改它。需要保存时应拷贝：

```go
// Bad：调用方改 trips[0] 会意外改掉 d1.trips
func (d *Driver) SetTrips(trips []Trip) {
  d.trips = trips
}

// Good
func (d *Driver) SetTrips(trips []Trip) {
  d.trips = make([]Trip, len(trips))
  copy(d.trips, trips)
}
```

- **返回**：暴露内部状态的 map/slice 要警惕调用方修改。持锁返回内部 map 的 `Snapshot()`，锁外任何访问都有数据竞争；应构造副本 map 返回。

### 【强制】map 初始化：空/程序填充用 make，固定列表用字面量

（Uber《Initializing Maps》）空 map 与以编程方式填充的 map 优先用 `make(...)`，让"声明"与"初始化"在视觉上可区分（`m1 = map[T1]T2{}` 与 `m2 map[T1]T2` 长得太像，而 `m2` 写入会 panic），也便于日后加容量提示：

```go
var (
  m1 = make(map[T1]T2)   // 可安全读写
  m2 map[T1]T2           // 写入会 panic
)
```

尽量为 `make()` 提供容量提示（容量语义与示例见第九章《Prefer Specifying Container Capacity》）。持有固定元素列表时用 map 字面量初始化：

```go
m := map[T1]T2{
  k1: v1,
  k2: v2,
  k3: v3,
}
```

经验法则：初始化时加入固定集合用字面量，否则用 `make`（有大小线索就加上）。

### 【参考】map 的语言事实

map 将一种类型的键关联到另一种类型的值。键可以是任何定义了相等运算符的类型：整数、浮点数、复数、字符串、指针、接口（动态类型支持相等即可）、struct 与数组；**slice 不能作键**（未定义相等）。与 slice 一样，map 持有底层数据结构的引用——传给函数后函数对内容的修改在调用方可见。map 可用冒号分隔键值对的复合字面量在初始化时构造：

```go
var timeZone = map[string]int{
    "UTC":  0*60*60,
    "EST": -5*60*60,
}
```

取不存在键的值返回该 map 元素类型的零值（整数 map 返回 0）；可用值类型为 bool 的 map 实现集合。需要区分"键不存在"与"零值"时用 "comma ok" 惯用法：`seconds, ok = timeZone[tz]`（tz 存在则 seconds 正确赋值且 ok 为 true，否则 seconds 为零值且 ok 为 false）；只测存在性不关心值时用空白标识符：`_, present := timeZone[tz]`。删除条目用内建 `delete(timeZone, "PDT")`，键已不存在时也是安全的。（EG《Maps》）

### 【参考】二维数据是"数组的数组/slice 的 slice"

Go 的数组和 slice 都是一维的；二维要定义成 `[3][3]float64` 或 `[][]byte` 这样的嵌套形式。由于 slice 变长，内层 slice 可以长度不同（如每行独立的 `LinesOfText`）。需要分配二维 slice 时有两种方式：逐行独立分配，或分配单个大数组再把各行切片进去——若各行可能增缩必须独立分配以免覆盖下一行，否则单次分配再切分更高效（如像素扫描线场景）。（EG《Two-dimensional slices》）

### 【强制】用原始字符串字面量避免转义

Go 支持可跨行、可含引号的 raw string literal（反引号），用它代替手工转义的字符串——后者难读得多：

```go
wantError := "unknown name:\"test\""   // Bad
wantError := `unknown error:"test"`    // Good
```

（Uber《Use Raw String Literals to Avoid Escaping》）
