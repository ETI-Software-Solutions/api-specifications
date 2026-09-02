#!/usr/bin/env python3
"""
build.py — deterministic generator for the ETI Event Gateway Sonar CRUD spec.

Inputs
    sonar-crud.config.yaml        SSOT (entity -> root field mapping, hosts, policy)
    sonar-queries.graphql         Sonar SDL, `type Query` only
    sonar-mutations.graphql       Sonar SDL, `type Mutation` only

Outputs
    sonar-crud.schemas.json       JSON Schema draft-07 bundle, one definition per
                                  CRUD action, derived ONLY from SDL argument lists.
    sonar-crud-gateway.yaml       AsyncAPI 3.0.0 spec whose payload schemas $ref
                                  the bundle by absolute URL.

Determinism: no clocks, no UUIDs, no dict-order dependence. Re-running on the
same inputs is byte-identical; audit.py enforces that.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

import yaml

# --------------------------------------------------------------------------
# SDL parsing
# --------------------------------------------------------------------------

_COMMENT = re.compile(r"^\s*#")


def strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _COMMENT.match(ln))


def parse_root_type(text: str, root: str) -> "OrderedDict[str, dict]":
    """Extract {fieldName: {args: [(name, type)], returns: str}} from `type <root> { ... }`."""
    body = strip_comments(text)
    m = re.search(r"type\s+" + root + r"\s*\{(.*)\}\s*$", body, re.S)
    if not m:
        raise SystemExit(f"build: no `type {root}` block found")
    inner = m.group(1)

    fields: "OrderedDict[str, dict]" = OrderedDict()
    i, n = 0, len(inner)
    while i < n:
        fm = re.compile(r"\n\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*(\(|:)").search(inner, i)
        if not fm:
            break
        name = fm.group(1)
        if fm.group(2) == ":":
            rm = re.compile(r":\s*([^\n]+)").match(inner, fm.end() - 1)
            fields[name] = {"args": [], "returns": rm.group(1).strip()}
            i = rm.end()
            continue
        # balanced-paren scan for the argument list
        depth, j = 1, fm.end()
        while j < n and depth:
            if inner[j] == "(":
                depth += 1
            elif inner[j] == ")":
                depth -= 1
            j += 1
        arg_src = inner[fm.end(): j - 1]
        rm = re.compile(r"\s*:\s*([^\n]+)").match(inner, j)
        fields[name] = {
            "args": parse_args(arg_src),
            "returns": rm.group(1).strip() if rm else "Unknown",
        }
        i = rm.end() if rm else j
    return fields


def parse_args(src: str) -> list:
    """Split an argument list on top-level commas/newlines -> [(name, gqlType)]."""
    out, depth, buf = [], 0, []
    for ch in src:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if depth == 0 and ch in ",\n":
            token = "".join(buf).strip()
            if token:
                out.append(token)
            buf = []
        else:
            buf.append(ch)
    token = "".join(buf).strip()
    if token:
        out.append(token)

    parsed = []
    for token in out:
        am = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", token)
        if am:
            parsed.append((am.group(1), am.group(2).strip()))
    return parsed


# --------------------------------------------------------------------------
# GraphQL type -> JSON Schema draft-07
# --------------------------------------------------------------------------

def gql_to_schema(gql: str, type_map: dict) -> tuple:
    """Return (schema, required). Input-object types absent from the corpus become
    opaque objects with additionalProperties: true — deferred to Sonar's own
    validation rather than guessed at."""
    required = gql.rstrip().endswith("!")
    core = gql.rstrip().rstrip("!").strip()
    if core.startswith("["):
        item_gql = core[1:core.rindex("]")].strip()
        item_schema, _ = gql_to_schema(item_gql, type_map)
        return {"type": "array", "items": item_schema}, required
    if core in type_map:
        return json.loads(json.dumps(type_map[core])), required
    return {"type": "object", "additionalProperties": True,
            "x-graphql-input-type": core}, required


# --------------------------------------------------------------------------
# GraphQL document assembly
# --------------------------------------------------------------------------

FIELDS_SENTINEL = "\x00FIELDS\x00"


def render_document(op_name: str, kind: str, root: str, args: list,
                    selection: str, fields_expr: str) -> str:
    """Single-line GraphQL document, JSON-string safe (no double quotes, no
    newlines).

    GraphQL braces are space-separated so that no literal `{{` or `}}` pair can
    be mistaken for a Jinja delimiter. The caller-supplied `fields` expression is
    held out as a sentinel across that normalisation and substituted afterwards,
    because normalising it would split its own `}}` terminator.
    """
    var_decls = ", ".join(f"${n}: {t}" for n, t in args)
    var_pass = ", ".join(f"{n}: ${n}" for n, t in args)
    head = f"{kind} {op_name}"
    if var_decls:
        head += f"({var_decls})"
    field = root
    if var_pass:
        field += f"({var_pass})"
    doc = f"{head} {{ {field} {{ {selection} }} }}"
    doc = re.sub(r"\{\s*\{", "{ {", doc)
    doc = re.sub(r"\}\s*\}", "} }", doc)
    doc = doc.replace(FIELDS_SENTINEL, fields_expr)
    if '"' in doc or "\n" in doc:
        raise SystemExit(f"build: document for {op_name} is not JSON-string safe")
    return doc


def selection_for(action: str, spec: dict, cfg: dict) -> str:
    if action == "read":
        cw = cfg["connection_wrapper"]
        return (f"{cw['entities_field']} {{ {FIELDS_SENTINEL} }} "
                f"{cw['page_info_field']} {{ {cw['page_info_selection']} }}")
    if spec.get("returns") == "success":
        return cfg["success_response_selection"]
    return FIELDS_SENTINEL


# --------------------------------------------------------------------------
# Emitters
# --------------------------------------------------------------------------

def pascal(s: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[_\-]", s))


def build_actions(cfg: dict, queries: dict, mutations: dict) -> list:
    """Flatten config x SDL into the ordered action table everything else derives from."""
    actions = []
    for entity, ops in cfg["entities"].items():
        for op in ("read", "create", "update", "delete"):
            spec = ops[op]
            root = spec["root"]
            table = queries if spec["kind"] == "query" else mutations
            if root not in table:
                raise SystemExit(f"build: root field `{root}` absent from SDL")
            sdl = table[root]
            op_name = pascal(entity) + op.capitalize()
            actions.append({
                "entity": entity,
                "op": op,
                "action": f"{entity}.{op}",
                "message": op_name,
                "root": root,
                "kind": spec["kind"],
                "returns": spec.get("returns", "connection"),
                "args": sdl["args"],
                "gql_returns": sdl["returns"],
                "op_name": op_name,
            })
    return actions


def build_schemas(cfg: dict, actions: list, lookups: dict,
                  ranges: dict, vendor: dict) -> dict:
    tm = cfg["type_map"]
    defs = OrderedDict()
    for a in actions:
        props, required = OrderedDict(), []
        for name, gql in a["args"]:
            schema, req = gql_to_schema(gql, tm)
            props[name] = schema
            if req:
                required.append(name)

        variables = OrderedDict([
            ("type", "object"),
            ("additionalProperties", False),
            ("properties", props),
        ])
        if required:
            variables["required"] = required

        envelope = OrderedDict([
            ("$comment",
             f"Generated from `{a['kind']} {a['root']}` in the Sonar SDL. "
             f"Returns {a['gql_returns']}. Do not hand-edit."),
            ("type", "object"),
            ("additionalProperties", False),
            ("required", ["action", "correlationId"] + (["variables"] if required else [])),
            ("properties", OrderedDict([
                ("action", {"const": a["action"]}),
                ("correlationId", {"type": "string", "minLength": 1, "maxLength": 128}),
                ("requestedAt", {"type": "string"}),
                ("fields", OrderedDict([
                    ("type", "string"),
                    ("description",
                     "Flat GraphQL selection set: identifiers separated by single "
                     "spaces. Braces, parentheses and quotes are rejected so the "
                     "value cannot terminate the enclosing selection set."),
                    ("pattern",
                     r"^[A-Za-z_][A-Za-z0-9_]*( [A-Za-z_][A-Za-z0-9_]*)*$"),
                    ("maxLength", 2048),
                ])),
                ("variables", variables),
                ("callbackPath", {"type": "string",
                                  "pattern": r"^/[A-Za-z0-9_\-/\.]*$",
                                  "maxLength": 512}),
            ])),
        ])
        defs[a["message"] + "Request"] = envelope

    for lk in cfg.get("lookups", []):
        defs[lk["message"] + "Request"] = lookups[lk["action"]]["schema"]
    for rg in ranges.values():
        defs[rg["message"] + "Request"] = rg["schema"]
    defs.update(build_identifier_defs(cfg))
    defs.update(vendor)

    return OrderedDict([
        ("$schema", "http://json-schema.org/draft-07/schema#"),
        ("$id", cfg["schema_base_url"]),
        ("title", "Sonar CRUD gateway request envelopes"),
        ("description",
         "Derived from sonar-queries.graphql and sonar-mutations.graphql by "
         "build.py. Argument names, GraphQL types and nullability are taken "
         "verbatim from the SDL root fields. Input-object interiors are opaque "
         "because the SDL corpus contains no input type definitions."),
        ("definitions", defs),
    ])


# --------------------------------------------------------------------------
# Vendor schema flattening
# --------------------------------------------------------------------------

def load_vendor(cfg: dict, root: Path) -> "OrderedDict[str, dict]":
    """Read the vendored third-party definitions, verify them against
    vendor.lock.json, and return them namespace-prefixed with internal $refs
    rewritten. Flattening happens here so the gateway only ever dereferences a
    single remote $ref, one hop deep."""
    lock = json.loads((root / "vendor.lock.json").read_text())
    out: "OrderedDict[str, dict]" = OrderedDict()
    for ns, entry in sorted(cfg.get("vendor_schemas", {}).items()):
        raw = (root / entry["file"]).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if ns not in lock:
            raise SystemExit(f"build: vendor `{ns}` is not in vendor.lock.json")
        if digest != lock[ns]["extract_sha256"]:
            raise SystemExit(
                f"build: vendor `{ns}` digest mismatch — refuse to build.\n"
                f"  expected {lock[ns]['extract_sha256']}\n  actual   {digest}")
        prefix = entry["prefix"]
        defs = json.loads(raw.decode())["definitions"]
        for name, schema in sorted(defs.items()):
            body = json.dumps(schema).replace(
                '"#/definitions/', f'"#/definitions/{prefix}.')
            doc = json.loads(body)
            doc["$comment"] = (f"Vendored from {lock[ns]['url']} "
                               f"(upstream sha256 {lock[ns]['upstream_sha256'][:16]}). "
                               f"Do not hand-edit; see vendor.lock.json.")
            out[f"{prefix}.{name}"] = doc
    return out


# --------------------------------------------------------------------------
# Targeted lookups
# --------------------------------------------------------------------------

FROM_GUARD = "default(-1, true)"


def _arg_types(root_name: str, queries: dict) -> dict:
    if root_name not in queries:
        raise SystemExit(f"build: lookup root `{root_name}` absent from SDL")
    return dict(queries[root_name]["args"])


def config_expr(path: str, default) -> str:
    """CONFIG lookup with a build-time fallback. Tenant data (enum members,
    field ids) lives in application.yaml; the SSOT value is the audited
    default so the spec is still self-contained and a missing key narrows
    rather than widens."""
    keys = "".join(f"['{k}']" for k in path.split("."))
    if isinstance(default, int):
        return "{{ CONFIG%s | default(%d, true) }}" % (keys, default)
    # string-valued config lands in a JSON value position, so it must be quoted
    return '"{{ CONFIG%s | default(\'%s\', true) }}"' % (keys, default)


def identifier_schema_ref(name: str) -> dict:
    return {"$ref": f"#/definitions/identifier.{name}"}


def build_identifier_defs(cfg: dict) -> "OrderedDict[str, dict]":
    """One definition per identifier, referenced by every schema that needs
    it. This is the dereference point: change the constraint here and every
    action, present and future, moves with it."""
    out: "OrderedDict[str, dict]" = OrderedDict()
    for name, spec in cfg["identifiers"].items():
        base = OrderedDict([("$comment", spec["description"].strip()),
                            ("title", spec["title"])])
        if "tmf" in spec:
            base["allOf"] = [
                {"$ref": f"#/definitions/{spec['tmf']}"},
                OrderedDict([
                    ("type", "object"),
                    ("required", ["name", "value"]),
                    ("properties", OrderedDict([
                        ("name", {"const": spec["characteristic"]}),
                        ("value", spec["schema"]),
                    ])),
                ]),
            ]
        else:
            base.update(spec["schema"])
        out[f"identifier.{name}"] = base
    return out


def compile_lookup(lk: dict, cfg: dict, queries: dict, fields_expr: str) -> dict:
    """Turn one SSOT lookup into its document chain, receiveFrom entries and
    request schema. Every GraphQL type is read off the SDL, never assumed."""
    hop_sel = cfg["hop_selection"]
    cw = cfg["connection_wrapper"]
    ident_name = lk["identifier"]
    ident = cfg["identifiers"][ident_name]
    aliases_seen, entries, docs = [], [], OrderedDict()

    for i, hop in enumerate(lk["hops"]):
        last = i == len(lk["hops"]) - 1
        types = _arg_types(hop["root"], queries)
        decls, passes, bindings = [], [], OrderedDict()

        def bind(arg, gql, value):
            if arg not in types and gql is None:
                raise SystemExit(f"build: `{hop['root']}` has no argument `{arg}`")
            decls.append((arg, gql or types[arg]))
            passes.append(f"{arg}: ${arg}")
            bindings[arg] = value

        for arg, expr in hop.get("args", {}).items():
            if arg == "@identifier":
                if expr != ident_name:
                    raise SystemExit(
                        f"build: hop identifier `{expr}` != lookup "
                        f"identifier `{ident_name}`")
                if "resolver" in ident:
                    r = ident["resolver"]
                    if hop["root"] != r["root"]:
                        raise SystemExit(
                            f"build: `{ident_name}` resolves through "
                            f"`{r['root']}`, hop uses `{hop['root']}`")
                    bind(r["value_arg"], None,
                         "{{ DATA.%s.value | tojson }}" % ident_name)
                    sc = r["scope"]
                    bind(sc["arg"], None, config_expr(sc["config"], sc["default"]))
                else:
                    b = ident["bindings"][lk["entity"]]
                    if hop["root"] != b["root"]:
                        raise SystemExit(
                            f"build: `{ident_name}` binds `{lk['entity']}` to "
                            f"`{b['root']}`, hop uses `{hop['root']}`")
                    bind(b["arg"], None, "{{ DATA.%s | tojson }}" % ident_name)
                    if "scope" in b:
                        sc = b["scope"]
                        bind(sc["arg"], None,
                             config_expr(sc["config"], sc["default"]))
                continue
            if arg not in types:
                raise SystemExit(f"build: `{hop['root']}` has no argument `{arg}`")
            if expr.startswith("@from:"):
                _, alias, path = expr.split(":", 2)
                if alias not in aliases_seen:
                    raise SystemExit(
                        f"build: hop `{hop['alias']}` reads alias `{alias}` "
                        f"before it is produced")
                # A miss must narrow to nothing, never widen to everything.
                bind(arg, None, "{{ RECEIVED.%s.%s | %s }}"
                     % (alias, path, FROM_GUARD))
            elif expr.startswith("$"):
                bind(arg, None, "{{ DATA.%s | tojson }}" % expr[1:])
            else:
                raise SystemExit(f"build: unsupported arg expression {expr!r}")

        sel_spec = hop.get("selection")
        if last:
            inner = FIELDS_SENTINEL
        elif sel_spec and sel_spec.startswith("@hop:"):
            inner = hop_sel[sel_spec.split(":", 1)[1]]
        else:
            raise SystemExit(f"build: intermediate hop `{hop['alias']}` "
                             f"needs an explicit selection")
        selection = (f"{cw['entities_field']} {{ {inner} }} "
                     f"{cw['page_info_field']} {{ {cw['page_info_selection']} }}")

        op_name = lk["message"] + pascal(hop["alias"])
        doc = _assemble(op_name, "query", hop["root"], decls, passes,
                        selection, fields_expr)
        var_json = ", ".join(f'"{k}": {v}' for k, v in bindings.items())
        mname = f"{lk['message']}-{pascal(hop['alias'])}-Request"
        docs[mname] = OrderedDict([
            ("mapping-template", _Literal(
                "{\n"
                f'  "operationName": "{op_name}",\n'
                f'  "query": "{doc}",\n'
                f'  "variables": {{ {var_json} }}\n'
                "}\n")),
        ])
        entries.append((hop["alias"], mname))
        aliases_seen.append(hop["alias"])

    if aliases_seen[-1] != "sonar":
        raise SystemExit(f"build: lookup {lk['action']} final alias must be "
                         f"`sonar` so the result envelope can read it")

    props = _envelope_props()
    props[ident_name] = identifier_schema_ref(ident_name)
    schema = OrderedDict([
        ("$comment", f"Lookup by `{ident_name}`. GraphQL argument types are "
                     f"read off the Sonar SDL; the identifier constraint is "
                     f"dereferenced from #/definitions/identifier.{ident_name}."),
        ("type", "object"),
        ("additionalProperties", False),
        ("required", ["action", "correlationId", ident_name]),
        ("properties", OrderedDict(
            [("action", {"const": lk["action"]})]
            + [(k, v) for k, v in props.items() if k != "action"])),
    ])
    return {"entries": entries, "mappings": docs, "schema": schema}


def compile_range(entity: str, root: str, cfg: dict, queries: dict,
                  fields_expr: str) -> dict:
    """Generate `<entity>.read.byRange`.

    Which attribute may appear in which bucket is decided by the GraphQL type
    on the SDL root field, so a typo or a range predicate against a string
    column is rejected at the gateway rather than by Sonar. Each bucket emits
    the member shape Sonar documents for it: integer_fields carries an
    operator, string_fields carries match/partial_matching, boolean_fields
    carries neither.
    """
    dialect = cfg["search_dialect"]
    cw = cfg["connection_wrapper"]
    types = _arg_types(root, queries)
    skip = {"paginator", "sorter", "search", "general_search",
            "general_search_mode", "aggregation", "reverse_relation_filters"}

    where_props, loops, op_maps = OrderedDict(), [], OrderedDict()
    for bname, bk in dialect["buckets"].items():
        attrs = sorted(a for a, t in types.items()
                       if a not in skip and t.rstrip("!") in bk["gql_types"])
        if not attrs:
            continue

        props = OrderedDict([("attribute", {"enum": attrs}),
                             ("value", {"type": ["number", "string", "boolean"]})])
        required = ["attribute", "value"]
        member = bk["member"].replace("@ATTR", "{{ c.attribute | tojson }}")
        member = member.replace("@VALUE", "{{ c.value | tojson }}")
        if "ops" in bk:
            props["op"] = {"enum": sorted(bk["ops"])}
            required.insert(1, "op")
            var = bk.get("op_placeholder", "OPS_" + bname.upper())
            op_maps[var] = bk["ops"]
            member = member.replace("@OP", "{{ %s[c.op] }}" % var)
        where_props[bname] = OrderedDict([
            ("type", "array"),
            ("maxItems", cfg["ranges"]["max_predicates"]),
            ("items", OrderedDict([
                ("type", "object"), ("additionalProperties", False),
                ("required", required), ("properties", props)])),
        ])
        loops.append(
            '"%s": [{%% for c in (DATA.where.%s | default([], true)) %%}%s'
            '{%% if not loop.last %%},{%% endif %%}{%% endfor %%}]'
            % (bk["vendor_bucket"], bname, member))

    sets = "".join(
        "{%% set %s = {%s} %%}" % (v, ", ".join(f"'{k}': {json.dumps(x)}"
                                                for k, x in sorted(m.items())))
        for v, m in op_maps.items())

    op_name = pascal(entity) + "ReadByRange"
    decls = [("search", types["search"]), ("paginator", types["paginator"]),
             ("sorter", types["sorter"])]
    passes = ["search: $search", "paginator: $paginator", "sorter: $sorter"]
    selection = (f"{cw['entities_field']} {{ {FIELDS_SENTINEL} }} "
                 f"{cw['page_info_field']} {{ {cw['page_info_selection']} }}")
    doc = _assemble(op_name, "query", root, decls, passes, selection, fields_expr)

    mapping = OrderedDict([("mapping-template", _Literal(
        "{\n"
        f'  "operationName": "{op_name}",\n'
        f'  "query": "{doc}",\n'
        f'  "variables": {sets}{{\n'
        f'    "search": [{{ {", ".join(loops)} }}],\n'
        '    "paginator": {{ DATA.paginator | tojson }},\n'
        '    "sorter": {{ DATA.sorter | tojson }}\n'
        "  }\n"
        "}\n"))])

    props = _envelope_props()
    props["where"] = OrderedDict([
        ("type", "object"), ("additionalProperties", False),
        ("minProperties", 1),
        ("description",
         "Predicate buckets. Same attribute ORs, different attributes AND, "
         "per Sonar's documented Search semantics."),
        ("properties", where_props),
    ])
    action = f"{entity}.read.byRange"
    schema = OrderedDict([
        ("$comment", f"Range and comparison predicates over `{root}`. Bucket "
                     f"membership is derived from the SDL argument types."),
        ("type", "object"), ("additionalProperties", False),
        ("required", ["action", "correlationId", "where"]),
        ("properties", OrderedDict(
            [("action", {"const": action})]
            + [(k, v) for k, v in props.items() if k != "action"])),
    ])
    return {"action": action, "message": pascal(entity) + "ReadByRange",
            "mapping_name": pascal(entity) + "ReadByRange-Sonar-Request",
            "mapping": mapping, "schema": schema,
            "summary": f"Comparison and range predicates over {root}."}


def _assemble(op_name, kind, root, decls, passes, selection, fields_expr):
    head = f"{kind} {op_name}"
    if decls:
        head += "(" + ", ".join(f"${n}: {t}" for n, t in decls) + ")"
    field = root + ("(" + ", ".join(passes) + ")" if passes else "")
    doc = f"{head} {{ {field} {{ {selection} }} }}"
    doc = re.sub(r"\{\s*\{", "{ {", doc)
    doc = re.sub(r"\}\s*\}", "} }", doc)
    doc = doc.replace(FIELDS_SENTINEL, fields_expr)
    if '"' in doc or "\n" in doc:
        raise SystemExit(f"build: {op_name} document is not JSON-string safe")
    return doc


def _envelope_props() -> "OrderedDict[str, dict]":
    return OrderedDict([
        ("action", {}),
        ("correlationId", {"type": "string", "minLength": 1, "maxLength": 128}),
        ("requestedAt", {"type": "string"}),
        ("fields", OrderedDict([
            ("type", "string"),
            ("pattern", r"^[A-Za-z_][A-Za-z0-9_]*( [A-Za-z_][A-Za-z0-9_]*)*$"),
            ("maxLength", 2048),
        ])),
        ("paginator", {"type": "object", "additionalProperties": True}),
        ("sorter", {"type": "array", "items": {"type": "object"}}),
        ("callbackPath", {"type": "string",
                          "pattern": r"^/[A-Za-z0-9_\-/\.]*$",
                          "maxLength": 512}),
    ])


class _Literal(str):
    pass


def _literal_presenter(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_Literal, _literal_presenter)
yaml.add_representer(
    OrderedDict,
    lambda d, data: d.represent_mapping("tag:yaml.org,2002:map", data.items()),
)


def _sonar_hop(son: dict, mapping_name: str) -> "OrderedDict":
    return OrderedDict([
        ("channel", "sonar-graphql"), ("method", "POST"), ("address", ""),
        ("mapping", f"#/components/x-mappings/{mapping_name}"),
        ("headers", OrderedDict([
            ("Authorization", "Bearer {{ TOKEN.%s.accessToken }}" % son["token_api"]),
            ("Content-Type", "application/json"),
            ("Accept", "application/json"),
            ("X-Trace-Id", "{{ CONTEXT.traceId }}"),
        ])),
    ])


def _result_publish(action: str) -> list:
    return [
        OrderedDict([
            ("channel", "sonar-crud-results"),
            ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
            ("headers", OrderedDict([
                ("x-correlation-id", "{{ DATA.correlationId }}"),
                ("x-sonar-action", action),
                ("x-trace-id", "{{ CONTEXT.traceId }}")])),
        ]),
        OrderedDict([
            ("channel", "sonar-crud-callback"), ("method", "POST"),
            ("address", "{{ DATA.callbackPath | default('/results') }}"),
            ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
            ("headers", OrderedDict([
                ("Content-Type", "application/json"),
                ("x-correlation-id", "{{ DATA.correlationId }}"),
                ("x-sonar-action", action)])),
            ("resilience", OrderedDict([
                ("retry", OrderedDict([("maxAttempts", 4), ("wait", "PT0.5S"),
                                       ("exponentialBackoff", True),
                                       ("backoffMultiplier", 2.0)])),
                ("circuitBreaker", OrderedDict([("slidingWindowSize", 20),
                                                ("failureRateThreshold", 50),
                                                ("waitDurationInOpenState", "PT30S")])),
                ("timeLimiter", {"timeout": "PT5S"})])),
        ]),
    ]


def build_spec(cfg: dict, actions: list, lookups: dict,
               ranges: dict) -> dict:
    base = cfg["schema_base_url"]
    son = cfg["sonar"]
    res = cfg["result"]
    fields_expr = "{{ DATA.fields | default('%s') }}" % cfg["default_fields"]

    messages, schemas, rules, mappings = (OrderedDict() for _ in range(4))
    routes = OrderedDict()

    for a in actions:
        sid = a["message"] + "Request"
        schemas[sid] = {"$ref": f"{base}#/definitions/{sid}"}
        messages[a["message"]] = OrderedDict([
            ("name", a["message"]),
            ("title", f"{a['entity']} {a['op']}"),
            ("summary",
             f"{a['kind']} `{a['root']}` -> {a['gql_returns']}"),
            ("contentType", "application/json"),
            ("payload", {"$ref": f"#/components/schemas/{sid}"}),
        ])

        selection = selection_for(a["op"], a, cfg)
        doc = render_document(a["op_name"], a["kind"], a["root"], a["args"],
                              selection, fields_expr)
        mappings[a["message"] + "-Request"] = OrderedDict([
            ("mapping-template", _Literal(
                "{\n"
                f'  "operationName": "{a["op_name"]}",\n'
                f'  "query": "{doc}",\n'
                '  "variables": {{ DATA.variables | tojson }}\n'
                "}\n")),
        ])

        rules[a["message"] + "-Rules"] = [OrderedDict([
            ("receiveFrom", OrderedDict([
                ("sonar", OrderedDict([
                    ("channel", "sonar-graphql"),
                    ("method", "POST"),
                    ("address", ""),
                    ("mapping", f"#/components/x-mappings/{a['message']}-Request"),
                    ("headers", OrderedDict([
                        ("Authorization",
                         "Bearer {{ TOKEN.%s.accessToken }}" % son["token_api"]),
                        ("Content-Type", "application/json"),
                        ("Accept", "application/json"),
                        ("X-Trace-Id", "{{ CONTEXT.traceId }}"),
                    ])),
                ])),
            ])),
            ("publishTo", [
                OrderedDict([
                    ("channel", "sonar-crud-results"),
                    ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
                    ("headers", OrderedDict([
                        ("x-correlation-id", "{{ DATA.correlationId }}"),
                        ("x-sonar-action", a["action"]),
                        ("x-trace-id", "{{ CONTEXT.traceId }}"),
                    ])),
                ]),
                OrderedDict([
                    ("channel", "sonar-crud-callback"),
                    ("method", "POST"),
                    ("address", "{{ DATA.callbackPath | default('/results') }}"),
                    ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
                    ("headers", OrderedDict([
                        ("Content-Type", "application/json"),
                        ("x-correlation-id", "{{ DATA.correlationId }}"),
                        ("x-sonar-action", a["action"]),
                    ])),
                    ("resilience", OrderedDict([
                        ("retry", OrderedDict([
                            ("maxAttempts", 4),
                            ("wait", "PT0.5S"),
                            ("exponentialBackoff", True),
                            ("backoffMultiplier", 2.0),
                        ])),
                        ("circuitBreaker", OrderedDict([
                            ("slidingWindowSize", 20),
                            ("failureRateThreshold", 50),
                            ("waitDurationInOpenState", "PT30S"),
                        ])),
                        ("timeLimiter", {"timeout": "PT5S"}),
                    ])),
                ]),
            ]),
        ])]

        routes[a["action"]] = {
            "x-routing-rules": f"#/components/x-routing-rules/{a['message']}-Rules"
        }

    for lk in cfg.get("lookups", []):
        comp = lookups[lk["action"]]
        sid = lk["message"] + "Request"
        schemas[sid] = {"$ref": f"{base}#/definitions/{sid}"}
        messages[lk["message"]] = OrderedDict([
            ("name", lk["message"]),
            ("title", lk["action"]),
            ("summary", lk["summary"]),
            ("contentType", "application/json"),
            ("payload", {"$ref": f"#/components/schemas/{sid}"}),
        ])
        mappings.update(comp["mappings"])
        rf = OrderedDict()
        for alias, mname in comp["entries"]:
            rf[alias] = OrderedDict([
                ("channel", "sonar-graphql"),
                ("method", "POST"),
                ("address", ""),
                ("mapping", f"#/components/x-mappings/{mname}"),
                ("headers", OrderedDict([
                    ("Authorization",
                     "Bearer {{ TOKEN.%s.accessToken }}" % son["token_api"]),
                    ("Content-Type", "application/json"),
                    ("Accept", "application/json"),
                    ("X-Trace-Id", "{{ CONTEXT.traceId }}"),
                ])),
            ])
        rules[lk["message"] + "-Rules"] = [OrderedDict([
            ("receiveFrom", rf),
            ("publishTo", [
                OrderedDict([
                    ("channel", "sonar-crud-results"),
                    ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
                    ("headers", OrderedDict([
                        ("x-correlation-id", "{{ DATA.correlationId }}"),
                        ("x-sonar-action", lk["action"]),
                        ("x-trace-id", "{{ CONTEXT.traceId }}"),
                    ])),
                ]),
                OrderedDict([
                    ("channel", "sonar-crud-callback"),
                    ("method", "POST"),
                    ("address", "{{ DATA.callbackPath | default('/results') }}"),
                    ("mapping", "#/components/x-mappings/Sonar-Result-Envelope"),
                    ("headers", OrderedDict([
                        ("Content-Type", "application/json"),
                        ("x-correlation-id", "{{ DATA.correlationId }}"),
                        ("x-sonar-action", lk["action"]),
                    ])),
                    ("resilience", OrderedDict([
                        ("retry", OrderedDict([
                            ("maxAttempts", 4), ("wait", "PT0.5S"),
                            ("exponentialBackoff", True),
                            ("backoffMultiplier", 2.0)])),
                        ("circuitBreaker", OrderedDict([
                            ("slidingWindowSize", 20),
                            ("failureRateThreshold", 50),
                            ("waitDurationInOpenState", "PT30S")])),
                        ("timeLimiter", {"timeout": "PT5S"}),
                    ])),
                ]),
            ]),
        ])]
        routes[lk["action"]] = {
            "x-routing-rules":
                f"#/components/x-routing-rules/{lk['message']}-Rules"}

    for action, rg in ranges.items():
        sid = rg["message"] + "Request"
        schemas[sid] = {"$ref": f"{base}#/definitions/{sid}"}
        messages[rg["message"]] = OrderedDict([
            ("name", rg["message"]), ("title", action),
            ("summary", rg["summary"]), ("contentType", "application/json"),
            ("payload", {"$ref": f"#/components/schemas/{sid}"}),
        ])
        mappings[rg["mapping_name"]] = rg["mapping"]
        rules[rg["message"] + "-Rules"] = [OrderedDict([
            ("receiveFrom", {"sonar": _sonar_hop(son, rg["mapping_name"])}),
            ("publishTo", _result_publish(action)),
        ])]
        routes[action] = {"x-routing-rules":
                          f"#/components/x-routing-rules/{rg['message']}-Rules"}

    # Shared result envelope. `| tojson` on an absent key renders the literal
    # four-char `null`, which is what makes the transport-failure case explicit.
    mappings["Sonar-Result-Envelope"] = OrderedDict([
        ("mapping-template", _Literal(
            "{\n"
            '  "correlationId": "{{ DATA.correlationId }}",\n'
            '  "action": "{{ DATA.action }}",\n'
            '  "status": "{{ \'OK\' if RECEIVED.sonar.data is defined and '
            "(RECEIVED.sonar.errors | default([])) | length == 0 "
            "else 'ERROR' }}\",\n"
            '  "traceId": "{{ CONTEXT.traceId }}",\n'
            '  "spanId": "{{ CONTEXT.spanId }}",\n'
            '  "data": {{ RECEIVED.sonar.data | tojson }},\n'
            '  "errors": {{ RECEIVED.sonar.errors | tojson }}\n'
            "}\n")),
    ])

    messages["SonarGraphQLRequest"] = OrderedDict([
        ("name", "SonarGraphQLRequest"),
        ("title", "GraphQL request envelope"),
        ("contentType", "application/json"),
        ("payload", {"$ref": "#/components/schemas/SonarGraphQLRequest"}),
    ])
    messages["SonarCrudResult"] = OrderedDict([
        ("name", "SonarCrudResult"),
        ("title", "CRUD result envelope"),
        ("contentType", "application/json"),
        ("payload", {"$ref": "#/components/schemas/SonarCrudResult"}),
    ])
    messages["SonarCrudRejected"] = OrderedDict([
        ("name", "SonarCrudRejected"),
        ("title", "Dead-lettered request"),
        ("contentType", "application/json"),
        ("payload", {"type": "object", "additionalProperties": True}),
    ])

    schemas["SonarGraphQLRequest"] = OrderedDict([
        ("type", "object"),
        ("required", ["query"]),
        ("properties", OrderedDict([
            ("operationName", {"type": "string"}),
            ("query", {"type": "string"}),
            ("variables", {"type": ["object", "null"]}),
        ])),
    ])
    schemas["SonarCrudResult"] = OrderedDict([
        ("type", "object"),
        ("required", ["correlationId", "action", "status"]),
        ("properties", OrderedDict([
            ("correlationId", {"type": "string"}),
            ("action", {"type": "string"}),
            ("status", {"enum": ["OK", "ERROR"]}),
            ("traceId", {"type": "string"}),
            ("spanId", {"type": "string"}),
            ("data", {"type": ["object", "array", "null"]}),
            ("errors", {"type": ["array", "null"]}),
        ])),
    ])

    channel_messages = OrderedDict(
        (a["message"], {"$ref": f"#/components/messages/{a['message']}"})
        for a in actions
    )
    for lk in cfg.get("lookups", []):
        channel_messages[lk["message"]] = {
            "$ref": f"#/components/messages/{lk['message']}"}
    for rg in ranges.values():
        channel_messages[rg["message"]] = {
            "$ref": f"#/components/messages/{rg['message']}"}

    spec = OrderedDict()
    spec["asyncapi"] = "3.0.0"
    spec["info"] = OrderedDict([
        ("title", "ETI Event Gateway - Sonar GraphQL CRUD facade"),
        ("version", cfg["version"]),
        ("description",
         "Generated by build.py from sonar-crud.config.yaml plus the Sonar SDL. "
         "Do not hand-edit; regenerate and re-run audit.py.\n\n"
         "Payload schemas are external references into "
         f"{cfg['schema_base_url']}, which build.py derives from the SDL. The "
         "GraphQL schema is therefore never restated inside this document.\n\n"
         "Results are delivered OUT OF BAND. The webhook response renderer binds "
         "only DATA, EVENT_ID, RETRY_AFTER, ERROR and REQUEST, so the Sonar "
         "payload cannot ride back on the HTTP response. Callers correlate on "
         "correlationId over the result channels."),
    ])
    spec["defaultContentType"] = "application/json"

    spec["servers"] = OrderedDict([
        ("crud-ingress", OrderedDict([
            ("host", "{ingressHost}"),
            ("pathname", "/webhooks"),
            ("protocol", "https"),
            ("description", "Inbound CRUD requests from ETI provisioning clients."),
            ("variables", {"ingressHost": {"default": "gateway.etisoftware.com"}}),
        ])),
        ("sonar-api", OrderedDict([
            ("host", "{%s}" % son["host_var"]),
            ("protocol", "https"),
            ("description", "Sonar GraphQL endpoint."),
            ("variables", {son["host_var"]: {"default": son["host_default"]}}),
        ])),
        ("results-kafka", OrderedDict([
            ("host", "{kafkaHost}:{kafkaPort}"),
            ("protocol", "kafka"),
            ("variables", OrderedDict([
                ("kafkaHost", {"default": "kafka"}),
                ("kafkaPort", {"default": "9092"}),
            ])),
        ])),
        ("callback-http", OrderedDict([
            ("host", "{callbackHost}"),
            ("protocol", "https"),
            ("description", "Client callback sink for CRUD results."),
            ("variables", {"callbackHost": {"default": "provisioning.etisoftware.com"}}),
        ])),
    ])

    spec["channels"] = OrderedDict([
        ("sonar-crud-in", OrderedDict([
            ("address", "/sonar/crud"),
            ("servers", [{"$ref": "#/servers/crud-ingress"}]),
            ("messages", channel_messages),
            ("x-webhook-auth", OrderedDict([
                ("type", "hmac-sha256"),
                ("secretEnv", "SONAR_CRUD_WEBHOOK_SECRET"),
                ("signatureHeader", "X-Signature-256"),
                ("signaturePrefix", "sha256="),
            ])),
            ("x-idempotency-key", OrderedDict([
                ("key", "$.correlationId"),
                ("ttl", "1h"),
            ])),
            ("x-rate-limit", OrderedDict([("rps", 50), ("burst", 100)])),
            ("x-log-redact", [
                "$.variables.input.password",
                "$.variables.input.email_address",
                "$.variables.input.phone_number",
            ]),
            ("x-payload-normalization", {"prune-empty-elements": False}),
            ("x-webhook-response", OrderedDict([
                ("success", OrderedDict([
                    ("status", 200),
                    ("contentType", "application/json"),
                    ("body", _Literal(
                        '{ "correlationId": "{{ DATA.correlationId }}", '
                        '"status": "ACCEPTED", "eventId": "{{ EVENT_ID }}" }\n')),
                ])),
                ("duplicate", OrderedDict([
                    ("status", 200),
                    ("body", _Literal(
                        '{ "correlationId": "{{ DATA.correlationId }}", '
                        '"status": "DUPLICATE" }\n')),
                ])),
                ("schemaNoMatch", OrderedDict([
                    ("status", 400),
                    ("body", _Literal(
                        '{ "status": "REJECTED", "reason": "payload matched no '
                        'declared CRUD action schema" }\n')),
                ])),
                ("classificationNoMatch", OrderedDict([
                    ("status", 400),
                    ("body", _Literal(
                        '{ "status": "REJECTED", "reason": "unknown action" }\n')),
                ])),
                ("parseFailure", OrderedDict([
                    ("status", 400),
                    ("body", _Literal(
                        '{ "status": "REJECTED", "reason": "unparseable body" }\n')),
                ])),
                ("unauthorized", OrderedDict([
                    ("status", 401),
                    ("body", _Literal('{ "status": "UNAUTHORIZED" }\n')),
                ])),
                ("rateLimited", OrderedDict([
                    ("status", 429),
                    # Custom headers REPLACE the defaults, so Retry-After is
                    # re-declared here on purpose.
                    ("headers", {"Retry-After": "{{ RETRY_AFTER }}"}),
                    ("body", _Literal('{ "status": "RATE_LIMITED" }\n')),
                ])),
                ("error", OrderedDict([
                    ("status", 500),
                    ("body", _Literal(
                        '{ "status": "ERROR", "reason": '
                        '"{{ ERROR.reason }}" }\n')),
                ])),
            ])),
            ("x-classification", OrderedDict([
                ("discriminator", "action"),
                ("discriminatorType", "PROPERTY_VALUE"),
                # Every request schema pins `action` with a const, so at most one
                # candidate can validate; FIRST is order-insensitive here.
                ("matchMode", "FIRST"),
                ("routes", routes),
                ("onNoMatch", OrderedDict([
                    ("action", "publish"),
                    ("channel", "sonar-crud-dlq"),
                    ("failureType", "PAYLOAD_FILTER_NO_MATCH"),
                ])),
            ])),
        ])),
        ("sonar-graphql", OrderedDict([
            ("address", son["pathname"]),
            ("servers", [{"$ref": "#/servers/sonar-api"}]),
            ("messages", {"SonarGraphQLRequest":
                          {"$ref": "#/components/messages/SonarGraphQLRequest"}}),
            ("bindings", {"http": {"headers": {
                "X-Source-System": "eti-event-gateway",
                "User-Agent": "eti-event-gateway/sonar-crud",
            }}}),
        ])),
        ("sonar-crud-results", OrderedDict([
            ("address", res["kafka_topic"]),
            ("servers", [{"$ref": "#/servers/results-kafka"}]),
            ("messages", {"SonarCrudResult":
                          {"$ref": "#/components/messages/SonarCrudResult"}}),
        ])),
        ("sonar-crud-callback", OrderedDict([
            ("address", res["callback_pathname"]),
            ("servers", [{"$ref": "#/servers/callback-http"}]),
            ("messages", {"SonarCrudResult":
                          {"$ref": "#/components/messages/SonarCrudResult"}}),
        ])),
        ("sonar-crud-dlq", OrderedDict([
            ("address", res["kafka_topic"] + ".dlq"),
            ("servers", [{"$ref": "#/servers/results-kafka"}]),
            ("messages", {"SonarCrudRejected":
                          {"$ref": "#/components/messages/SonarCrudRejected"}}),
        ])),
    ])

    spec["operations"] = OrderedDict([
        ("onSonarCrudRequest", OrderedDict([
            ("action", "receive"),
            ("channel", {"$ref": "#/channels/sonar-crud-in"}),
            ("bindings", {"http": {"method": "POST"}}),
            # sync so DATA is bound in the response renderer and the caller's
            # correlationId can be echoed on the ack.
            ("x-ack-style", "sync"),
            ("x-debug-validation", False),
        ])),
        ("callSonarGraphql", OrderedDict([
            ("action", "send"),
            ("channel", {"$ref": "#/channels/sonar-graphql"}),
        ])),
        ("publishCrudResult", OrderedDict([
            ("action", "send"),
            ("channel", {"$ref": "#/channels/sonar-crud-results"}),
        ])),
        ("postCrudCallback", OrderedDict([
            ("action", "send"),
            ("channel", {"$ref": "#/channels/sonar-crud-callback"}),
        ])),
        ("publishCrudDlq", OrderedDict([
            ("action", "send"),
            ("channel", {"$ref": "#/channels/sonar-crud-dlq"}),
        ])),
    ])

    spec["components"] = OrderedDict([
        ("messages", messages),
        ("schemas", schemas),
        ("x-routing-rules", rules),
        ("x-mappings", mappings),
    ])
    return spec


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sonar-crud.config.yaml")
    ap.add_argument("--sdl-dir", default=".")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    sdl_dir, out_dir = Path(args.sdl_dir), Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    queries = parse_root_type(
        (sdl_dir / cfg["sdl"]["queries"]).read_text(), "Query")
    mutations = parse_root_type(
        (sdl_dir / cfg["sdl"]["mutations"]).read_text(), "Mutation")

    actions = build_actions(cfg, queries, mutations)
    vendor = load_vendor(cfg, Path(args.config).parent)
    fields_expr = "{{ DATA.fields | default('%s') }}" % cfg["default_fields"]
    lookups = {lk["action"]: compile_lookup(lk, cfg, queries, fields_expr)
               for lk in cfg.get("lookups", [])}
    ranges = {}
    for ent in cfg.get("ranges", {}).get("entities", []):
        root = cfg["entities"][ent]["read"]["root"]
        rg = compile_range(ent, root, cfg, queries, fields_expr)
        ranges[rg["action"]] = rg
    schemas = build_schemas(cfg, actions, lookups, ranges, vendor)
    spec = build_spec(cfg, actions, lookups, ranges)

    schema_txt = json.dumps(schemas, indent=2) + "\n"
    spec_txt = yaml.dump(spec, sort_keys=False, default_flow_style=False,
                         width=100, allow_unicode=True)

    (out_dir / "sonar-crud.schemas.json").write_text(schema_txt)
    (out_dir / "sonar-crud-gateway.yaml").write_text(spec_txt)

    print(f"build: {len(queries)} query roots, {len(mutations)} mutation roots parsed")
    print(f"build: {len(actions)} CRUD actions across "
          f"{len(cfg['entities'])} entities")
    print(f"build: {len(lookups)} targeted lookups, "
          f"{sum(len(l['entries']) for l in lookups.values())} hops")
    print(f"build: {len(ranges)} range actions")
    print(f"build: {len(vendor)} vendor definitions flattened "
          f"({', '.join(sorted(cfg.get('vendor_schemas', {})))})")
    print(f"build: schemas sha256={hashlib.sha256(schema_txt.encode()).hexdigest()[:16]}")
    print(f"build: spec    sha256={hashlib.sha256(spec_txt.encode()).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
