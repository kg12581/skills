---
name: spring-crud-extractor
description: 读取 JSON 接口定义（api_url / 请求方法 / 请求头 / 请求体），分析 Java Spring 代码仓中该接口背后的 SQL CRUD 操作（MyBatis 系列、Spring Data JPA、JdbcTemplate、Hibernate 等），生成包含 setup 和 teardown SQL 的 YAML 报告。以文字流程为主、候选收集脚本为辅，覆盖各种 Spring 写法。当用户需要根据 API 地址和方法提取测试用 SQL、为接口生成准备/清理数据脚本、或梳理某个 API 背后的数据操作时使用。
---

# Spring SQL CRUD 提取器

## 概述

输入：JSON 接口定义，每个接口包含 `api_url`、请求方法（method）、请求头（headers）、请求体（body）。

输出：YAML 报告，每个接口给出调用链和 SQL，并按用途分成 `setup`（准备数据）和 `teardown`（清理数据）两部分。

定位：**文字流程为主，脚本为辅**。脚本只是一个“候选收集器”，用简单规则粗扫代码里的 SQL 候选和端点；真正的分析、判断和补全（尤其框架生成 SQL）靠文字流程完成。脚本结果与直接读代码不一致时，以代码为准。

## 文字流程（默认主路径）

按以下四步执行，不要跳过判断步骤。

### 手工分析原则

- 先读代码再下结论：每个 SQL 都要能在源码里找到出处，不猜、不编。
- 以真实调用为准：字段注入、构造器注入、静态方法、反射、AOP 代理都可能让“看起来像调用”的代码不成立。
- 有歧义就并列给出候选并写进 notes，而不是硬选一个。
- 一个接口里可能有多个数据操作（事务方法、循环、批量调用），全部列出，按调用顺序排列。

### 第 1 步：读取 JSON 接口定义

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

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `api_url` | 是 | 接口路径；可带域名，分析时去掉协议和域名只留路径 |
| `method` | 是 | GET / POST / PUT / DELETE / PATCH |
| `headers` | 否 | 请求头，原样回显到报告 |
| `body` | 否 | 请求体（对象或 JSON 字符串），回显并把匹配的参数值绑定到 setup SQL |
| `id` | 否 | 报告中的标识，缺省用 `方法 路径` |

判断点：

- `api_url` 带域名/端口时先去掉，只留路径。
- 路径匹配不到时，尝试去掉或加上常见前缀（`/api`、`/v1`），检查类级 `@RequestMapping` 是否带变量。
- 多个 Controller 方法映射同一路径时，全部列为候选。
- 匹配不到端点时，可在 JSON 里补 `controller` + `controller_method` 提示字段直接指定入口。

### 第 2 步：追踪调用链

对每个入口方法：

1. 打开方法体，找出所有 `对象.方法(...)` 调用。
2. 确定对象类型：查字段声明（`private final UserService userService;`）、构造器参数、`@Autowired` 字段、Lombok `@RequiredArgsConstructor` 生成的构造器、方法参数。
3. 类型名以 `Controller`、`Service`、`Mapper`、`Repository`、`Dao`、`Manager`、`Jdbc` 结尾 → 进入该类型同名方法继续追踪；`this.方法()` 在本类内继续。
4. 递归直到落到 Mapper/Repository/JdbcTemplate 方法，或找不到更多调用为止。
5. 记录调用链，例如：`UserController.createUser → UserService.createUser → UserMapper.insertUser`。

判断点：

- 调用可能绕层：Controller → Facade → Service → Service → Mapper，一直追到 SQL 为止。
- 遇到接口类型（`UserMapper`、`UserRepository`），去实现、XML、父接口里找 SQL；继承方法（`ServiceImpl.save`、`JpaRepository.deleteById`）去父类/父接口确认。
- 静态方法、lambda、反射里的调用，人工判断是否真的落到数据库；无法确认就标注。

### 第 3 步：提取 SQL（覆盖所有 Spring 写法）

先按下面的清单对照项目的写法，再逐条提取 SQL。详细规则见 [references/patterns.md](references/patterns.md)：

| Spring 写法 | 怎么提取 SQL |
| --- | --- |
| MyBatis XML（`*Mapper.xml`） | `<select/insert/update/delete>`：id=方法名，namespace 末段=所属类，resultType=实体，SQL 展开 `<if>/<where>/<foreach>` |
| MyBatis 注解（`@Select/@Insert/@Update/@Delete`） | 直接取字符串（含 `"a" + "b"` 拼接） |
| MyBatis Provider（`@*Provider`） | `sql: null`，去 Provider 方法里找 SQL 文本 |
| MyBatis-Plus（`IService/ServiceImpl/BaseMapper` + `QueryWrapper`） | 框架生成 SQL：写 `sql: null`，注明实体/表与操作 |
| Spring Data JPA 派生方法（`findBy…/deleteBy…/count…`） | 按方法名前缀定操作，表=实体 `@Table(name)` 或实体名转下划线，`sql: null` |
| Spring Data JPA `@Query` | 直接取 JPQL/SQL，操作取第一个关键字 |
| JPA `Specification` / `Example` | 框架动态生成：`sql: null`，注明实体/表 |
| JdbcTemplate / NamedParameterJdbcTemplate | 取内联 SQL；变量 SQL 从赋值处拼出片段并标注动态 |
| SqlSessionTemplate / sqlSession | 按 mapper 方法名找对应 SQL |
| Hibernate `Session.createQuery/createSQLQuery` | 直接取 JPQL/SQL |
| Spring Data JDBC（`@Query` 仓库） | 直接取 SQL |
| 动态 SQL（`<script>`、`StringBuilder`） | 给 SQL 骨架 + 说明，不伪造完整语句 |

