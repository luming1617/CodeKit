## 八、异常处理与错误码规范

### 【强制】可通过预检查规避的 RuntimeException 不应该用 catch 处理

如 NullPointerException、IndexOutOfBoundsException 应预检查：正例 `if (obj != null) {...}`；反例 `try { obj.method(); } catch (NullPointerException e)`。无法预检查的除外（如数字格式解析的 NumberFormatException）。

### 【强制】异常捕获后不要用来做流程控制、条件控制；catch 时分清稳定代码与非稳定代码，尽可能区分异常类型再做对应处理

对大段代码 try-catch 使程序无法对不同异常做出正确应激反应，也不利于定位问题。

### 【强制】捕获异常是为了处理它；不想处理就抛给调用者，最外层的业务使用者必须处理异常，将其转化为用户可以理解的内容

### 【强制】事务场景中，抛出异常被 catch 后如果需要回滚，一定要注意手动回滚事务

### 【强制】finally 块必须对资源对象、流对象进行关闭，有异常也要 try-catch；JDK7 及以上使用 try-with-resources；禁止在 finally 块中使用 return（会丢弃 try 块的返回点）

### 【强制】捕获异常与抛异常必须完全匹配，或捕获异常是抛异常的父类

### 【强制】调用 RPC、二方包、或动态生成类的相关方法时，捕捉异常必须使用 Throwable 类拦截

说明：类冲突仲裁或字节码修改框架（ASM）可能导致运行期 NoSuchMethodError，编译期正确不代表运行期安全。

### 【推荐】防止 NPE 是程序员的基本修养，注意场景：返回基本类型时 return 包装对象自动拆箱、数据库查询结果可能为 null、集合元素 isNotEmpty 但元素可能为 null、远程调用返回对象一律判空、Session 取数、级联调用 obj.getA().getB().getC()。正例：JDK8 用 Optional 防 NPE

### 【推荐】区分 unchecked/checked 异常，避免直接 throw new RuntimeException()，更不允许抛出 Exception 或 Throwable，应使用有业务含义的自定义异常（如 DAOException / ServiceException）

### 【参考】异常返回策略分场景：公司外 http/api 开放接口必须用 errorCode；应用内部推荐直接异常抛出；跨应用 RPC 调用优先用 Result 封装 isSuccess()、errorCode、errorMessage

### 【强制】错误码为字符串类型共 5 位：错误产生来源 + 四位数字编号。A 表示错误来源于用户（参数错误、支付超时等），B 表示当前系统错误（业务逻辑出错、健壮性差），C 表示第三方服务错误（CDN 出错、消息投递超时）；编号从 0001 到 9999，大类间步长预留 100

一级宏观错误码：A0001（用户端错误）、B0001（系统执行出错）、C0001（调用第三方服务出错）；全部正常时返回 00000。

### 【强制】错误码不体现版本号和错误等级信息；不能直接输出给用户作为提示信息；堆栈、error_message、error_code、user_tip 是互相转义的整体，请勿越俎代庖

### 【强制】错误码使用者避免随意定义新的错误码，尽可能在原有附表中找语义相同或相近的使用；编号不与公司业务/组织架构挂钩，统一平台先到先得、审批后永久固定

### 【推荐】获取第三方服务错误码向上抛出时允许本系统转义（C 转 B），并在错误信息上带上原有的第三方错误码；业务独特信息由 error_message 承载，不让错误码涵盖过多业务属性
