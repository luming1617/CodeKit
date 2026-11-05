## 十二、MySQL 数据库规范

### 【强制】表达是与否概念的字段用 is_xxx 命名，类型 unsigned tinyint（1 是 0 否）；任何非负字段必须是 unsigned

### 【强制】表名、字段名必须小写字母或数字，禁止数字开头、禁止两个下划线中间只有数字；表名不使用复数名词；禁用保留字（desc、range、match、delayed 等）

MySQL 在 Linux 下默认区分大小写，禁止出现大写字母。

### 【强制】索引命名：主键 pk_字段名、唯一索引 uk_字段名、普通索引 idx_字段名

### 【强制】必备三字段：id（bigint unsigned 主键，自增步长 1）、create_time、update_time（均为 datetime）；表命名遵循"业务名称_表的作用"（alipay_task / trade_config），库名与应用名尽量一致

### 【强制】小数类型用 decimal，禁止 float 和 double；char 用于长度几乎相等的字符串；varchar 长度不超过 5000，超长用 text 并独立成表用主键对应

### 【强制】业务上具有唯一特性的字段（含组合字段）必须建唯一索引；超过三个表禁止 join，join 字段数据类型保持绝对一致且被关联字段需有索引

### 【强制】varchar 字段建索引必须指定索引长度（长度 20 的索引区分度一般可达 90% 以上）；页面搜索严禁左模糊或全模糊，需要时走搜索引擎（B-Tree 最左前缀匹配）

### 【推荐】order by 利用索引有序性：排序字段放组合索引最后，避免 file_sort；利用覆盖索引避免回表；超多分页用延迟关联或子查询优化（`SELECT t1.* FROM 表1 t1, (select id from 表1 where 条件 LIMIT 100000,20) t2 where t1.id=t2.id`）

### 【推荐】SQL 性能至少达到 range 级别，要求 ref 级别，最好 consts；组合索引区分度最高的在最左边，等号条件列前置于非等号列；防止字段类型不同造成隐式转换导致索引失效

### 【强制】count(*) 统计行数（不用 count(列名)/count(常量)）；count(distinct col1,col2) 一列全 NULL 时返回 0；sum() 注意全 NULL 返回 NULL 的 NPE 问题，用 `IFNULL(SUM(column), 0)`；用 ISNULL() 判断 NULL（NULL 与任何值直接比较都为 NULL）

### 【强制】分页查询 count 为 0 应直接返回；不得使用外键与级联（外键概念在应用层解决，级联是强阻塞有更新风暴风险）；禁止使用存储过程；数据订正（删除/修改）先 select 确认无误再执行 update

### 【强制】查询和变更涉及多表时，列名前必须加表别名（或表名）限定，避免同名字段抛 "Column is ambiguous"；推荐别名前加 as 并按 t1、t2、t3 顺序命名

### 【推荐】in 操作能避免则避免，实在避免不了则评估集合元素数量，控制在 1000 以内；单表行数超 500 万行或容量超 2GB 才推荐分库分表；字段允许适当冗余提高查询性能但须保证数据一致（非频繁修改、非唯一索引、非超长 varchar/text 字段才可冗余）

### 【强制】表查询一律不要使用 * 作为查询字段列表；POJO 布尔属性不加 is 而数据库字段加 is_，必须在 resultMap 中做映射；不要用 resultClass 当返回参数，每表必须有 <resultMap>；sql.xml 参数用 #{} / #param#，禁止 ${}（SQL 注入）；不允许直接拿 HashMap 与 Hashtable 作为查询结果集输出

### 【强制】更新数据表记录时必须同时更新 update_time 字段为当前时间

### 【推荐】不要写一个大而全的数据更新接口（update 所有字段），不更新无改动字段（易出错、效率低、增加 binlog）

### 【参考】@Transactional 事务不要滥用：影响数据库 QPS，且需考虑缓存回滚、搜索引擎回滚、消息补偿、统计修正等回滚方案；字符存储用 utf8 字符集，注意 LENGTH("轻松工作")=12 与 CHARACTER_LENGTH=4 的区别，存表情用 utf8mb4；TRUNCATE TABLE 快但无事务、不触发 trigger，不建议在开发代码中使用
