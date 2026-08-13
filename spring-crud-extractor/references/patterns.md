# 提取模式参考

当提取结果缺失 CRUD 操作、接口追踪失败，或需要新增提取模式时，先读本文件。

## 目录

1. [接口追踪](#接口追踪)
2. [MyBatis XML 映射器](#mybatis-xml-映射器)
3. [MyBatis 注解映射器](#mybatis-注解映射器)
4. [Spring Data JPA 仓库](#spring-data-jpa-仓库)
5. [Spring JDBC](#spring-jdbc)
6. [表名与参数推断规则](#表名与参数推断规则)
7. [边界情况](#边界情况)
8. [新增提取模式](#新增提取模式)

## 接口追踪

输入为 YAML（推荐）或 JSON 接口清单。每个条目可以自动追踪（`Controller → Service → Mapper/Repository → SQL`），也可以通过显式提示字段直接定位。

### YAML 接口清单

每个条目是一个映射，字段越少，自动搜索越多：

```yaml
interfaces:
  - id: create-user
    http_method: POST
    path: /api/users
    controller: UserController
    controller_method: createUser
    service: UserService
    service_method: createUser
    mapper: UserMapper
    mapper_methods: [insertUser]

  - controller_method: findUser
  - method_name: deleteUser
```

- 单独的 `controller_method` / `method_name`（不带类名）会在所有解析到的类中搜索方法；Controller 命中优先，其次 Service，再其次 Mapper/Repository。
- HTTP 方法 + 路径（`POST /api/users`）会匹配 Spring MVC 注解（`@GetMapping`/`@PostMapping`/…，可带路径，也支持 `@RequestMapping(method = …)`），并结合类级 `@RequestMapping` 基础路径。
- `--auto` 会根据发现的全部端点自动生成这份接口清单，因此可以完全不提供接口信息。

### 解析规则

- 入口优先级：`controller + controller_method`，其次 `service + service_method`，再其次直接指定 `mapper`/`repository`。
- 对每个 Java 文件建立轻量符号表：类名、字段类型（`private final UserService userService;`）、构造方法/方法参数类型、方法体。
- 当 `var.method(...)` 中 `var` 能解析为已知类型，且类型名以 `Controller`、`Service`、`Mapper`、`Repository`、`Dao`、`Manager` 或 `Jdbc` 结尾时，会继续追踪该调用；`this.method(...)` 在本类内继续追踪。
- CRUD 操作匹配条件：`op.owner == 类型名` 且 `op.method == 被调用方法名`：
  - MyBatis XML：owner = mapper `namespace` 的最后一段。
  - MyBatis 注解/Provider：owner = 文件名去掉扩展名（如 `UserMapper.java` → `UserMapper`）。
  - JPA 仓库：owner = 仓库接口名。
  - Spring JDBC：owner = 所在类文件名去扩展名，method = `jdbcTemplate` 调用所在的方法。
- 追踪深度默认 4；链路较长时用 `--depth` 提高。

### 排查方法

- `未找到类：X`：类文件可能不存在，或不同包下有同名简单类；改用全限定名，或检查类名拼写。
- `方法不存在或无方法体：X.m`：方法名不对，或者它是只匹配 CRUD 操作的仓库/接口方法（检查记录里的 `owner`/`method` 拼写）。
- `未匹配到 CRUD 操作：X.m`：方法存在，但背后没有 mapper/repository/JdbcTemplate SQL，或 SQL 是动态拼接的。
- `没有匹配到端点：POST /api/xxx`：Spring MVC 注解解析不到该路径/方法；检查 Controller 映射，或改用 `类.方法` 写法。
- 静态解析不出调用链时，补充 `mapper` + `mapper_methods` 或 `repository` + `repository_methods` 提示字段。

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

## 新增提取模式

1. 在 `scripts/extract.py` 中新增扫描函数，返回与现有提取器相同的字段（`source`、`file`、`owner`、`namespace`、`method`、`operation`、`entity`、`table`、`tables`、`params`、`sql`）。
2. 在 `analyze` 中注册（基于 XML 的源放在第一个循环，基于 Java 文本的源放在第二个循环）。
3. 在本文件补充一个输入/输出示例。
4. 在 SKILL.md 的支持来源列表中补充该 `source` 值；统计计数是通用计算，无需额外处理。
