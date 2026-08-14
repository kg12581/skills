#!/usr/bin/env python3
"""候选收集器：粗扫 Java Spring 项目中的 SQL 候选与端点，最终分析由文字流程完成。

用法：
    python3 extract.py <代码仓路径> <接口.json> [--out report.yaml]
    python3 extract.py <代码仓路径> --auto [--out report.yaml]
    python3 extract.py <代码仓路径>                 # 全量导出 SQL 候选

输入 JSON：{"apis": [{"api_url": "...", "method": "POST", "headers": {...}, "body": {...}}]}
输出 YAML：每个接口包含 setup（INSERT/UPDATE）与 teardown（DELETE + 自动 DELETE FROM）。

说明：脚本只做粗扫（SQL 候选收集 + 端点匹配 + 方法名粗链），JPA 派生方法、
MyBatis-Plus、Provider、动态 SQL 等由框架生成的 SQL 请按 SKILL.md 的文字流程人工补全。
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

SKIP_DIRS = {".git", ".idea", ".gradle", "target", "build", "node_modules"}
TRACEABLE = ("Controller", "Service", "Mapper", "Repository", "Dao", "Manager", "Jdbc")
CRUD_OPS = ("SELECT", "INSERT", "UPDATE", "DELETE")


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def snake_case(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]", "_", name)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().strip("_")


def is_traceable(name: str) -> bool:
    return any(name.endswith(s) for s in TRACEABLE)


def sql_operation(sql: str) -> str | None:
    m = re.match(r"\s*(select|insert|update|delete|merge|replace)\b", sql or "", re.IGNORECASE)
    if not m:
        return None
    op = m.group(1).upper()
    return "INSERT" if op in ("REPLACE", "MERGE") else op


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
    return named + [f"?{i}" for i in range(1, sql.count("?") + 1)]


def extract_block(text: str, start: int) -> tuple[str, int]:
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return text[start + 1 :], len(text)


# ---------- 文件发现 ----------

def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix == ".java" or (p.suffix == ".xml" and fn.lower().endswith("mapper.xml")):
                yield p, p.relative_to(root).as_posix()


# ---------- SQL 候选收集 ----------

def xml_candidates(path: Path, rel: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return out
    if localname(root.tag) != "mapper":
        return out
    owner = (root.attrib.get("namespace") or Path(rel).stem).rsplit(".", 1)[-1]
    for el in root:
        tag = localname(el.tag)
        if tag not in ("select", "insert", "update", "delete"):
            continue
        sql = " ".join("".join(el.itertext()).split())
        tables = detect_tables(sql, tag.upper())
        entity = (el.attrib.get("resultType") or "").rsplit(".", 1)[-1] or None
        out.append({
            "source": "mybatis-xml", "file": rel, "owner": owner,
            "method": el.attrib.get("id", ""), "operation": tag.upper(),
            "entity": entity, "table": tables[0] if tables else None,
            "tables": tables, "params": extract_params(sql), "sql": sql or None,
        })
    return out


METHOD_RE = re.compile(
    r"(?m)^\s*(?:(?:public|protected|private|static|final|synchronized|default|abstract)\s+|"
    r"@\w+(?:\s*\([^)]*\))?\s*)*"
    r"(?:[A-Za-z_][\w<>,.\[\]?]*\s+)+(\w+)\s*\(([^;{}]*?)\)\s*"
    r"(?:throws\s+[\w.,\s]+)?\s*(?=\{)",
)
METHOD_STMT_RE = re.compile(
    r"(?m)^\s*(?:(?:public|protected|private|static|final|default|abstract)\s+|"
    r"@\w+(?:\s*\([^)]*\))?\s*)*"
    r"(?:[A-Za-z_][\w<>,.\[\]?]*\s+)+(\w+)\s*\([^;{}]*?\)\s*;",
)
SQL_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _in_annotation(text: str, pos: int) -> bool:
    head = text[max(0, pos - 300):pos]
    for m in re.finditer(r"@\w+\s*\(", head):
        if ";" not in head[m.end():]:
            return True
    return False


def _enclosing_method(text: str, pos: int, positions: list) -> str:
    prev = next((n for p, n in reversed(positions) if p < pos), "")
    nxt = next(((p, n) for p, n in positions if p > pos), (None, ""))
    if nxt[0] is not None and (nxt[0] - pos) < 300 and _in_annotation(text, pos):
        return nxt[1]
    return prev


def java_candidates(text: str, rel: str) -> list[dict]:
    out: list[dict] = []
    owner = Path(rel).stem
    positions = sorted(
        [(m.start(), m.group(1)) for m in METHOD_RE.finditer(text)]
        + [(m.start(), m.group(1)) for m in METHOD_STMT_RE.finditer(text)]
    )
    for m in SQL_STRING_RE.finditer(text):
        sql = " ".join(m.group(1).split())
        op = sql_operation(sql)
        if not op:
            continue
        method = _enclosing_method(text, m.start(), positions)
        tables = detect_tables(sql, op)
        out.append({
            "source": "java-sql", "file": rel, "owner": owner, "method": method,
            "operation": op, "entity": None, "table": tables[0] if tables else None,
            "tables": tables, "params": extract_params(sql), "sql": sql,
        })
    return out


# ---------- 类骨架（方法名粗链用） ----------

def parse_class(text: str) -> dict:
    cls = re.search(r"\b(?:class|interface|record)\s+(\w+)", text)
    declared = {m.group(1) for m in METHOD_RE.finditer(text)}
    declared |= {m.group(1) for m in METHOD_STMT_RE.finditer(text)}
    methods = []
    for mm in METHOD_RE.finditer(text):
        brace = text.find("{", mm.end())
        end = extract_block(text, brace)[1] if brace != -1 else len(text)
        methods.append((mm.start(), end, mm.group(1)))
    return {"class": cls.group(1) if cls else None, "declared": declared,
            "body": text, "methods": methods}


CALL_RE = re.compile(r"\b\w+\.([A-Za-z_]\w*)\s*\(")


def build_edges(classes: dict) -> list[tuple[str, str, str, str]]:
    """(源类, 源方法, 被调用方法名, 目标类)：不解析类型，靠方法名粗匹配。"""
    edges: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for cls, info in classes.items():
        for m in CALL_RE.finditer(info["body"]):
            callee = m.group(1)
            src_method = next((name for start, end, name in info["methods"]
                               if start <= m.start() < end), "")
            if not src_method:
                continue
            targets = [t for t, ti in classes.items()
                       if t != cls and callee in ti["declared"] and is_traceable(t)]
            if targets:
                for t in targets:
                    key = (cls, src_method, callee, t)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(key)
                continue
            if callee in info["declared"]:
                key = (cls, src_method, callee, cls)
                if key not in seen:
                    seen.add(key)
                    edges.append(key)
    return edges


def collect(
    entries: list[tuple[str, str]], classes: dict, edges: list[tuple],
    candidates: list[dict], max_depth: int,
) -> tuple[list[dict], list[str]]:
    found: list[dict] = []
    chain: list[str] = []
    visited: set[tuple[str, str]] = set()
    queue = [(c, m, 0) for c, m in entries]
    while queue:
        cls, method, depth = queue.pop(0)
        key = (cls, method)
        if key in visited or depth > max_depth:
            continue
        visited.add(key)
        chain.append(f"{cls}.{method}")
        for cand in candidates:
            if cand["owner"] == cls and cand["method"] == method:
                found.append(cand)
        for src, src_method, callee, tgt in edges:
            if src == cls and src_method == method:
                queue.append((tgt, callee, depth + 1))
    return dedupe(found), list(dict.fromkeys(chain))


def dedupe(ops: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for op in ops:
        key = (op["source"], op["file"], op["owner"], op["method"], op["sql"])
        if key not in seen:
            seen.add(key)
            out.append(op)
    return out


# ---------- Spring MVC 端点 ----------

MAPPING_VERBS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}


def join_paths(base: str, tail: str) -> str:
    if not tail:
        return (base or "/").rstrip("/") or "/"
    if not base:
        return tail.rstrip("/") or "/"
    return (base.rstrip("/") + "/" + tail.lstrip("/")).rstrip("/") or "/"


def path_from(inner: str) -> str:
    pm = re.search(r"(?:value|path)\s*=\s*[\"']([^\"']+)[\"']", inner)
    if not pm:
        pm = re.search(r"[\"'](/[^\"']*)[\"']", inner)
    return pm.group(1) if pm else ""


def build_endpoints(java_texts) -> dict[tuple, list[tuple[str, str]]]:
    endpoints: dict[tuple, list[tuple[str, str]]] = {}
    for _p, rel, text in java_texts:
        cls_m = re.search(r"\b(?:class|interface)\s+(\w+)", text)
        if not cls_m:
            continue
        class_name, class_start = cls_m.group(1), cls_m.start()
        cm = re.search(r"@RequestMapping\s*\(([^)]*)\)", text[max(0, class_start - 3000):class_start])
        base = path_from(cm.group(1)) if cm else ""
        prev_end = 0
        for mm in METHOD_RE.finditer(text):
            block_start, block = prev_end, text[prev_end:mm.start(1)]
            brace = text.find("{", mm.end())
            prev_end = extract_block(text, brace)[1] if brace != -1 else len(text)
            if mm.group(1) == class_name:
                continue
            http, tail = None, ""
            for ann, verb in MAPPING_VERBS.items():
                am = re.search(r"@" + ann + r"\b(?:\s*\(([^)]*)\))?", block)
                if am:
                    http, tail = verb, path_from(am.group(1) or "")
                    break
            if http is None:
                rm = re.search(r"@RequestMapping\s*\(([^)]*)\)", block)
                if rm and block_start + rm.start() >= class_start:
                    inner = rm.group(1)
                    mm_m = re.search(r"method\s*=\s*(?:RequestMethod\.)?(\w+)", inner)
                    http, tail = (mm_m.group(1).upper() if mm_m else None), path_from(inner)
            full = join_paths(base, tail)
            if http:
                endpoints.setdefault((http, full), []).append((class_name, mm.group(1)))
            endpoints.setdefault((None, full), []).append((class_name, mm.group(1)))
    return endpoints


def resolve_endpoint(http: str | None, path: str, endpoints: dict) -> list[tuple[str, str]]:
    path = (path or "/").rstrip("/") or "/"
    keys = [((http.upper(), path) if http else None), (None, path)]
    for key in keys:
        hits = endpoints.get(key)
        if hits:
            return list(hits)
    return []


def normalize_api_url(url: str) -> str:
    url = (url or "").strip()
    m = re.match(r"https?://[^/]+(/.*)?$", url, re.IGNORECASE)
    if m:
        url = m.group(1) or "/"
    return url.rstrip("/") or "/"


def auto_discover_items(endpoints: dict) -> list[dict]:
    items, seen = [], set()
    for (http, path), hits in sorted(endpoints.items(), key=lambda kv: (kv[0][1], kv[0][0] or "")):
        if http is None:
            continue
        for cls_name, method in hits:
            key = (http, path, cls_name, method)
            if key in seen:
                continue
            seen.add(key)
            items.append({"id": f"{http} {path} ({cls_name}.{method})", "api_url": path,
                          "method": http, "headers": {}, "body": {},
                          "controller": cls_name, "controller_method": method})
    return items


# ---------- setup / teardown ----------

def build_setup_teardown(ops: list[dict], body=None) -> tuple[list[dict], list[dict]]:
    setup: list[dict] = []
    for op in ops:
        if op["operation"] not in ("INSERT", "UPDATE"):
            continue
        if isinstance(body, dict) and op.get("params"):
            values = {p: body.get(p) for p in op["params"] if p in body}
            if values:
                op = {**op, "values": values}
        setup.append(op)
    teardown = [op for op in ops if op["operation"] == "DELETE"]
    teardown_tables = {op.get("table") for op in teardown if op.get("table")}
    for op in setup:
        t = op.get("table")
        if t and t not in teardown_tables:
            teardown_tables.add(t)
            teardown.append({"source": "generated", "file": None, "owner": None, "method": None,
                             "operation": "DELETE", "entity": None, "table": t, "tables": [t],
                             "params": [], "sql": f"DELETE FROM {t}"})
    return setup, teardown


def trace_api_item(item, classes: dict, edges: list, candidates: list[dict],
                   endpoints: dict, max_depth: int) -> dict:
    notes: list[str] = []
    api_url = normalize_api_url(item.get("api_url"))
    method = (item.get("method") or "").upper() or None
    body = item.get("body", item.get("requestBody"))
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            pass
    if item.get("controller") and item.get("controller_method"):
        entries = [(item["controller"].rsplit(".", 1)[-1], item["controller_method"])]
    elif item.get("service") and item.get("service_method"):
        entries = [(item["service"].rsplit(".", 1)[-1], item["service_method"])]
    else:
        entries = resolve_endpoint(method, api_url, endpoints)
        if not entries:
            notes.append(f"没有匹配到端点：{method or ''} {api_url}".strip())
    found, chain = collect(entries, classes, edges, candidates, max_depth)
    setup, teardown = build_setup_teardown(found, body)
    if found and not setup and not teardown:
        notes.append("只收集到 SELECT 候选，未生成 setup/teardown SQL")
    if found and notes:
        status = "partial"
    elif found:
        status = "ok"
    elif notes:
        status = "not_found"
    else:
        status = "no_crud_found"
    return {
        "id": item.get("id") or f"{method or ''} {api_url}".strip(),
        "api_url": api_url, "method": method, "headers": item.get("headers") or {}, "body": body,
        "entry": {"class": entries[0][0], "method": entries[0][1]} if entries else None,
        "call_chain": chain, "setup": setup, "teardown": teardown,
        "trace": {"status": status, "notes": notes},
    }


def load_api_items(raw) -> list[dict]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, list):
        return data
    items = data.get("apis") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict) and data.get("api_url"):
        items = [data]
    return items or []


def summarize(ops: list[dict]) -> dict:
    summary = {"crud_total": len(ops), "crud_by_operation": {op: 0 for op in CRUD_OPS}, "crud_by_source": {}}
    for op in ops:
        summary["crud_by_operation"][op["operation"]] += 1
        summary["crud_by_source"][op["source"]] = summary["crud_by_source"].get(op["source"], 0) + 1
    return summary


NOTE = "本报告由候选收集器生成：SQL 均为代码中可直接找到的候选语句；JPA 派生方法、MyBatis-Plus、Provider、动态 SQL 等框架生成部分请按 SKILL.md 文字流程人工补全，并以实际代码为准。"


def analyze(repo, apis_raw=None, max_depth: int = 4, auto: bool = False) -> dict:
    root = Path(repo).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"项目路径不存在：{root}")
    files = list(iter_files(root))
    java_texts = []
    candidates: list[dict] = []
    for path, rel in files:
        if path.suffix == ".java":
            text = path.read_text(encoding="utf-8", errors="replace")
            java_texts.append((path, rel, text))
            candidates.extend(java_candidates(text, rel))
        else:
            candidates.extend(xml_candidates(path, rel))
    candidates = dedupe(candidates)
    classes = {}
    for _p, rel, text in java_texts:
        info = parse_class(text)
        if info["class"]:
            classes[info["class"]] = info
    endpoints = build_endpoints(java_texts) if (apis_raw is not None or auto) else {}
    edges = build_edges(classes)

    if apis_raw is None and not auto:
        return {"mode": "full-candidates", "project": str(root), "files_scanned": len(files),
                "note": NOTE, "summary": summarize(candidates), "sql_candidates": candidates}

    items = auto_discover_items(endpoints) if auto else load_api_items(apis_raw)
    results = [trace_api_item(it, classes, edges, candidates, endpoints, max_depth) for it in items]
    all_ops = [op for r in results for op in (r["setup"] + r["teardown"])]
    summary = {"apis_total": len(results), "apis_with_sql": len([r for r in results if r["setup"] or r["teardown"]])}
    summary.update(summarize(all_ops))
    return {"mode": "api-sql-setup", "project": str(root), "files_scanned": len(files),
            "apis_count": len(results), "note": NOTE, "summary": summary, "apis": results}


def strip_sql(ops: list[dict]) -> None:
    for op in ops:
        op["sql"] = None


def dump_result(result: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    if _yaml is None:
        raise RuntimeError("输出 YAML 需要 PyYAML（pip install pyyaml）")
    return _yaml.safe_dump(result, allow_unicode=True, sort_keys=False, default_flow_style=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="候选收集器：粗扫 Java Spring 项目中的 SQL 候选与端点。")
    ap.add_argument("repo", help="Java Spring 代码仓路径")
    ap.add_argument("apis", nargs="?", help="JSON 文件（api_url/请求方法/请求头/请求体）")
    ap.add_argument("--auto", action="store_true", help="自动发现所有 Controller 端点（无需输入文件）")
    ap.add_argument("--out", help="把结果写入该文件（默认 YAML）")
    ap.add_argument("--format", choices=("yaml", "json"), default="yaml", help="输出格式（默认 yaml）")
    ap.add_argument("--no-sql", action="store_true", help="结果中隐藏原始 SQL 文本")
    ap.add_argument("--depth", type=int, default=4, help="最大调用链深度（默认 4）")
    args = ap.parse_args(argv)
    if args.auto and args.apis:
        ap.error("--auto 不能与输入文件同时使用")
    apis_raw = Path(args.apis).read_text(encoding="utf-8") if args.apis else None
    result = analyze(args.repo, apis_raw, max_depth=args.depth, auto=args.auto)
    if args.no_sql:
        for r in result.get("apis", []):
            strip_sql(r["setup"])
            strip_sql(r["teardown"])
        if result.get("sql_candidates") is not None:
            strip_sql(result["sql_candidates"])
    output = dump_result(result, args.format)
    if args.out:
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
