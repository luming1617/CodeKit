## 一、命名风格与常量规范

### 【强制】命名不得以下划线或美元符号开始/结束，禁用种族歧视性词语

反例：`_name` / `__name` / `$name` / `name_`。种族歧视词用 `blockList` / `allowList` / `secondary` 替代 `blackList` / `whiteList` / `slave`。

### 【强制】严禁拼音与英文混合命名，更不允许直接使用中文

正例：`ali` / `alibaba` / `taobao` 等国际通用名称可视同英文。
反例：`DaZhePromotion`（打折）/ `getPingfenByName()`（评分）。

### 【强制】大小写风格按元素类型统一

- 类名：UpperCamelCase，例外后缀 DO / BO / DTO / VO / AO / PO / UID。正例：`UserDO` / `HtmlDTO`；反例：`UserDo` / `HTMLDto`。
- 方法名、参数名、成员变量、局部变量：lowerCamelCase。正例：`localValue` / `getHttpMessage()`。
- 常量：全部大写，单词间下划线，语义完整不嫌长。正例：`MAX_STOCK_COUNT`；反例：`MAX_COUNT`。
- 包名：全小写，点分隔符间仅一个自然语义英语单词，用单数形式；类名有复数含义时可用复数。正例：包 `com.alibaba.ei.kunlun.aap.util`，类 `MessageUtils`。

### 【强制】抽象类以 Abstract 或 Base 开头；异常类以 Exception 结尾；测试类以被测类名开始、Test 结尾

### 【强制】POJO 类布尔类型变量不要加 is 前缀，否则框架反解析会引发序列化错误

说明：数据库字段用 `is_deleted`，POJO 属性用 `deleted`，在 `<resultMap>` 中设置映射。反例：`Boolean isDeleted` 的 getter 是 `isDeleted()`，框架误以为属性名是 `deleted`。

### 【强制】Service 和 DAO 类对外暴露接口，实现类用 Impl 后缀

正例：`CacheServiceImpl` 实现 `CacheService` 接口。形容能力的接口用 -able 形容词命名，正例：`AbstractTranslator` 实现 `Translatable`。

### 【强制】杜绝不规范缩写，避免子父类成员变量、不同代码块局部变量同名

反例：`AbstractClass` 缩写成 `AbsClass`；`condition` 缩写成 `condi`。

### 【参考】各层方法命名前缀约定

获取单个 `get`、多个 `list`（复数结尾，如 `listObjects`）、统计 `count`、插入 `save/insert`、删除 `remove/delete`、修改 `update`。领域模型命名：`xxxDO`（xxx 为表名）、`xxxDTO`、`xxxVO`，POJO 是统称，禁止命名成 `xxxPOJO`。

### 【强制】不允许任何魔法值（未经预先定义的常量）直接出现在代码中

反例：开发者 A 定义 key 为 `"Id#taobao_" + tradeId`，开发者 B 写成 `"Id#taobao" + tradeId`，导致线上故障。

### 【强制】long/Long 赋值数值后用大写 L，不能是小写 l

说明：`Long a = 2l;` 无法分辨是数字 21 还是 Long 型 2。

### 【推荐】按功能归类常量，善用 enum

大而全的常量类不利维护，如缓存常量放 `CacheConsts`。变量值仅在固定范围内变化时用 enum 定义；枚举类名带 Enum 后缀，成员全大写下划线分隔（`ProcessStatusEnum.SUCCESS`）。
