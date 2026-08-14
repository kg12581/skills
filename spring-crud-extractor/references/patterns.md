# 提取模式参考

本文件是提取模式的细节参考，配合 SKILL.md 的文字流程使用：手工分析时按本文件的规则执行；脚本（候选收集器）结果异常、需要覆盖新的 Spring 写法或新增提取模式时，也先读本文件。

## 目录

1. [接口追踪](#接口追踪)
2. [Spring 写法全覆盖清单](#spring-写法全覆盖清单)
3. [MyBatis XML 映射器](#mybatis-xml-映射器)
4. [MyBatis 注解映射器](#mybatis-注解映射器)
5. [Spring Data JPA 仓库](#spring-data-jpa-仓库)
6. [Spring JDBC](#spring-jdbc)
7. [表名与参数推断规则](#表名与参数推断规则)
8. [边界情况](#边界情况)
9. [新增提取模式](#新增提取模式)

## 接口追踪

输入为 JSON 接口定义（每个条目含 `api_url`、`method`、`headers`、`body`）。每个条目按 `Controller → Service → Mapper/Repository → SQL` 追踪，也可以通过提示字段直接定位入口。

### JSON 接口定义（api_url / 请求方法 / 请求头）

```json
{
  "apis": [
    {
      "id": "create-user",
      "api_url": "http://localhost:8080/api/users",
      "method": "POST",
      "headers": {"Content-Type": "application/json"},
      "body": {"name": "张三", "age": 18}
    }
  ]
}
```

- `api_url` 会去掉协议和域名只留路径，再按（方法, 路径）匹配 Spring MVC 端点。
- `headers` 原样回显到报告的每个接口条目中。
- `body`（请求体）原样回显，并在 setup 的 INSERT/UPDATE 记录上按参数名绑定值（`values` 字段）；JSON 字符串会自动解析成对象。
- 匹配不到端点时，报告 `没有匹配到端点：方法 路径`；可在 JSON 里补 `controller` + `controller_method` 提示字段。

### 入口提示字段

匹配不到端点或想直接指定入口时，可在 JSON 条目里加提示字段：

```json
{
  "apis": [
    {
      "api_url": "/api/users",
      "method": "POST",
      "controller": "UserController",
      "controller_method": "createUser",
      "service": "UserService",
      "service_method": "createUser"
    }
  ]
}
```

- `controller` + `controller_method`：直接指定入口，跳过端点匹配。
- `service` + `service_method`：从 Service 方法开始追踪。
- `--auto` 自动发现所有 Controller 端点，无需输入文件。

### setup / teardown 生成规则

- `setup` = 该接口调用链中的 INSERT / UPDATE 操作（请求前准备数据）；请求体的值会按参数名绑定（`values` 字段）。
- `teardown` = 该接口调用链中的 DELETE 操作，加上对 setup 涉及、但缺少 DELETE 的表自动补充的 `DELETE FROM <表>`（标记 `source: generated`，作为清理模板）。
- 接口只有 SELECT 操作时，setup/teardown 为空，并在 `trace.notes` 说明。

### 解析规则

- 脚本用“方法名粗链”：某类方法体里调用 `x.y(...)`，只要 `y` 在另一个可追踪类（Controller/Service/Mapper/Repository/Dao/Manager/Jdbc）里声明，就连过去；不解析字段类型。
- SQL 候选来源：`mybatis-xml`（XML mapper）和 `java-sql`（Java 字符串字面量里的 SQL）。
- 框架生成的 SQL（JPA 派生、MyBatis-Plus、Provider、动态 SQL）脚本收集不到，按文字流程人工补全。
- 追踪深度默认 4；链路较长时用 `--depth` 提高。

### Spring 写法全覆盖清单

对照项目实际写法，逐条确认提取方式：

| 写法 | 示例 | 提取方式 |
| --- | --- | --- |
| MyBatis XML | `<select id="findById">…</select>` | 直接取 SQL；`<if>/<where>/<foreach>` 展开 |
| MyBatis 注解 | `@Insert("INSERT …")` | 直接取字符串（含 `"a" + "b"` 拼接） |
| MyBatis Provider | `@SelectProvider(type=…, method=…)` | `sql: null`，去 Provider 方法找 SQL |
| MyBatis-Plus | `IService.save` / `BaseMapper.selectList(wrapper)` | 框架生成：`sql: null`，注明实体/表/操作 |
| JPA 派生方法 | `findByStatus` / `deleteByName` | 按前缀定操作，表=实体 `@Table` 或转下划线，`sql: null` |
| JPA `@Query` | `@Query("update User u …")` | 直接取 JPQL/SQL |
| JPA `Specification` / `Example` | `repository.findAll(spec)` | 框架生成：`sql: null`，注明实体/表 |
| JdbcTemplate | `jdbcTemplate.query("SELECT …", …)` | 取内联 SQL；变量 SQL 从赋值处拼片段并标注 |
| SqlSessionTemplate | `sqlSession.selectList("demo.UserMapper.find", …)` | 按 statement id 找对应 XML/注解 SQL |
| Hibernate Session | `session.createQuery("from User …")` | 直接取 JPQL/SQL |
| Spring Data JDBC | `@Query` 仓库方法 | 直接取 SQL |
| 动态 SQL | `<script>`、`StringBuilder`、`QueryWrapper` | 给骨架 + 说明，不伪造完整语句 |

一个接口里出现多种写法时（如 MyBatis + JPA + JdbcTemplate 混用），每种都提取，按调用顺序放进 setup/teardown。

### 排查方法

- `没有匹配到端点：方法 路径`：检查 `api_url` 拼写和 `method`，或改用 `controller` + `controller_method` 提示字段。
- `只收集到 SELECT 候选`：该接口的写操作来自框架生成（JPA 派生、MyBatis-Plus 等），按文字流程补全。
- 未找到类/方法（手工追踪时）：类文件可能不存在或不同包下有同名简单类；用全限定名或检查拼写。
- `没有匹配到端点：POST /api/xxx`：Spring MVC 注解解析不到该路径/方法；检查 Controller 映射，或改用 `类.方法` 写法。
- 静态解析不出调用链时，补充 `controller`/`service` 提示字段，或按文字流程人工读代码补全。

## MyBatis XML 映射器

匹配文件：项目根目录下所有 `*Mapper.xml`。

示例：

```xml
<mapper namespace="com.example.mapper.UserMapper">
  <select id="findById" resultType="com.example.entity.User">
    SELECT * FROM t_user WHERE id = #{id}
  </select>
  <insert id="insertUser" useGeneratedKeys="true" keyProperty="id">
    INSERT INTO t_user (name, age) VALUES (#{name}, #{age})
  </insert>
</mapper>
```

捕获内容：`source=mybatis-xml`、`owner`（namespace 最后一段）、`namespace`、`method=id`、按标签得出的 `operation`、`resultType` 的简单类名作为 `entity`，以及从 SQL 文本提取的表名和参数。动态标签（`<if>`、`<where>`、`<foreach>`）会被展开：只保留其文本，忽略其属性。

## MyBatis 注解映射器

匹配文件：任何包含 `@Select/@Insert/@Update/@Delete` 或 `@*Provider` 变体的 `.java` 文件。

示例（支持字符串拼接 SQL）：

```java
@Mapper
public interface UserMapper {
    @Select("SELECT id, name FROM t_user " +
            "WHERE name = #{name}")
    User findByName(@Param("name") String name);

    @InsertProvider(type = UserSqlProvider.class, method = "insertSql")
    int insertUser(User user);
}
```

捕获内容：`source=mybatis-annotation` 并带展开后的 SQL，或 `source=mybatis-provider` 且 `sql=null`（SQL 在 Provider 类中动态构建）。`operation` 来自注解名。

## Spring Data JPA 仓库

匹配文件：继承 `CrudRepository`、`JpaRepository` 或 `PagingAndSortingRepository` 的接口/类。

示例：

```java
@Repository
public interface UserRepository extends JpaRepository<User, Long> {
    List<User> findByStatus(String status);

    @Modifying
    @Query("update User u set u.status = :status where u.id = :id")
    int updateStatus(@Param("id") Long id, @Param("status") String status);

    void deleteById(Long id);
}
```

派生查询方法名按前缀分类：

| 前缀 | 操作 |
| --- | --- |
| `find`、`get`、`read`、`query`、`search`、`count`、`exists`、`select`、`list`、`all` | SELECT |
| `save`、`insert`、`persist`、`create`、`add`、`store` | INSERT |
| `update`、`modify`、`touch` | UPDATE |
| `delete`、`remove`、`purge` | DELETE |

`@Query` SQL 原样捕获，操作类型取 SQL 第一个关键字（所以 `@Modifying` + `update …` 得到 UPDATE）。派生查询 `sql=null`，`params` 从方法签名提取。JPQL 实体名（`update User u …`）会通过实体上的 `@Table(name=…)` 映射回物理表名。

## Spring JDBC

匹配文件：任何调用 `JdbcTemplate` / `NamedParameterJdbcTemplate` 方法（`query`、`queryForList`、`queryForObject`、`queryForMap`、`queryForRowSet`、`queryForStream`、`update`、`batchUpdate`、`execute`）且 SQL 为内联字符串的 `.java` 文件。变量名必须包含 `jdbc`（覆盖 `jdbcTemplate`、`namedJdbcTemplate`、`jdbc` 等）。

示例：

```java
public int updatePassword(Long id, String password) {
    return jdbcTemplate.update(
        "UPDATE t_user SET password = :password WHERE id = :id",
        new MapSqlParameterSource()
            .addValue("id", id)
            .addValue("password", password));
}
```

捕获内容：`source=spring-jdbc`、所在方法名（尽力而为）、SQL 文本、表名和参数（`:name` 与 `?1..n`）。SQL 通过变量传入的调用（如 `String sql = "…"; jdbcTemplate.query(sql, …)`）无法捕获。

## 表名与参数推断规则

- 表名取自 SQL 第一个关键字：`INSERT [INTO] t`、`UPDATE t SET`、`DELETE FROM t`、`SELECT … FROM t`；`JOIN t` 子句会追加进去。
- JPA 表名通过实体类上的 `@Table(name = "…")` 解析，缺省回退为实体简单名转下划线（`UserInfo` → `user_info`）。
- 参数：MyBatis `#{name}` / `${name}`、JDBC 命名参数 `:name` 和位置参数 `?`（报告为 `?1`、`?2` …）。

## 边界情况

- Provider 动态 SQL（`@*Provider`、XML `<script>`）：`sql=null`，需要 SQL 文本时手动到 Provider 方法中查找。
- 动态拼接 SQL（Java 代码字符串拼接、`StringBuilder`）：无法捕获。
- 多语句块（如一个 `<select>` 输出多条语句）：只分类第一个操作/表。
- 不同包下同名简单类：追踪时以最后解析到的文件为准；可使用全限定名或 `mapper`/`repository` 提示字段。
- 重复记录（source/file/owner/method/SQL 相同）会自动去重。

## 灵活场景处理（人工判断为主）

脚本是正则规则，遇到以下场景可能解析失败或解析偏。遇到时按文字流程人工读代码补全，不要依赖脚本结果：

| 场景 | 处理方式 |
| --- | --- |
| 继承方法（`ServiceImpl`、`BaseMapper`、父接口） | 去父类/父接口找方法体和 SQL；框架方法标 `sql: null` 并注明实体/表 |
| MyBatis-Plus `QueryWrapper` / `LambdaQueryWrapper` | SQL 由框架生成；记录实体/表，标“框架动态生成” |
| JPA `Specification` / `Example` / `@Query` 拼接 | 同上；`@Query` 能拿到的部分照写，其余标注动态 |
| 方法重载 / 同名类 | 用全限定名和参数签名区分；不确定时并列列出候选 |
| 事务方法内多次写库 | 按调用顺序收集全部 INSERT/UPDATE/DELETE，不要只取第一个 |
| 静态工具类、反射、AOP | 无法静态确认是否写库时，标注“无法静态确认”，不硬写 SQL |
| 多数据源 | 报告表名并提示确认数据源；不要假设默认数据源 |
| `JdbcTemplate` 变量 SQL | 从变量赋值处拼出片段，标注“动态拼接”，不给伪完整 SQL |

## 新增提取模式

1. 在 `scripts/extract.py` 中新增扫描函数，返回与现有提取器相同的字段（`source`、`file`、`owner`、`namespace`、`method`、`operation`、`entity`、`table`、`tables`、`params`、`sql`）。
2. 在 `analyze` 中注册（基于 XML 的源放在第一个循环，基于 Java 文本的源放在第二个循环）。
3. 在本文件补充一个输入/输出示例。
4. 在 SKILL.md 的支持来源列表中补充该 `source` 值；统计计数是通用计算，无需额外处理。