每条记录固定字段：`source`、`file`、`owner`、`method`、`operation`、`entity`、`table`、`tables`、`params`、`sql`；请求体对象会按参数名绑定到 `values`。

### 第 4 步：组装 setup / teardown SQL

- `setup` = 调用链中的 INSERT / UPDATE SQL（请求前准备数据）。
- `teardown` = 调用链中的 DELETE SQL，再加上对 setup 涉及、但缺少 DELETE 的表自动补充的 `DELETE FROM <表>`（`source: generated`，清理模板，正式使用前按需补 WHERE）。
- 接口只有 SELECT 时，setup/teardown 为空，并在 notes 说明。

状态：入口缺失 → `not_found`；追踪成功但无 SQL → `no_crud_found`；有结果但有提示 → `partial`；全部正常 → `ok`。

## 灵活场景处理（脚本容易漏、需要人工判断的场景）

| 场景 | 怎么处理 |
| --- | --- |
| 同名类在不同包 | 用全限定名区分；报告写清楚用的是哪个类，或列出候选 |
| 方法重载 | 按参数类型区分；不确定时列出所有重载对应的 SQL |
| 继承 / 接口默认方法 | 去父类、父接口、`ServiceImpl`、`BaseMapper` 找方法和 SQL |
| MyBatis-Plus | `save/removeById/list + QueryWrapper` 框架生成 SQL：标 `sql: null` 并注明实体/表 |
| JPA `Specification` / `Example` | 同上，框架动态生成，无法给完整 SQL |
| 事务接口多次写库 | 按调用顺序把所有 INSERT/UPDATE/DELETE 都放进 setup/teardown |
| 动态 SQL | 给骨架 + 说明，不伪造完整语句 |
| 多数据源 | 无法确认时报告表名并提示“请确认数据源” |
| 纯 Service 无 Controller | 在 JSON 里补 `controller` 或直接给 `service` 字段 |
| 脚本解析失败 | 按文字流程人工读代码补全，并在 notes 注明“脚本未识别，人工补充” |

## 完整示例（文字流程演示）

输入 `{"apis": [{"api_url": "/api/users", "method": "POST", "body": {"name": "张三"}}]}`：

1. 读 `UserController`：类级 `@RequestMapping("/api/users")` + `@PostMapping` → 方法 `create`。
2. 方法体 `return userService.create(user);` → `userService` 是构造器注入的 `UserService`，进入 `UserService.create`。
3. `UserService.create` 里调用 `userMapper.insertUser(user)` 和 `userRepository.save(user)`。
4. `insertUser` 有 `@Insert("INSERT INTO t_user (name) VALUES (#{name})")`；`save` 是 JPA 继承方法，表为 `t_user`（框架生成，`sql: null`）。
5. 组装：setup = 两条 INSERT（`values` 绑定 `name: 张三`）；teardown = 自动生成 `DELETE FROM t_user`。

## 脚本快通道（可选）

```bash
spring-crud-extractor /path/to/repo apis.json --out report.yaml
spring-crud-extractor /path/to/repo --auto --out report.yaml
spring-crud-extractor /path/to/repo            # 全量导出 SQL 候选
```

- 脚本只收集代码中能直接找到的 SQL 候选（`mybatis-xml` / `java-sql`），框架生成部分（JPA 派生、MyBatis-Plus、Provider、动态 SQL）不会出现，需按文字流程人工补全。
- `--auto` 自动发现所有 Controller 端点；`--out` 写 YAML（默认）、`--format json`、`--no-sql`、`--depth` 调整追踪深度。
- 报告顶部 `note` 会提示候选收集器的局限。

## 输出 YAML 示例

```yaml
mode: api-sql-setup
project: /path/to/repo
apis_count: 1
note: 本报告由候选收集器生成……
summary:
  apis_total: 1
  apis_with_sql: 1
  crud_total: 2
  crud_by_operation: {INSERT: 1, DELETE: 1}
apis:
  - id: create-user
    api_url: /api/users
    method: POST
    headers: {Content-Type: application/json}
    body: {name: 张三}
    entry: {class: UserController, method: create}
    call_chain: [UserController.create, UserService.create, UserMapper.insertUser]
    setup:
      - source: java-sql
        owner: UserMapper
        method: insertUser
        operation: INSERT
        table: t_user
        params: [name]
        values: {name: 张三}
        sql: INSERT INTO t_user (name) VALUES (#{name})
    teardown:
      - source: generated
        operation: DELETE
        table: t_user
        sql: DELETE FROM t_user
    trace: {status: ok, notes: []}
```

## 支持的 SQL 来源

- `mybatis-xml`：MyBatis `*Mapper.xml`。
- `java-sql`：Java 代码中的 SQL 字符串（MyBatis 注解、JPA `@Query`、JdbcTemplate 等）。
- `generated`：自动生成的 teardown 模板。
- 框架生成（JPA 派生、MyBatis-Plus、Provider、动态 SQL）：`sql: null`，由文字流程补全并注明。

## 结果不完整时怎么办

- 先看 `trace.notes` 和报告顶部的 `note`。
- `没有匹配到端点`：检查 `api_url` 拼写和 `method`，或补 `controller` + `controller_method` 提示字段。
- 脚本结果与直接读代码不一致：以代码为准，按文字流程人工补全并说明差异。
- 生成的 teardown `DELETE FROM <表>` 是模板，正式使用前按测试数据特征补 WHERE 条件。
