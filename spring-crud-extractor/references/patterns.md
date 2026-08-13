# Extraction Patterns Reference

Load this file when extraction results are missing CRUD operations, when interface tracing fails, or when adding support for a new pattern.

## Contents

1. [Interface tracing](#interface-tracing)
2. [MyBatis XML mappers](#mybatis-xml-mappers)
3. [MyBatis annotation mappers](#mybatis-annotation-mappers)
4. [Spring Data JPA repositories](#spring-data-jpa-repositories)
5. [Spring JDBC](#spring-jdbc)
6. [Table and parameter heuristics](#table-and-parameter-heuristics)
7. [Edge cases](#edge-cases)
8. [Adding a new source](#adding-a-new-source)

## Interface tracing

Input: an interface list in YAML (preferred) or JSON. Each item can be traced automatically (`Controller → Service → Mapper/Repository → SQL`) or pinned with explicit hints.

### YAML interface lists

Each item is a mapping; fewer fields means more searching:

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

- A lone `controller_method` / `method_name` (no class) is searched across all parsed classes; Controller hits rank first, then Service, then Mapper/Repository.
- HTTP verb + path (`POST /api/users`) is resolved against Spring MVC annotations (`@GetMapping`/`@PostMapping`/… with optional path, `@RequestMapping(method = …)`), combined with the class-level `@RequestMapping` base path.
- `--auto` builds that interface list for you from every discovered endpoint, so you can provide no interface info at all.

Resolution rules:

- Entry point: `controller + controller_method`, else `service + service_method`, else `mapper`/`repository` directly.
- Per Java file, the tool builds a light symbol table: class name, field types (`private final UserService userService;`), constructor/method parameter types, and method bodies.
- A call `var.method(...)` is followed when `var` resolves to a known type and the type name ends with `Controller`, `Service`, `Mapper`, `Repository`, `Dao`, `Manager`, or `Jdbc`. `this.method(...)` calls are followed within the same class.
- A CRUD operation matches when `op.owner == type` and `op.method == called method`:
  - MyBatis XML: owner = last segment of the mapper `namespace`.
  - MyBatis annotation/provider: owner = file stem (e.g. `UserMapper.java` → `UserMapper`).
  - JPA repository: owner = repository interface name.
  - Spring JDBC: owner = file stem of the class, method = enclosing method of the `jdbcTemplate` call.
- Trace depth defaults to 4; raise with `--depth` when chains are longer.

Troubleshooting:

- `class not found: X`: the class file may be absent or the simple name collides between packages; use a fully qualified name or check the class name spelling.
- `method has no body or is not found: X.m`: the method name is wrong, or it is a repository/interface method that only matches CRUD ops directly (check `owner`/`method` spelling in the record).
- `no CRUD operation matched: X.m`: the method exists but no mapper/repository/JdbcTemplate SQL was found behind it, or the SQL is built dynamically.
- `no endpoint matched: POST /api/xxx`: the Spring MVC annotations don't resolve to that path/method; check the controller mappings or switch to `Class.method` form.
- Add `mapper` + `mapper_methods` or `repository` + `repository_methods` hints when the call chain cannot be resolved statically.

## MyBatis XML mappers

Files matched: `*Mapper.xml` anywhere under the project root.

Example:

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

Captured: `source=mybatis-xml`, `owner` (namespace last segment), `namespace`, `method=id`, `operation` from the tag, `entity` from `resultType` (simple name), tables and params from the SQL text. Dynamic tags (`<if>`, `<where>`, `<foreach>`) are flattened: their text is included, their attributes are ignored.

## MyBatis annotation mappers

Files matched: any `.java` containing `@Select/@Insert/@Update/@Delete` or the `@*Provider` variants.

Example (string-concatenated SQL is supported):

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

Captured: `source=mybatis-annotation` with flattened SQL, or `source=mybatis-provider` with `sql=null` (SQL is built dynamically in the provider class). `operation` comes from the annotation name.

## Spring Data JPA repositories

Files matched: interfaces/classes extending `CrudRepository`, `JpaRepository`, or `PagingAndSortingRepository`.

Example:

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

Derived query names are classified by prefix:

| Prefix | Operation |
| --- | --- |
| `find`, `get`, `read`, `query`, `search`, `count`, `exists`, `select`, `list`, `all` | SELECT |
| `save`, `insert`, `persist`, `create`, `add`, `store` | INSERT |
| `update`, `modify`, `touch` | UPDATE |
| `delete`, `remove`, `purge` | DELETE |

`@Query` SQL is captured as-is; the operation comes from the SQL's first keyword (so `@Modifying` + `update …` yields UPDATE). Derived queries have `sql=null` and `params` extracted from the method signature. JPQL entity names (`update User u …`) are mapped back to the physical table via `@Table(name=…)` on the entity.

## Spring JDBC

Files matched: any `.java` calling `JdbcTemplate` / `NamedParameterJdbcTemplate` methods (`query`, `queryForList`, `queryForObject`, `queryForMap`, `queryForRowSet`, `queryForStream`, `update`, `batchUpdate`, `execute`) with an inline SQL string. The variable name must contain `jdbc` (covers `jdbcTemplate`, `namedJdbcTemplate`, `jdbc`, …).

Example:

```java
public int updatePassword(Long id, String password) {
    return jdbcTemplate.update(
        "UPDATE t_user SET password = :password WHERE id = :id",
        new MapSqlParameterSource()
            .addValue("id", id)
            .addValue("password", password));
}
```

Captured: `source=spring-jdbc`, `method` = enclosing Java method (best-effort), SQL text, tables, and params (`:name` and `?1..n`). Calls that pass SQL via a variable (e.g. `String sql = "…"; jdbcTemplate.query(sql, …)`) are not captured.

## Table and parameter heuristics

- Tables come from the first keyword of the SQL: `INSERT [INTO] t`, `UPDATE t SET`, `DELETE FROM t`, `SELECT … FROM t`; `JOIN t` clauses are appended.
- JPA tables resolve from `@Table(name = "…")` on the entity class, falling back to snake_case of the entity simple name (`UserInfo` → `user_info`).
- Params: MyBatis `#{name}` / `${name}`, JDBC named `:name` and positional `?` markers (reported as `?1`, `?2`, …).

## Edge cases

- Provider-based dynamic SQL (`@*Provider`, XML `<script>`): `sql=null`; locate the provider method manually when SQL text is needed.
- Dynamically built SQL (string concatenation in Java code, `StringBuilder`): not captured.
- Multi-statement blocks (e.g. a `<select>` emitting several statements): only the first operation/table is classified.
- Same simple class name in different packages: the last parsed file wins for tracing; use fully qualified names or `mapper`/`repository` hints.
- Duplicate records (same source/file/owner/method/SQL) are de-duplicated.

## Adding a new source

1. Add a scanner function in `scripts/extract.py` returning records with the same keys as the existing extractors (`source`, `file`, `owner`, `namespace`, `method`, `operation`, `entity`, `table`, `tables`, `params`, `sql`).
2. Register it in `analyze` (XML-based sources go in the first loop, Java-text sources in the second loop).
3. Document the pattern here with one input/output example.
4. Add the `source` value to the SKILL.md supported-sources list; summary counts are computed generically.
