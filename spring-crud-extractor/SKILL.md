---
name: spring-crud-extractor
description: 给定 Java Spring 代码仓和 YAML 接口清单（controller/service/mapper/repository 类名 + 方法名），离线解析每个接口对应的 SQL CRUD 操作（MyBatis XML/注解、Spring Data JPA、JdbcTemplate），输出 YAML 报告（调用链与 SQL 明细）。当用户要求根据接口提取或分析 SQL 增删改查、梳理某个 API 背后的数据操作、或提供接口信息定位对应 CRUD SQL 时使用。
---

# Spring SQL CRUD 提取器

## 概述

离线工具：输入 Java Spring 代码仓和接口清单（YAML 或 JSON），输出每个接口的调用链和 SQL CRUD 操作（SELECT / INSERT / UPDATE / DELETE），来源覆盖 MyBatis XML/注解、Spring Data JPA 仓库和 JdbcTemplate，报告格式为 YAML。无需启动服务或联网。

## 快速开始

按 `interfaces.yaml` 中的接口清单提取：

```bash
spring-crud-extractor /path/to/repo interfaces.yaml
```

`spring-crud-extractor` 是本 skill 的 `scripts/extract.py` 的软链接，也可以在本目录直接运行（`python3 scripts/extract.py …`）。

不提供接口清单，自动发现所有 Controller 端点并逐一提取：

```bash
spring-crud-extractor /path/to/repo --auto
```

或者全量导出仓库中所有 CRUD 操作：

```bash
spring-crud-extractor /path/to/repo
```

选项说明：`--out report.yaml` 把结果写入 YAML 文件（默认输出格式就是 YAML）；`--format json` 切换为 JSON；`--no-sql` 隐藏原始 SQL 文本；`--depth 5` 提高调用链深度（默认 4）；`--interfaces-json '{...}'` 以内联 JSON 传入接口清单。

## 需要提供什么接口信息

YAML 接口清单——每一项是一个映射，字段写得越少，工具自动搜索得越多：

```yaml
# 接口清单
interfaces:
  - id: create-user
    http_method: POST
    path: /api/users
    controller: UserController
    controller_method: createUser

  - controller_method: findUser        # 只给方法名：在所有 Controller/Service 里搜索

  - method_name: deleteUser            # 等价写法

  - service: UserService               # 直接从 Service 开始追踪
    service_method: resetPassword
```

字段说明：

| 字段 | 示例 | 解析方式 |
| --- | --- | --- |
| `controller` + `controller_method` | `UserController` + `createUser` | 直接作为入口（推荐） |
| 只有 `controller_method` | `findUser` | 在所有 Controller/Service 类中搜索 |
| 只有 `method_name` | `deleteUser` | 同上，等价字段 |
| `http_method` + `path` | `POST` + `/api/users` | 匹配 Spring MVC 注解（`@GetMapping` 等） |
| `service` + `service_method` | `UserService` + `resetPassword` | 跳过 Controller，从 Service 开始追踪 |

自动追踪失败时，可以补充提示字段：`mapper` + `mapper_methods`，或 `repository` + `repository_methods`。JSON 输入仍然支持（语义相同）。

工具会自动追踪 `Controller → Service → Mapper/Repository → SQL`（类型名以 Controller/Service/Mapper/Repository/Dao/Manager/Jdbc 结尾的会被继续追踪）。追踪失败时，`trace.notes` 会说明缺什么（类找不到 / 方法找不到 / 端点不匹配），据此补充提示字段即可。

## 输出（YAML 报告）

```yaml
mode: interface-trace
project: /path/to/repo
interfaces_count: 2
summary:
  interfaces_total: 2
  interfaces_with_crud: 2
  crud_total: 4
  crud_by_operation:
    SELECT: 2
    INSERT: 1
    UPDATE: 0
    DELETE: 1
results:
  - id: create-user
    http_method: POST
    path: /api/users
    entry:
      class: UserController
      method: createUser
    call_chain:
      - UserController.createUser
      - UserService.createUser
      - UserMapper.insertUser
    crud_operations:
      - source: mybatis-annotation
        file: src/main/java/com/example/mapper/UserMapper.java
        owner: UserMapper
        method: insertUser
        operation: INSERT
        table: t_user
        tables: [t_user]
        params: [name, age]
        sql: INSERT INTO t_user (name, age) VALUES (#{name}, #{age})
    trace:
      status: ok
      notes: []
```

CRUD 记录字段：`source`（mybatis-xml / mybatis-annotation / mybatis-provider / jpa-repository / spring-jdbc）、`file`、`owner`（mapper/repository/service 类）、`method`、`operation`、`entity`、`table`、`tables`、`params`、`sql`。

`trace.status` 取值：`ok`（找到操作）、`partial`（找到操作但有提示）、`no_crud_found`（追踪成功但未匹配到任何操作）、`not_found`（入口缺失）。

## 支持的 SQL 来源

- MyBatis XML：`*Mapper.xml` 文件，根节点为 `<mapper>`；捕获 id、namespace、resultType，并把 `<if>`/`<where>` 子节点展开进 SQL 文本。
- MyBatis 注解：`@Select/@Insert/@Update/@Delete`（支持字符串拼接 SQL）；`@*Provider` 变体只上报，不带 SQL 文本。
- Spring Data JPA：派生查询方法名（`findBy…`、`deleteBy…`、`save` 等）和 `@Query` SQL；实体表名通过 `@Table(name=…)` 解析，缺省回退为实体名转下划线。
- Spring JDBC：`JdbcTemplate` / `NamedParameterJdbcTemplate` 调用中 SQL 字符串包含 CRUD 关键字的语句。

结果不完整或需要新增提取模式时，先读 [references/patterns.md](references/patterns.md) 了解模式细节、示例和边界情况。

## 结果不完整时怎么办

- 先看 `trace.notes`：它会明确指出缺失的类或方法。
- 在接口 YAML 中补充 `service`/`mapper`/`repository` 提示字段，而不是降低预期。
- Provider 动态 SQL（`@SelectProvider`、XML `<script>`）：`sql` 为 null，需要时手动到 Provider 方法里找 SQL 文本。
- 动态拼接的 SQL（字符串拼接、`StringBuilder`）：无法捕获，属于尽力而为。
