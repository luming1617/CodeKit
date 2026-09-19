## 一、命名规范

> 来源：Uber Go Style Guide 中文版（github.com/xxjwxc/uber_go_guide_cn，正文缩写 `Uber`）、Effective Go（go.dev/doc/effective_go，缩写 `EG`）、Go Code Review Comments（go.dev/wiki/CodeReviewComments，缩写 `CRC`）。

### 【强制】包名全小写、短小、单个语义词，不用下划线和 mixedCaps

包名只应是源码目录的基名：`src/encoding/base64` 导入为 `"encoding/base64"`，但包名是 `base64`，不是 `encoding_base64` 也不是 `encodingBase64`。

- 要求全小写，无大写字母、无下划线（Uber《Package Names》）。
- 要求简短精练——调用方每个使用点都会输入它；不嫌短，因为所有人都会敲这个名字（EG《Package names》）。
- 不用复数：`net/url` 而非 `net/urls`（Uber）。
- 不用 `common`、`util`、`shared`、`lib` 这类无信息量名字（Uber）；同样避免 `misc`、`api`、`types`、`interfaces`（CRC《Package Names》）。
- 不必事先担心包名冲突：包名只是导入的默认名，无需全局唯一，罕见冲突时导入方可本地改名。（EG）

### 【强制】导出名避免与包名重复

导入方总是以 `包名.名字` 引用，所以导出名里不要再带包名前缀：

- `bufio` 包的缓冲读取类型叫 `Reader`（客户端看到 `bufio.Reader`），不叫 `BufReader`；因为导入实体总带包名，`bufio.Reader` 不会与 `io.Reader` 冲突。
- `ring` 包唯一导出的类型是 `Ring`，其构造函数就叫 `New`（客户端看到 `ring.New`），不叫 `NewRing`。
- 在 `chubby` 包里定义 `File`（客户端写 `chubby.File`），不要定义 `ChubbyFile`（客户端将写 `chubby.ChubbyFile`）。

（EG《Package names》；CRC《Package Names》）

### 【强制】多词标识符用 MixedCaps 或 mixedCaps，不用下划线

这是 Go 的既定惯例，即使与其他语言惯例冲突也照样适用：未导出常量应写作 `maxLength`，而不是 `MaxLength` 或 `MAX_LENGTH`。（EG《MixedCaps》；CRC《Mixed Caps》）

### 【强制】首字母缩略词与专有词在名字中保持大小写一致

"URL"、"NATO" 这类词要么全大写要么全小写，不允许混合大小写：`urlPony` / `URLPony`，绝不写 `Url`；`ServeHTTP` 而非 `ServeHttp`；多缩略词如 `xmlHTTPRequest` / `XMLHTTPRequest`。"ID" 作 "identifier" 缩写时同样适用：写 `appID` 不写 `appId`。协议缓冲区编译器生成的代码豁免此规则。（CRC《Initialisms》）

### 【强制】getter 不加 Get 前缀

Go 不提供自动 getter/setter，手写时 getter 与字段同名、仅首字母大小写不同：字段 `owner`（未导出），getter 就叫 `Owner`（导出），setter 若需要叫 `SetOwner`。导出大小写机制正好用于区分字段与方法。

```go
owner := obj.Owner()
if owner != user {
    obj.SetOwner(user)
}
```

（EG《Getters》）

### 【强制】单方法接口按方法名加 -er 后缀命名；规范名不得滥用

`Reader`、`Writer`、`Formatter`、`CloseNotifier` 等是惯例。`Read`、`Write`、`Close`、`Flush`、`String` 等名字有规范签名与含义：除非你的方法签名和含义与之完全一致，否则不要占用这些名字；反之，若你的方法语义与知名类型一致，就用同样的名字与签名——字符串转换方法应叫 `String` 而不是 `ToString`。（EG《Interface names》）

### 【强制】函数名用 MixedCaps；测试函数允许下划线分组

遵循 Go 社区的函数命名 MixedCaps 惯例。唯一例外是测试函数，可含下划线对相关用例分组，如 `TestMyFunction_WhatIsBeingTested`。（Uber《Function Names》）

### 【强制】变量名宜短，作用域越大名字越具体

Go 的变量名普遍比其他语言短，局部变量尤甚：宁用 `c` 不用 `lineCount`，宁用 `i` 不用 `sliceIndex`。基本规则：名字被使用的地点距离声明越远，就要越具描述性；方法接收者一两个字母足够；循环下标、reader 等常见量可单字母（`i`、`r`）；不常见的东西和全局变量需要更具描述性的名字。（CRC《Variable Names》）

### 【强制】未导出的顶层 var 与 const 以 `_` 开头

顶层变量和常量是包级作用域，通用名很容易在其他文件中被误用；加 `_` 前缀让使用者一眼看出它是全局符号（`_defaultPort`、`_defaultUser`）。例外：未导出的错误值允许用 `err` 前缀而不带下划线，见第六章《错误命名》。（Uber《Prefix Unexported Globals with _》）

### 【强制】不要复用语言预声明标识符作名字

Go 规范列出的预声明标识符（`error`、`string` 等）不应作程序内名字：按上下文遮蔽原标识符或使代码费解，最好情况是编译器抱怨，最坏情况引入潜伏的、grep 不到的 bug。反例：`var error string`、`func handleErrorMessage(error string)`、结构体字段 `error error` / `string string`（导致 `f.error` 与 `Error()` 视觉混淆）；正例是 `errorMessage`、`msg`、字段 `err`/`str`。编译器不会为使用预声明标识符报错，但 `go vet` 等工具能指出这类遮蔽。（Uber《Avoid Using Built-In Names》）

### 【推荐】名字长短以可读性为准，不要靠长名字堆信息

`once.Do(setup)` 读起来很好，改成 `once.DoOrWaitUntilDone(setup)` 不会更佳。长名字不会自动让代码更可读，一条有用的文档注释往往胜过额外长的名字。（EG《Package names》）
