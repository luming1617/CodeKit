## 五、并发处理规范

### 【强制】线程资源必须通过线程池提供，不允许在应用中自行显式创建线程

### 【强制】线程池不允许使用 Executors 创建，必须通过 ThreadPoolExecutor 构造

说明：Executors 的隐患——FixedThreadPool 和 SingleThreadPool 允许请求队列长度 `Integer.MAX_VALUE`，可能堆积大量请求导致 OOM；CachedThreadPool 允许创建线程数 `Integer.MAX_VALUE`，可能创建大量线程导致 OOM。

### 【强制】创建线程或线程池时指定有意义的线程名称，方便出错时回溯（jstack 排查时非常有帮助）

正例：自定义 `ThreadFactory`，用 `namePrefix + nextId.getAndIncrement()` 按外部特征分组命名。

### 【强制】获取单例对象需要保证线程安全，其中的方法也要保证线程安全；SimpleDateFormat 线程不安全，不要定义为 static，否则必须加锁或使用 DateUtils / ThreadLocal 包装

JDK8 应用推荐 Instant / LocalDateTime / DateTimeFormatter（immutable、thread-safe）替代。

### 【强制】必须回收自定义的 ThreadLocal 变量，尤其在线程池场景下线程经常复用，不清理会影响后续业务并造成内存泄露；尽量在代理中使用 try-finally 块调用 remove()

ThreadLocal 对象建议用 static 修饰；它无法解决共享对象的更新问题。

### 【强制】锁的粒度最小化：能用无锁数据结构就不用锁；能锁区块就不锁整个方法体；能用对象锁就不用类锁；避免在锁代码块中调用 RPC 方法

### 【强制】对多个资源、数据库表、对象同时加锁时，必须保持一致的加锁顺序，否则可能死锁

### 【强制】阻塞等待获取锁时，lock() 必须在 try 代码块之外，且 lock 与 try 之间不得有可抛异常的方法调用，保证 finally 中一定能解锁

反例：把 `lock.lock()` 放在 try 内——其他方法先抛异常时，finally 会对未加锁对象 unlock，抛 `IllegalMonitorStateException`。

### 【强制】用 tryLock 尝试机制获取锁时，进入业务代码块前必须判断当前线程是否持有锁，释放规则与阻塞方式相同

### 【强制】并发修改同一记录时避免更新丢失，需要加锁：应用层加锁、缓存加锁、或数据库层乐观锁（version 字段）。冲突概率 < 20% 推荐乐观锁，否则悲观锁；乐观锁重试次数不得小于 3 次

### 【强制】多线程并行定时任务用 ScheduledExecutorService，不要用 Timer——Timer 多个 TimeTask 中一个未捕获异常会终止其它所有任务

### 【推荐】资金相关的金融敏感信息使用悲观锁策略，遵循一锁、二判、三更新、四释放

### 【推荐】CountDownLatch 异步转同步时，每个线程退出前必须调用 countDown，线程执行代码注意 catch 异常，确保 countDown 被执行到（子线程异常堆栈无法在主线程 try-catch）

### 【推荐】避免 Random 实例被多线程共享（竞争同一 seed 导致性能下降），JDK7+ 直接用 ThreadLocalRandom；双重检查锁延迟初始化推荐将目标属性声明为 volatile

### 【参考】volatile 解决多线程内存不可见问题，一写多读可同步，多写不行——count++ 用 AtomicInteger，JDK8 推荐 LongAdder；HashMap 容量不够 resize 时高并发可能死链导致 CPU 飙升
