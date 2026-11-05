## 三、OOP 与日期时间规范

### 【强制】所有的覆写方法必须加 @Override 注解；相同参数类型、相同业务含义才可用可变参数，避免使用 Object

可变参数必须放在参数列表最后。正例：`public List<User> listUsers(String type, Long... ids)`。

### 【强制】外部正在调用或二方库依赖的接口，不允许修改方法签名；过时接口必须加 @Deprecated 并说明新接口

### 【强制】equals 用常量或确定有值的对象调用；整型包装类之间值比较全部用 equals

正例：`"test".equals(object);`，推荐 JDK7 `Objects#equals`。说明：Integer 在 -128~127 区间复用缓存对象，区间外均在堆上产生，`==` 比较是大坑。

### 【强制】任何货币金额，均以最小货币单位且整型类型存储

### 【强制】浮点数等值判断：基本类型不能用 ==，包装类型不能用 equals

正例：① 指定误差范围 `if (Math.abs(a - b) < 1e-6F)`；② 用 BigDecimal 运算后 `x.compareTo(y) == 0` 判断。

### 【强制】BigDecimal 等值比较用 compareTo() 而非 equals()；禁止用构造方法 BigDecimal(double)

说明：equals 比较值和精度（1.0 与 1.00 为 false）；`new BigDecimal(0.1F)` 实际存储 0.10000000149。正例：`new BigDecimal("0.1")` 或 `BigDecimal.valueOf(0.1)`。

### 【强制】数据类型使用标准：POJO 类属性必须用包装类型；RPC 方法返回值和参数必须用包装类型；局部变量推荐用基本类型

说明：包装类型的 null 能表达额外信息（远程调用失败等），基本类型默认值会掩盖问题。DO 类属性类型必须与数据库字段匹配（bigint 对应 Long，否则 id 溢出成负数）。

### 【强制】定义 DO/DTO/VO 等 POJO 类时不要设定任何属性默认值；序列化类新增属性时不要修改 serialVersionUID；构造方法内禁止业务逻辑（初始化放 init 方法）；POJO 必须写 toString；禁止同一属性同时存在 isXxx() 和 getXxx()

### 【推荐】循环体内字符串连接使用 StringBuilder 的 append 方法；getter/setter 不要增加业务逻辑；类内方法定义顺序：公有/保护方法 > 私有方法 > getter/setter

### 【推荐】慎用 Object 的 clone（默认浅拷贝）；类成员与方法访问控制从严（不允许外部 new 则构造方法 private，工具类不允许 public/default 构造方法，仅本类使用的成员必须 private）

### 【强制】日期格式化 pattern 中年份统一用小写 y，分清 M（月）/m（分）、H（24 时制）/h（12 时制）

反例：`YYYY` 是 week in which year，本周跨年时返回下一年。正例：`new SimpleDateFormat("yyyy-MM-dd HH:mm:ss")`。

### 【强制】获取当前毫秒数用 System.currentTimeMillis()，而不是 new Date().getTime()；不允许使用 java.sql.Date / java.sql.Time / java.sql.Timestamp

JDK8 统计时间场景推荐 Instant；推荐用 Instant 代替 Date、LocalDateTime 代替 Calendar、DateTimeFormatter 代替 SimpleDateFormat（线程安全）。

### 【强制】不要写死一年为 365 天；避免公历闰年 2 月问题；用枚举值指代月份

正例：`LocalDate.now().lengthOfYear()` 获取当年天数。说明：Date/Calendar 的 month 取值 0-11（0 表示 January）。
