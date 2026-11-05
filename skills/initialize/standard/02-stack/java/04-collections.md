## 四、集合处理规范

### 【强制】覆写 equals 就必须覆写 hashCode；Set 存储的对象、自定义对象作为 Map 的键必须覆写这两种方法

### 【强制】判断集合是否为空用 isEmpty()，而不是 size()==0（部分集合前者为 O(1) 且可读性更好）

### 【强制】Collectors.toMap() 转 Map 时必须使用含 BinaryOperator mergeFunction 的重载，且注意 value 为 null 会抛 NPE

说明：key 重复时不带 mergeFunction 抛 `IllegalStateException`；HashMap.merge 内部对 null value 直接抛 `NullPointerException`。正例：`Collectors.toMap(Pair::getKey, Pair::getValue, (v1, v2) -> v2)`。

### 【强制】ArrayList 的 subList() 结果不可强转成 ArrayList（是内部类 SubList，原列表的视图）；对父集合增删会导致子列表遍历/增删抛 ConcurrentModificationException

### 【强制】Map 的 keySet()/values()/entrySet() 返回集合不可添加元素；Collections.emptyList()/singletonList() 等是 immutable list，不可增删

### 【强制】集合转数组必须用 toArray(T[] array)，传入类型完全一致、长度为 0 的空数组

正例：`String[] array = list.toArray(new String[0]);`。无参 toArray 只能返回 Object[]，强转抛 ClassCastException。

### 【强制】Collection 的 addAll() 要对输入集合参数做 NPE 判断；Arrays.asList() 返回的集合不能调用 add/remove/clear（适配器模式，后台数据仍是数组）

### 【强制】泛型通配符遵循 PECS 原则：<? extends T> 的集合不能 add，<? super T> 不能 get；频繁往外读取用 extends，经常往里插入用 super

### 【强制】无泛型限制的集合赋值给泛型限制的集合后，使用元素时需做 instanceof 判断；不要在 foreach 循环里 remove/add 元素，remove 用 Iterator 方式（并发时对 Iterator 加锁）

### 【强制】JDK7 及以上 Comparator 实现必须满足：x 与 y 比较结果和 y 与 x 相反、传递性、x=y 则 x/z 与 y/z 比较相同，否则 sort 抛 IllegalArgumentException

反例：`return o1.getId() > o2.getId() ? 1 : -1;` 未处理相等情形。

### 【推荐】JDK7 及以上使用 diamond 语法 `new HashMap<>(16)`；集合初始化时指定初始值大小：initialCapacity =（元素数 / 负载因子 0.75）+ 1，无法确定时用默认值 16

### 【推荐】用 entrySet 遍历 Map KV 而非 keySet（keySet 实际遍历两次）；JDK8 用 Map.forEach

### 【推荐】注意 Map 类集合 K/V 能否存 null：HashMap 均可；Hashtable key/value 均不允许；ConcurrentHashMap key/value 均不允许（存 null 抛 NPE）；TreeMap key 不允许、value 允许

### 【参考】利用 Set 元素唯一特性做去重，避免 List.contains() 遍历去重；合理区分集合的有序性(sort)与稳定性(order)：ArrayList 是 order/unsort，HashMap 是 unorder/unsort，TreeSet 是 order/sort
