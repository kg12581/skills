#!/usr/bin/env python3
"""离线提取 Java Spring 项目中指定接口的 SQL CRUD 操作。

用法：
    python3 extract.py <代码仓路径>                               # 全量导出所有 CRUD 操作
    python3 extract.py <代码仓路径> <接口清单.yaml|json>          # 按接口清单追踪 CRUD 操作
    python3 extract.py <代码仓路径> --interfaces-json '{...}'
    python3 extract.py <代码仓路径> --auto                        # 自动发现所有 Controller 端点

选项：
    --out <文件>      把结果写入文件（默认 YAML）
    --format yaml|json  输出格式（默认 yaml）
    --no-sql          结果中隐藏原始 SQL 文本
    --depth <n>       最大调用链深度（默认 4）
    --auto            自动发现所有 Controller 端点（无需接口清单）

支持的 SQL 来源：
    mybatis-xml, mybatis-annotation, mybatis-provider,
    jpa-repository, spring-jdbc
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

CRUD_OPS = ("SELECT", "INSERT", "UPDATE", "DELETE")
SKIP_DIRS = {".git", ".idea", ".gradle", "target", "build", "node_modules"}
TRACEABLE_SUFFIXES = ("Controller", "Service", "Mapper", "Repository", "Dao", "Manager", "Jdbc")


# ---------- 小工具函数 ----------

def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def snake_case(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]", "_", name)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().strip("_")


def detect_tables(sql: str, operation: str) -> list[str]:
    if not sql:
        return []
    patterns = {
        "INSERT": r"\bINSERT\s+(?:INTO\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        "UPDATE": r"\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)",
        "DELETE": r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)",
        "SELECT": r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)",
    }
    tables: list[str] = []
    for pat in (patterns.get(operation), r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)"):
        if pat:
            tables.extend(re.findall(pat, sql, flags=re.IGNORECASE))
    skip = {"select", "from", "where", "join", "dual"}
    return list(dict.fromkeys(t for t in tables if t.lower() not in skip))


def extract_params(sql: str) -> list[str]:
    if not sql:
        return []
    named: list[str] = []
    for a, b, c in re.findall(r"#\{(\w+)\}|\$\{(\w+)\}|(?<!:):(\w+)", sql):
        named.append(a or b or c)
    named = list(dict.fromkeys(named))
    positional = [f"?{i}" for i in range(1, sql.count("?") + 1)]
    return named + positional


def join_java_strings(text: str) -> str:
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text))


def first_sql_literal(content: str) -> str:
    """取内容中第一段 Java 字符串字面量链（'a' + 'b' + ...）。"""
    m = re.match(
        r'\s*("(?:[^"\\]|\\.)*"\s*(?:\+\s*"(?:[^"\\]|\\.)*"\s*)*)',
        content,
    )
    if not m:
        return ""
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def extract_balanced(text: str, start: int) -> tuple[str, int]:
    """返回从 `start` 开始的括号配对内容 (内容, 结束下标)。"""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return text[start + 1 :], len(text)


def extract_block(text: str, start: int) -> tuple[str, int]:
    """返回从 `start` 开始的花括号配对内容 (内容, 结束下标)。"""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return text[start + 1 :], len(text)


def next_method_name(text: str, pos: int) -> str:
    """取 pos 之后第一个后跟 '(' 的标识符，跳过注解名。"""
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", text[pos:]):
        abs_start = pos + m.start()
        prev = text[abs_start - 1] if abs_start > 0 else ""
        if prev not in "@._" and not prev.isalnum():
            return m.group(1)
    return ""


def sql_operation(sql: str) -> str | None:
    if not sql:
        return None
    m = re.match(r"\s*(select|insert|update|delete|merge|replace)", sql, re.IGNORECASE)
    if not m:
        return None
    op = m.group(1).upper()
    return "INSERT" if op in ("REPLACE", "MERGE") else op


def split_top_level(s: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def is_traceable_type(name: str) -> bool:
    return any(name.endswith(s) for s in TRACEABLE_SUFFIXES)


def is_leaf_type(name: str) -> bool:
    return name.endswith(("Mapper", "Repository", "Dao"))


# ---------- 接口清单解析（YAML 或 JSON） ----------

def parse_yaml(text: str):
    if _yaml is None:
        raise RuntimeError(
            "读取 YAML 接口清单需要 PyYAML（pip install pyyaml）"
        )
    return _yaml.safe_load(text)


def load_interface_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        data = raw
    else:
        text = raw if isinstance(raw, str) else ""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = parse_yaml(text)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError(f"接口清单必须是映射或列表，当前是 {type(data).__name__}")
    items = data.get("interfaces")
    if items is None:
        items = (
            [data]
            if any(
                data.get(k)
                for k in (
                    "controller",
                    "service",
                    "mapper",
                    "repository",
                    "method_name",
                    "path",
                )
            )
            else []
        )
    return items or []


# ---------- Spring MVC 端点发现 ----------

MAPPING_VERBS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def join_paths(base: str, tail: str) -> str:
    if not tail:
        return (base or "/").rstrip("/") or "/"
    if not base:
        return tail.rstrip("/") or "/"
    return (base.rstrip("/") + "/" + tail.lstrip("/")).rstrip("/") or "/"


def _path_from_annotation(inner: str) -> str:
    pm = re.search(r"(?:value|path)\s*=\s*[\"']([^\"']+)[\"']", inner)
    if not pm:
        pm = re.search(r"[\"'](/[^\"']*)[\"']", inner)
    return pm.group(1) if pm else ""


def build_endpoints(java_texts) -> dict[tuple, list[tuple[str, str]]]:
    """从 Spring MVC 注解构建映射：HTTP方法+路径 -> [(类, 方法)]。"""
    endpoints: dict[tuple, list[tuple[str, str]]] = {}
    for _path, rel, text in java_texts:
        cls_m = re.search(r"\b(?:class|interface)\s+(\w+)", text)
        if not cls_m:
            continue
        class_name = cls_m.group(1)
        class_start = cls_m.start()
        base = ""
        cm = re.search(
            r"@RequestMapping\s*\(([^)]*)\)",
            text[max(0, class_start - 3000) : class_start],
        )
        if cm:
            base = _path_from_annotation(cm.group(1))
        prev_end = 0
        for mm in METHOD_RE.finditer(text):
            block_start = prev_end
            block = text[block_start : mm.start(1)]
            brace = text.find("{", mm.end())
            prev_end = extract_block(text, brace)[1] if brace != -1 else len(text)
            method_name = mm.group(1)
            if method_name == class_name:
                continue
            http = None
            tail = ""
            for ann, verb in MAPPING_VERBS.items():
                am = re.search(r"@" + ann + r"\b(?:\s*\(([^)]*)\))?", block)
                if am:
                    http = verb
                    tail = _path_from_annotation(am.group(1) or "")
                    break
            if http is None:
                rm = re.search(r"@RequestMapping\s*\(([^)]*)\)", block)
                if rm and block_start + rm.start() >= class_start:
                    inner = rm.group(1)
                    mm_m = re.search(r"method\s*=\s*(?:RequestMethod\.)?(\w+)", inner)
                    if mm_m:
                        http = mm_m.group(1).upper()
                    tail = _path_from_annotation(inner)
            full = join_paths(base, tail)
            if http:
                endpoints.setdefault((http, full), []).append((class_name, method_name))
            endpoints.setdefault((None, full), []).append((class_name, method_name))
    return endpoints


def resolve_endpoint(
    http: str | None, path: str, endpoints: dict
) -> list[tuple[str, str]]:
    path = (path or "/").rstrip("/") or "/"
    for key in ((http.upper(), path) if http else (None,), (None, path)):
        hits = endpoints.get(key)
        if hits:
            return list(hits)
    return []


def find_methods_by_name(name: str, classes: dict) -> list[str]:
    hits = [c for c, info in classes.items() if name in info["methods"]]

    def rank(c: str) -> int:
        if c.endswith("Controller"):
            return 0
        if c.endswith("Service"):
            return 1
        if is_leaf_type(c):
            return 2
        return 3

    return sorted(hits, key=rank)


def auto_discover_items(endpoints: dict) -> list[dict]:
    items: list[dict] = []
    seen: set[tuple] = set()
    for (http, path), hits in sorted(
        endpoints.items(), key=lambda kv: (kv[0][1], kv[0][0] or "")
    ):
        if http is None:
            continue
        for cls_name, method in hits:
            key = (http, path, cls_name, method)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "id": f"{http} {path} ({cls_name}.{method})",
                    "http_method": http,
                    "path": path,
                    "controller": cls_name,
                    "controller_method": method,
                }
            )
    return items


# ---------- 项目文件发现 ----------

def iter_project_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix == ".java" or (
                p.suffix == ".xml" and fn.lower().endswith("mapper.xml")
            ):
                yield p, p.relative_to(root).as_posix()


# ---------- CRUD 提取器 ----------

def extract_mybatis_xml(path: Path, rel: str) -> list[dict]:
    ops: list[dict] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return ops
    if localname(root.tag) != "mapper":
        return ops
    ns = root.attrib.get("namespace", "") or None
    owner = ns.rsplit(".", 1)[-1] if ns else Path(rel).stem
    for el in root:
        tag = localname(el.tag)
        if tag not in ("select", "insert", "update", "delete"):
            continue
        method = el.attrib.get("id", "")
        sql = " ".join("".join(el.itertext()).split())
        entity = el.attrib.get("resultType", "").rsplit(".", 1)[-1] or None
        tables = detect_tables(sql, tag.upper())
        ops.append(
            {
                "source": "mybatis-xml",
                "file": rel,
                "owner": owner,
                "namespace": ns,
                "method": method,
                "operation": tag.upper(),
                "entity": entity,
                "table": tables[0] if tables else None,
                "tables": tables,
                "params": extract_params(sql),
                "sql": sql or None,
            }
        )
    return ops


MYBATIS_ANN = {
    "Select": "SELECT",
    "Insert": "INSERT",
    "Update": "UPDATE",
    "Delete": "DELETE",
}
MYBATIS_PROVIDER = {
    "SelectProvider": "SELECT",
    "InsertProvider": "INSERT",
    "UpdateProvider": "UPDATE",
    "DeleteProvider": "DELETE",
}


def extract_mybatis_annotations(text: str, rel: str) -> list[dict]:
    ops: list[dict] = []
    owner = Path(rel).stem
    pattern = (
        r"@(Select|Insert|Update|Delete|"
        r"SelectProvider|InsertProvider|UpdateProvider|DeleteProvider)\s*\("
    )
    for m in re.finditer(pattern, text):
        content, end = extract_balanced(text, m.end() - 1)
        ann = m.group(1)
        method = next_method_name(text, end)
        if ann in MYBATIS_PROVIDER:
            ops.append(
                {
                    "source": "mybatis-provider",
                    "file": rel,
                    "owner": owner,
                    "namespace": None,
                    "method": method,
                    "operation": MYBATIS_PROVIDER[ann],
                    "entity": None,
                    "table": None,
                    "tables": [],
                    "params": [],
                    "sql": None,
                }
            )
            continue
        sql = " ".join(join_java_strings(content).split())
        tables = detect_tables(sql, MYBATIS_ANN[ann])
        ops.append(
            {
                "source": "mybatis-annotation",
                "file": rel,
                "owner": owner,
                "namespace": None,
                "method": method,
                "operation": MYBATIS_ANN[ann],
                "entity": None,
                "table": tables[0] if tables else None,
                "tables": tables,
                "params": extract_params(sql),
                "sql": sql or None,
            }
        )
    return ops


JPA_REPO_RE = re.compile(
    r"\b(?:interface|class)\s+(\w+)[^{;]*?\b"
    r"(?:CrudRepository|JpaRepository|PagingAndSortingRepository)\s*<([^>]+)>"
)


def build_entity_table_map(java_texts) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for _path, _rel, text in java_texts:
        for tm in re.finditer(r'@Table\s*\(\s*name\s*=\s*"([^"]+)"', text):
            cm = re.search(
                r"\b(?:class|interface|record)\s+(\w+)",
                text[tm.end() : tm.end() + 1000],
            )
            if cm:
                mapping.setdefault(cm.group(1), tm.group(1))
    return mapping


def classify_jpa_method(name: str) -> str | None:
    lower = name.lower()
    if re.match(r"^(delete|remove|purge)", lower):
        return "DELETE"
    if re.match(r"^(update|modify|touch)", lower):
        return "UPDATE"
    if re.match(r"^(save|insert|persist|create|add|store)", lower):
        return "INSERT"
    if re.match(r"^(find|get|read|query|search|count|exists|select|list|all)", lower):
        return "SELECT"
    return None


INHERITED_JPA_METHODS: dict[str, tuple[str, list[str]]] = {
    "save": ("INSERT", ["entity"]),
    "saveAll": ("INSERT", ["entities"]),
    "insert": ("INSERT", ["entity"]),
    "findById": ("SELECT", ["id"]),
    "findAll": ("SELECT", []),
    "findAllById": ("SELECT", ["ids"]),
    "existsById": ("SELECT", ["id"]),
    "count": ("SELECT", []),
    "getById": ("SELECT", ["id"]),
    "getReferenceById": ("SELECT", ["id"]),
    "deleteById": ("DELETE", ["id"]),
    "delete": ("DELETE", ["entity"]),
    "deleteAll": ("DELETE", []),
    "deleteAllById": ("DELETE", ["ids"]),
}


def extract_jpa_repositories(
    text: str, rel: str, entity_tables: dict[str, str]
) -> list[dict]:
    ops: list[dict] = []
    for m in JPA_REPO_RE.finditer(text):
        entity = m.group(2).split(",")[0].strip().rsplit(".", 1)[-1]
        table = entity_tables.get(entity) or snake_case(entity)
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        block, _ = extract_block(text, brace)
        query_methods: set[str] = set()
        for qm in re.finditer(r"@Query\s*\(", block):
            content, qend = extract_balanced(block, qm.end() - 1)
            sql = " ".join(first_sql_literal(content).split())
            method = next_method_name(block, qend)
            query_methods.add(method)
            op = sql_operation(sql) or "SELECT"
            tables = detect_tables(sql, op)
            first_table = tables[0] if tables else None
            if first_table:
                display_table = entity_tables.get(first_table) or (
                    table if first_table == entity else first_table
                )
            else:
                display_table = table
            ops.append(
                {
                    "source": "jpa-repository",
                    "file": rel,
                    "owner": m.group(1),
                    "namespace": m.group(1),
                    "method": method,
                    "operation": op,
                    "entity": entity,
                    "table": display_table,
                    "tables": tables or [table],
                    "params": extract_params(sql),
                    "sql": sql or None,
                }
            )
        for mm in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*;", block):
            method = mm.group(1)
            if method in query_methods:
                continue
            op = classify_jpa_method(method)
            if not op:
                continue
            params = []
            for p in mm.group(2).split(","):
                pm = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", p.strip())
                if pm:
                    params.append(pm.group(1))
            ops.append(
                {
                    "source": "jpa-repository",
                    "file": rel,
                    "owner": m.group(1),
                    "namespace": m.group(1),
                    "method": method,
                    "operation": op,
                    "entity": entity,
                    "table": table,
                    "tables": [table],
                    "params": params,
                    "sql": None,
                }
            )
        for name, (op, params) in INHERITED_JPA_METHODS.items():
            ops.append(
                {
                    "source": "jpa-repository",
                    "file": rel,
                    "owner": m.group(1),
                    "namespace": m.group(1),
                    "method": name,
                    "operation": op,
                    "entity": entity,
                    "table": table,
                    "tables": [table],
                    "params": list(params),
                    "sql": None,
                }
            )
    return ops


JDBC_CALL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
    r"(queryForList|queryForObject|queryForMap|queryForRowSet|queryForStream|"
    r"query|update|batchUpdate|execute)\s*\(",
    re.IGNORECASE,
)


def extract_jdbc(text: str, rel: str) -> list[dict]:
    ops: list[dict] = []
    owner = Path(rel).stem
    for m in JDBC_CALL_RE.finditer(text):
        if "jdbc" not in m.group(1).lower():
            continue
        content, _ = extract_balanced(text, m.end() - 1)
        sql = " ".join(first_sql_literal(content).split())
        op = sql_operation(sql)
        if not op:
            continue
        method = enclosing_method(text, m.start())
        tables = detect_tables(sql, op)
        ops.append(
            {
                "source": "spring-jdbc",
                "file": rel,
                "owner": owner,
                "namespace": None,
                "method": method,
                "operation": op,
                "entity": None,
                "table": tables[0] if tables else None,
                "tables": tables,
                "params": extract_params(sql),
                "sql": sql,
            }
        )
    return ops


def enclosing_method(text: str, pos: int) -> str:
    """尽力推断包含字节位置 `pos` 的 Java 方法名。"""
    head = text[:pos]
    pat = re.compile(
        r"(?m)^\s*(?:public|protected|private|static|final|synchronized|default|@Override\s+)*"
        r"(?:[A-Za-z_][\w<>,.\[\]\s]*?\s+)*(\w+)\s*\("
    )
    for mm in reversed(list(pat.finditer(head))):
        if mm.start() == 0 or head[mm.start() - 1].isspace():
            return mm.group(1)
    return ""


# ---------- Java 类骨架（用于接口追踪） ----------

FIELD_RE = re.compile(
    r"(?m)^\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*"
    r"(?:private|protected|public)\s+(?:static\s+|final\s+)*"
    r"([A-Za-z_][\w<>\[\].]*)\s+([A-Za-z_]\w*)\s*(?:=|;)"
)

METHOD_RE = re.compile(
    r"(?m)^\s*(?:(?:public|protected|private|static|final|synchronized|default|abstract)\s+|"
    r"@\w+(?:\s*\([^)]*\))?\s*)*"
    r"(?:[A-Za-z_][\w<>,.\[\]?]*\s+)+(\w+)\s*\(([^;{}]*?)\)\s*"
    r"(?:throws\s+[\w.,\s]+)?\s*(?=\{)"
)

CONSTRUCTOR_RE = re.compile(
    r"(?m)^\s*(?:public|protected|private)?\s*([A-Za-z_]\w*)\s*\(\s*([^;{}]*?)\)\s*\{"
)


def parse_params(params_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for p in split_top_level(params_str):
        p = p.strip()
        if not p:
            continue
        m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", p)
        if m:
            type_part = p[: m.start()].strip()
            params[m.group(1)] = type_part.split("<")[0].strip() or m.group(1)
    return params


def parse_java_file(text: str, rel: str) -> dict:
    info = {
        "file": rel,
        "package": "",
        "class": None,
        "fields": {},
        "params": {},
        "methods": {},
    }
    pm = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.MULTILINE)
    if pm:
        info["package"] = pm.group(1)
    cm = re.search(r"\b(?:class|interface|record)\s+(\w+)", text)
    if cm:
        info["class"] = cm.group(1)
    for fm in FIELD_RE.finditer(text):
        info["fields"][fm.group(2)] = fm.group(1).split("<")[0].strip()
    for ctor in CONSTRUCTOR_RE.finditer(text):
        if ctor.group(1) == info["class"]:
            info["params"].update(parse_params(ctor.group(2)))
    for mm in METHOD_RE.finditer(text):
        name = mm.group(1)
        if name == info["class"]:
            continue
        brace = text.find("{", mm.end())
        if brace == -1:
            continue
        body, _ = extract_block(text, brace)
        info["methods"][name] = {
            "params": parse_params(mm.group(2)),
            "body": body,
        }
    return info


CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")


def find_calls(body: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in CALL_RE.finditer(body)]


def resolve_var(var: str, cls: dict, method_params: dict) -> str | None:
    if var == "this":
        return cls["class"]
    if var in cls["fields"]:
        return cls["fields"][var]
    if var in method_params:
        return method_params[var]
    if var in cls["params"]:
        return cls["params"][var]
    return None


def match_crud(owner: str, method: str, ops: list[dict]) -> list[dict]:
    return [op for op in ops if op["owner"] == owner and op["method"] == method]


def dedupe_chain(chain: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in chain:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def trace_interface(
    entry_class: str,
    entry_method: str,
    classes: dict,
    ops: list[dict],
    max_depth: int,
) -> tuple[list[str], list[dict], list[str]]:
    notes: list[str] = []
    found: list[dict] = []
    chain: list[str] = []
    visited: set[tuple[str, str]] = set()

    def visit(type_name: str, method: str, depth: int):
        if depth <= 0:
            notes.append(f"追踪深度已达上限：{type_name}.{method}")
            return
        key = (type_name, method)
        if key in visited:
            return
        visited.add(key)
        chain.append(f"{type_name}.{method}")
        direct = match_crud(type_name, method, ops)
        if direct:
            found.extend(direct)
            return
        cls = classes.get(type_name)
        if cls is None:
            notes.append(f"未找到类：{type_name}")
            return
        minfo = cls["methods"].get(method)
        if minfo is None:
            if is_leaf_type(type_name):
                return
            notes.append(f"方法不存在或无方法体：{type_name}.{method}")
            return
        for var, called in find_calls(minfo["body"]):
            target = resolve_var(var, cls, minfo["params"])
            if not target or target not in classes:
                continue
            if var != "this" and not is_traceable_type(target):
                continue
            visit(target, called, depth - 1)

    visit(entry_class, entry_method, max_depth)
    return dedupe_chain(chain), dedupe_ops(found), notes


def resolve_class_name(name: str, classes: dict) -> str | None:
    if not name:
        return None
    simple = name.rsplit(".", 1)[-1]
    return simple if simple in classes else None


def trace_interfaces(
    interface_info,
    classes: dict,
    ops: list[dict],
    endpoints: dict,
    max_depth: int = 4,
) -> list[dict]:
    results: list[dict] = []
    for item in load_interface_items(interface_info):
        notes: list[str] = []
        found: list[dict] = []
        chain: list[str] = []
        entries: list[tuple[str, str]] = []

        entry_class = (
            item.get("controller")
            or item.get("class")
            or item.get("service")
            or item.get("mapper")
            or item.get("repository")
        )
        entry_method = (
            item.get("controller_method")
            or item.get("method")
            or item.get("service_method")
        )
        if entry_class and entry_method:
            entries.append(
                (resolve_class_name(entry_class, classes) or entry_class, entry_method)
            )
        elif entry_method and not entry_class:
            hits = find_methods_by_name(entry_method, classes)
            if not hits:
                notes.append(f"所有类中都未找到方法：{entry_method}")
            else:
                entries.extend((h, entry_method) for h in hits)
        elif item.get("method_name"):
            hits = find_methods_by_name(item["method_name"], classes)
            if not hits:
                notes.append(f"所有类中都未找到方法：{item['method_name']}")
            else:
                entries.extend((h, item["method_name"]) for h in hits)
        elif item.get("path"):
            hits = resolve_endpoint(item.get("http_method"), item["path"], endpoints)
            if not hits:
                verb = item.get("http_method") or ""
                notes.append(f"没有匹配到端点：{verb} {item['path']}".strip())
            else:
                entries.extend(hits)
        else:
            notes.append(
                "无法识别的接口条目（请提供方法名、类.方法 或 HTTP 路径）"
            )

        for cls_name, method in entries:
            c, f, n = trace_interface(cls_name, method, classes, ops, max_depth)
            chain.extend(c)
            found.extend(f)
            notes.extend(n)

        for leaf_key in ("mapper", "repository"):
            leaf = item.get(leaf_key)
            if not leaf:
                continue
            leaf_class = resolve_class_name(leaf, classes) or leaf
            for lm in item.get(f"{leaf_key}_methods", []) or []:
                hits = match_crud(leaf_class, lm, ops)
                if hits:
                    found.extend(hits)
                    chain.append(f"{leaf_class}.{lm}")
                else:
                    notes.append(f"未匹配到 CRUD 操作：{leaf_class}.{lm}")

        found = dedupe_ops(found)
        if found and notes:
            status = "partial"
        elif found:
            status = "ok"
        elif notes and any(
            ("not found" in n) or ("missing" in n) or ("no CRUD operation matched" in n)
            for n in notes
        ):
            status = "not_found"
        else:
            status = "no_crud_found"
        results.append(
            {
                "id": item.get("id")
                or (
                    f"{entry_class}.{entry_method}"
                    if entry_class and entry_method
                    else item.get("method_name") or item.get("path") or "interface"
                ),
                "http_method": item.get("http_method"),
                "path": item.get("path"),
                "entry": (
                    {"class": entry_class, "method": entry_method}
                    if entry_class and entry_method
                    else None
                ),
                "call_chain": dedupe_chain(chain),
                "crud_operations": found,
                "trace": {"status": status, "notes": notes},
            }
        )
    return results


def dedupe_ops(ops: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for op in ops:
        key = (op["source"], op["file"], op["owner"], op["method"], op["sql"])
        if key in seen:
            continue
        seen.add(key)
        out.append(op)
    return out


def summarize(ops: list[dict]) -> dict:
    summary = {
        "crud_total": len(ops),
        "crud_by_operation": {op: 0 for op in CRUD_OPS},
        "crud_by_source": {},
    }
    for op in ops:
        summary["crud_by_operation"][op["operation"]] += 1
        summary["crud_by_source"][op["source"]] = (
            summary["crud_by_source"].get(op["source"], 0) + 1
        )
    return summary


# ---------- 编排 ----------

def analyze(repo, interface_info=None, max_depth: int = 4, auto: bool = False) -> dict:
    root = Path(repo).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"项目路径不存在：{root}")
    files = list(iter_project_files(root))
    java_texts: list[tuple] = []
    ops: list[dict] = []
    for path, rel in files:
        if path.suffix == ".java":
            java_texts.append(
                (path, rel, path.read_text(encoding="utf-8", errors="replace"))
            )
        else:
            ops.extend(extract_mybatis_xml(path, rel))
    entity_tables = build_entity_table_map(java_texts)
    for _path, rel, text in java_texts:
        ops.extend(extract_mybatis_annotations(text, rel))
        ops.extend(extract_jpa_repositories(text, rel, entity_tables))
        ops.extend(extract_jdbc(text, rel))
    ops = dedupe_ops(ops)

    classes: dict[str, dict] = {}
    endpoints: dict[tuple, list[tuple[str, str]]] = {}
    if interface_info is not None or auto:
        for _path, rel, text in java_texts:
            info = parse_java_file(text, rel)
            if info["class"]:
                classes[info["class"]] = info
        endpoints = build_endpoints(java_texts)

    if interface_info is None and not auto:
        return {
            "mode": "full-extract",
            "project": str(root),
            "files_scanned": len(files),
            "summary": summarize(ops),
            "crud_operations": ops,
        }

    if auto:
        interface_info = {"interfaces": auto_discover_items(endpoints)}
    results = trace_interfaces(interface_info, classes, ops, endpoints, max_depth)
    matched = [r for r in results if r["crud_operations"]]
    summary = {
        "interfaces_total": len(results),
        "interfaces_with_crud": len(matched),
    }
    summary.update(summarize([op for r in results for op in r["crud_operations"]]))
    return {
        "mode": "interface-trace",
        "project": str(root),
        "files_scanned": len(files),
        "interfaces_count": len(results),
        "summary": summary,
        "results": results,
    }


def strip_sql(ops: list[dict]) -> None:
    for op in ops:
        op["sql"] = None


def dump_result(result: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    if _yaml is None:
        raise RuntimeError("输出 YAML 需要 PyYAML（pip install pyyaml）")
    return _yaml.safe_dump(
        result,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="离线提取 Java Spring 项目的 SQL CRUD 操作。"
    )
    ap.add_argument("repo", help="Java Spring 代码仓路径")
    ap.add_argument(
        "interfaces",
        nargs="?",
        help="描述待追踪接口的 YAML 或 JSON 文件",
    )
    ap.add_argument(
        "--interfaces-json",
        dest="inline",
        help="以内联 JSON 描述待追踪的接口",
    )
    ap.add_argument("--out", help="把结果写入该文件（默认 YAML）")
    ap.add_argument("--no-sql", action="store_true", help="结果中隐藏原始 SQL 文本")
    ap.add_argument("--depth", type=int, default=4, help="最大调用链深度（默认 4）")
    ap.add_argument(
        "--format",
        choices=("yaml", "json"),
        default="yaml",
        help="输出格式（默认 yaml）",
    )
    ap.add_argument(
        "--auto",
        action="store_true",
        help="自动发现所有 Controller 端点并逐一提取（无需接口清单）",
    )
    args = ap.parse_args(argv)

    if args.auto and (args.interfaces or args.inline):
        ap.error("--auto 不能与接口清单同时使用")
    interface_raw = args.inline or (Path(args.interfaces).read_text(encoding="utf-8") if args.interfaces else None)
    result = analyze(args.repo, interface_raw, max_depth=args.depth, auto=args.auto)
    if args.no_sql:
        if result.get("crud_operations") is not None:
            strip_sql(result["crud_operations"])
        for r in result.get("results", []):
            strip_sql(r["crud_operations"])
    output = dump_result(result, args.format)
    if args.out:
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
