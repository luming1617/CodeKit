## 十三、安全

> 来源：Uber Go Style Guide 中文版（缩写 `Uber`）、Effective Go（缩写 `EG`）、Go Code Review Comments（缩写 `CRC`）。
>
> 说明：本模块只收录三份来源中明确给出的安全相关条款；未收录不代表无风险。

### 【强制】生成密钥等安全随机数必须用 crypto/rand

不要用 [`math/rand`](https://pkg.go.dev/math/rand) 或 [`math/rand/v2`](https://pkg.go.dev/math/rand/v2) 生成密钥——包括"用完即弃"的临时密钥也不要用。以 `Time.Nanoseconds()` 作种子时，熵只有寥寥几位。应当使用 [`crypto/rand.Reader`](https://pkg.go.dev/crypto/rand#pkg-variables)；需要文本时用 [`crypto/rand.Text`](https://pkg.go.dev/crypto/rand#Text)，或者用 `encoding/hex` / `encoding/base64` 对随机字节做编码：

```go
import (
    "crypto/rand"
    "fmt"
)

func Key() string {
  return rand.Text()
}
```

（CRC《Crypto Rand》）

### 【参考】其他与安全性相关的既有条款

三份来源中与安全性相关、已在其他章节按原出处收录的条款：错误不得丢弃与只处理一次（第六章）、生产代码不 panic（第六章）、goroutine 生命周期管理（第五章）、序列化字段 tag（第七章）、结构体拷贝别名（第七章）。此处只做索引，不重复展开，也不额外附加来源未声明的安全定性。
