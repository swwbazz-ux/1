"""Resolve direct calls through unambiguous module-level Python re-exports."""
import ast
import dis
from collections import Counter
from importlib.util import resolve_name
from pathlib import PurePosixPath
from types import CodeType


def module_symbols(path, source):
    parts = PurePosixPath(path).with_suffix("").parts
    package = ".".join(parts[:-1])
    module = package if parts[-1] == "__init__" else ".".join(parts)
    tree = ast.parse(source, filename=path)
    code = compile(tree, path, "exec")
    writes = Counter(ins.argval for ins in dis.get_instructions(code)
                     if ins.opname in {"STORE_NAME", "DELETE_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})
    imports, functions = {}, {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and writes[stmt.name] == 1:
            functions[stmt.name] = stmt
        if not isinstance(stmt, ast.ImportFrom):
            continue
        try:
            origin = resolve_name("." * stmt.level + (stmt.module or ""), package)
        except ImportError:
            continue
        for alias in stmt.names:
            local = alias.asname or alias.name
            if writes[local] == 1:
                imports[local] = (origin, alias.name)
    # A star import can replace any binding in this namespace.
    if any(alias.name == "*" for stmt in ast.walk(tree) if isinstance(stmt, ast.ImportFrom)
           for alias in stmt.names):
        imports.clear()
        functions.clear()
    return module, imports, functions, code


def resolve_reexport(modules, module, name):
    seen = set()
    while module in modules and (module, name) not in seen:
        _, imports, functions, _ = modules[module]
        if name in functions:
            return (module, name) if len(seen) >= 2 else None
        if name not in imports:
            break
        seen.add((module, name))
        module, name = imports[name]
    return None


def add_reexport_calls(graph, sources):
    nodes = {(n["source_file"].replace("\\", "/"), n.get("source_location"), n["label"]): n["id"]
             for n in graph["nodes"] if n.get("_callable") and n.get("source_file")}
    edges = graph["links"]
    existing = {(e.get("_src", e["source"]), e.get("_tgt", e["target"])) for e in edges
                if e.get("relation") == "calls"}
    modules = {}
    ambiguous = set()
    for path, data in sources.items():
        if not path.endswith(".py"):
            continue
        try:
            mod, imports, functions, code = module_symbols(path, data)
        except (SyntaxError, ValueError):
            continue  # Other language versions or incomplete working-tree edits.
        if mod in modules:
            ambiguous.add(mod)
        modules[mod] = path, imports, functions, code
    for mod in ambiguous:
        del modules[mod]

    count = 0
    for mod, (path, imports, functions, code) in modules.items():
        targets = {}
        for name in imports:
            resolved = resolve_reexport(modules, mod, name)
            if resolved:
                origin, original = resolved
                target_path, _, defs, _ = modules[origin]
                target = nodes.get((target_path, f"L{defs[original].lineno}", original + "()"))
                if target:
                    targets[name] = target
        if not targets:
            continue
        for body in code.co_consts:
            if not isinstance(body, CodeType) or body.co_name not in functions:
                continue
            fn = functions[body.co_name]
            caller = nodes.get((path, f"L{fn.lineno}", fn.name + "()"))
            if not caller:
                continue
            # Match the compiled global load to the AST callee at its exact position.
            # This excludes parameters, closures, nested functions and passing a callback as data.
            loads = {(i.positions.lineno, i.positions.col_offset, i.argval)
                     for i in dis.get_instructions(body) if i.opname == "LOAD_GLOBAL"}
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                callee = call.func
                target = targets.get(callee.id)
                if not target or (callee.lineno, callee.col_offset, callee.id) not in loads:
                    continue
                if (caller, target) in existing:
                    continue
                edges.append(dict(source=caller, target=target, relation="calls", context="call",
                                  confidence="EXTRACTED", confidence_score=1.0, weight=1.0,
                                  _origin="ast", source_file=path, source_location=f"L{callee.lineno}"))
                existing.add((caller, target))
                count += 1
    return count
