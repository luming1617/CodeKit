## 九、日志规范

### 【强制】应用中不可直接使用日志系统（Log4j、Logback）中的 API，必须依赖日志门面框架（SLF4J 优先 / JCL）

正例（SLF4J）：
```java
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
private static final Logger logger = LoggerFactory.getLogger(Test.class);
```

### 【强制】所有日志文件至少保存 15 天（有些异常以"周"为频次发生）；涉及网络运行状态、网络安全事件、个人敏感信息操作等相关记录留存不少于六个月，并做网络多机备份

### 【强制】扩展日志（打点、临时监控、访问日志等）命名方式：appName_logType_logName.log

正例：`mppserver_monitor_timeZoneConvert.log`。推荐对日志分类，错误日志与业务日志分开存放。

### 【强制】日志输出时字符串变量之间使用占位符，而不是拼接

正例：`logger.debug("Processing trade with id: {} and symbol: {}", id, symbol);`

### 【强制】trace/debug/info 级别日志输出必须进行日志级别的开关判断

正例：
```java
if (logger.isDebugEnabled()) {
    logger.debug("Current ID is: {} and name is: {}", id, getName());
}
```
说明：参数可能涉及字符串拼接或方法调用，无谓浪费开销。

### 【强制】避免重复打印日志，日志配置文件中必须设置 additivity=false

### 【强制】生产环境禁止直接使用 System.out / System.err 输出日志，或使用 e.printStackTrace() 打印异常堆栈

### 【强制】异常信息必须包括两类：案发现场信息和异常堆栈信息；如果不处理则通过 throws 往上抛出

正例：`logger.error("inputParams:{} and errorMessage:{}", 各类参数或对象 toString(), e.getMessage(), e);`

### 【强制】日志打印时禁止直接用 JSON 工具将对象转换成 String（对象内被覆写的 get 方法可能抛异常，影响正常业务流程），仅打印业务相关属性值或调用 toString()

### 【推荐】谨慎记录日志：生产环境禁止输出 debug 日志，有选择地输出 info 日志；用 warn 记录刚上线的业务行为时注意输出量，并及时删除观察日志

### 【推荐】用 warn 级别记录用户输入参数错误的情况，error 级别只记录系统逻辑出错、异常或重要错误信息，避免频繁报警

### 【推荐】尽量用英文描述日志错误信息，说不清楚时用中文；国际化团队或海外部署服务器因字符集问题使用全英文
