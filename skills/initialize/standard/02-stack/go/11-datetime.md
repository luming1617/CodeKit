## 十一、日期与时间

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。

### 【强制】处理时间一律使用 "time" 包

时间很复杂，常见的错误假设包括：一天有 24 小时、一小时有 60 分钟、一周有 7 天、一年有 365 天等等。例如"一天 24 小时"意味着：给一个时刻加 24 小时并不总能得到下一个日历日。因此处理时间时必须使用 [`"time"`](https://pkg.go.dev/time) 包，它以更安全、更准确的方式处理这些错误假设。（Uber《Use "time" to handle time》）

### 【强制】时刻用 time.Time，比较/加减用其方法

处理时刻（instant）使用 `time.Time`，比较、加、减时间使用 `time.Time` 上的方法，不要用裸整数：

```go
// Bad
func isActive(now, start, stop int) bool {
  return start <= now && now < stop
}

// Good
func isActive(now, start, stop time.Time) bool {
  return (start.Before(now) || start.Equal(now)) && now.Before(stop)
}
```

（Uber《Use time.Time for instants of time》）

### 【强制】时段用 time.Duration；加法按意图选 Add 还是 AddDate

处理时段（period）使用 `time.Duration`：

```go
// Bad：poll(10) —— 是秒还是毫秒？
func poll(delay int) {
  for {
    // ...
    time.Sleep(time.Duration(delay) * time.Millisecond)
  }
}

// Good
func poll(delay time.Duration) {
  for {
    // ...
    time.Sleep(delay)
  }
}

poll(10*time.Second)
```

给时刻加 24 小时时，用哪种加法取决于意图：想要"下一个日历日的同一时刻"用 `Time.AddDate`；想要"保证比原时刻晚 24 小时的瞬间"用 `Time.Add`：

```go
newDay := t.AddDate(0 /* years */, 0 /* months */, 1 /* days */)
maybeNewDay := t.Add(24 * time.Hour)
```

（Uber《Use time.Duration for periods of time》）

### 【强制】与外部系统交互也尽量用 time.Time / time.Duration

与外部系统交互时尽可能使用 `time.Duration` 与 `time.Time`（Uber《Use time.Time and time.Duration with external systems》）：

- 命令行 flag：`flag` 经 `time.ParseDuration` 支持 `time.Duration`
- JSON：`encoding/json` 经 `Time` 的 `UnmarshalJSON` 方法支持把 `time.Time` 编码为 [RFC 3339](https://tools.ietf.org/html/rfc3339) 字符串
- SQL：`database/sql` 在底层驱动支持时可在 `DATETIME`/`TIMESTAMP` 列与 `time.Time` 之间转换
- YAML：`gopkg.in/yaml.v2` 支持 `time.Time`（RFC 3339 字符串）与 `time.Duration`（`time.ParseDuration`）

无法使用 `time.Duration` 时，用 `int` 或 `float64` 并把单位写进字段名——例如 `encoding/json` 不支持 `time.Duration`，应写 `IntervalMillis int` 并配 `json:"intervalMillis"` 标签，而非无单位的 `Interval int`。无法使用 `time.Time` 时，除非另有约定，用 `string` 并按 RFC 3339 格式化时间戳——`Time.UnmarshalText` 默认使用该格式，`Time.Format` 与 `time.Parse` 可用常量 `time.RFC3339`。（Uber）

### 【参考】"time" 包不支持闰秒

实践中通常不是问题，但需记住：`"time"` 包不支持解析带闰秒的时间戳（golang/go issue 8728），计算中也不考虑闰秒（issue 15190）——比较两个时刻时，差值不会包含两者之间可能发生的闰秒。（Uber）
