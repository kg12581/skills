---
name: spring-crud-extractor
description: 给定 Java Spring 代码仓和 YAML 接口清单（controller/service/mapper/repository 类名 + 方法名），离线解析每个接口对应的 SQL CRUD 操作（MyBatis XML/注解、Spring Data JPA、JdbcTemplate），输出 YAML 报告（调用链与 SQL 明细）。当用户要求根据接口提取或分析 SQL 增删改查、梳理某个 API 背后的数据操作、或提供接口信息让我定位对应 CRUD SQL 时使用。Use when given a Java Spring repository plus a YAML interface list to extract the SQL CRUD operations behind each interface and write a YAML report (offline, no server).
---

# Spring SQL CRUD Extractor

## Overview

Offline tool: input a Java Spring repository and an interface list (YAML or JSON), output each interface's call chain and SQL CRUD operations (SELECT / INSERT / UPDATE / DELETE) from MyBatis XML/annotations, Spring Data JPA repositories, and JdbcTemplate — as a YAML report. No server or network is needed.

## Quick start

Trace the interfaces listed in `interfaces.yaml`:

```bash
spring-crud-extractor /path/to/repo interfaces.yaml
```

`spring-crud-extractor` is a symlink to this skill's `scripts/extract.py`, so you can also run it directly from this directory (`python3 scripts/extract.py …`).

No interface list needed: auto-discover every controller endpoint and trace it:

```bash
spring-crud-extractor /path/to/repo --auto
```

Or dump every CRUD operation in the repo:

```bash
spring-crud-extractor /path/to/repo
```

Options: `--out report.yaml` writes the result to a YAML file (default format is YAML), `--format json` switches to JSON, `--no-sql` hides raw SQL text, `--depth 5` raises the call-chain depth (default 4), and `--interfaces-json '{...}'` passes the interface list inline.

## What interface info to provide

YAML interface list — each item is a mapping, and the less you write the more the tool searches for you:

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

Fields:

| Field | Example | How it is resolved |
| --- | --- | --- |
| `controller` + `controller_method` | `UserController` + `createUser` | Direct entry point (recommended) |
| `controller_method` alone | `findUser` | Searched in all Controller/Service classes |
| `method_name` alone | `deleteUser` | Same, alternative field |
| `http_method` + `path` | `POST` + `/api/users` | Matched against Spring MVC annotations (`@GetMapping` etc.) |
| `service` + `service_method` | `UserService` + `resetPassword` | Skip the controller, start from the service |

Optional hint fields when automatic tracing misses a step: `mapper` + `mapper_methods`, or `repository` + `repository_methods`. JSON input is still supported (same semantics).

The tool auto-traces `Controller → Service → Mapper/Repository → SQL` (types ending in Controller/Service/Mapper/Repository/Dao/Manager/Jdbc are followed). When tracing fails, `trace.notes` explains what is missing (class not found / method not found / endpoint not matched), and you can add hint fields to bridge the gap.

## Output (YAML report)

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

CRUD record fields: `source` (`mybatis-xml` / `mybatis-annotation` / `mybatis-provider` / `jpa-repository` / `spring-jdbc`), `file`, `owner` (mapper/repository/service class), `method`, `operation`, `entity`, `table`, `tables`, `params`, `sql`.

`trace.status` values: `ok` (ops found), `partial` (ops found but with notes), `no_crud_found` (traced but nothing matched), `not_found` (entry missing).

## Supported sources

- MyBatis XML: `*Mapper.xml` files with `<mapper>` root; captures id, namespace, resultType, and flattens `<if>`/`<where>` children into SQL text.
- MyBatis annotations: `@Select/@Insert/@Update/@Delete` with string-concatenated SQL; `@*Provider` variants are reported without SQL text.
- Spring Data JPA: derived query names (`findBy…`, `deleteBy…`, `save`, …) and `@Query` SQL; entity tables resolved from `@Table(name=…)` with snake_case fallback.
- Spring JDBC: `JdbcTemplate` / `NamedParameterJdbcTemplate` calls whose SQL string contains a CRUD keyword.

Detailed patterns, examples, and edge cases: read [references/patterns.md](references/patterns.md) when results are incomplete or when adding a new extraction pattern.

## When results are incomplete

- Read `trace.notes` first: it names the exact missing class/method.
- Add `service`/`mapper`/`repository` hint fields to the interface YAML instead of lowering expectations.
- Provider-based dynamic SQL (`@SelectProvider`, XML `<script>`): `sql` is `null`; locate the provider method manually if the SQL text is needed.
- Dynamically built SQL (string concatenation, `StringBuilder`): not captured; treat extraction as best-effort.
