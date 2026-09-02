#!/usr/bin/env python3
"""
harness.py — layered test harness for the Sonar CRUD gateway interface.

Five stages, in increasing order of cost and of evidential weight:

  L1 schema   offline   fixture bodies against the published schema bundle
  L2 render   offline   templates -> GraphQL documents, parsed and checked
                        against the SDL; includes the narrowing probe
  L3 spec     gateway   spec upload, then /admin/validate for every fixture
  L4 route    gateway   end-to-end through the gateway into mock_sonar
  L5 probe    tenant    read-only introspection; closes vendor gaps

Each stage prints what it proves and, in HARNESS.md, what it does not. Any
failure in any selected stage exits non-zero. There is no warning tier.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from graphql import parse as gql_parse
from graphql.language import ast as gast
from jsonschema import Draft7Validator

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import jinjava_model as JM                                    # noqa: E402
import build as B                                             # noqa: E402

ROOT = Path(__file__).parent.parent
HERE = Path(__file__).parent


# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.rows, self.failures = [], []

    def check(self, stage: str, name: str, ok: bool, detail: str = ""):
        self.rows.append((stage, name, ok, detail))
        if not ok:
            self.failures.append(f"{stage} {name}: {detail}")
        return ok

    def stage_summary(self, stage: str):
        rows = [r for r in self.rows if r[0] == stage]
        bad = [r for r in rows if not r[2]]
        for r in bad:
            print(f"    FAIL {r[1]}: {r[3]}")
        print(f"    {len(rows) - len(bad)}/{len(rows)} checks passed")


def load():
    cfg = yaml.safe_load((ROOT / "sonar-crud.config.yaml").read_text())
    spec = yaml.safe_load((ROOT / "sonar-crud-gateway.yaml").read_text())
    bundle = json.loads((ROOT / "sonar-crud.schemas.json").read_text())
    fixtures = json.loads((ROOT / "fixtures.json").read_text())
    scenarios = json.loads((HERE / "scenarios.json").read_text())
    import os
    raw_app = yaml.safe_load((ROOT / "application.yaml").read_text())
    problems: list = []
    app = spring_resolve(raw_app, os.environ, problems=problems)
    app["__placeholder_problems__"] = problems
    queries = B.parse_root_type((ROOT / cfg["sdl"]["queries"]).read_text(), "Query")
    mutations = B.parse_root_type((ROOT / cfg["sdl"]["mutations"]).read_text(),
                                  "Mutation")
    return cfg, spec, bundle, fixtures, scenarios, app, queries, mutations


def rule_for(spec, action):
    routes = spec["channels"]["sonar-crud-in"]["x-classification"]["routes"]
    if action not in routes:
        return None
    name = routes[action]["x-routing-rules"].rsplit("/", 1)[-1]
    return spec["components"]["x-routing-rules"][name][0]


SPRING_PH = __import__("re").compile(r"^\$\{([A-Za-z0-9_.]+)(?::([^}]*))?\}$")


def spring_resolve(node, env, path="", problems=None):
    """Model Spring's placeholder binding.

    `CONFIG` exposes the RESOLVED Environment, never the raw `${VAR:default}`
    text, so a harness that reads application.yaml literally would render the
    placeholder straight into a GraphQL document. Resolving here is what makes
    the L2 prediction faithful — and a placeholder with neither an environment
    value nor an inline default is reported, because Spring fails to start on
    exactly that.
    """
    problems = [] if problems is None else problems
    if isinstance(node, dict):
        return ({k: spring_resolve(v, env, f"{path}.{k}".lstrip("."), problems)
                 for k, v in node.items()}, problems)[0]
    if isinstance(node, list):
        return [spring_resolve(v, env, path, problems) for v in node]
    if isinstance(node, str):
        m = SPRING_PH.match(node.strip())
        if not m:
            return node
        name, default = m.group(1), m.group(2)
        if name in env:
            raw = env[name]
        elif default is not None:
            raw = default
        else:
            problems.append(f"{path}: ${{{name}}} has no value and no default; "
                            f"Spring would refuse to start")
            return node
        for cast in (int, float):
            try:
                return cast(raw)
            except (TypeError, ValueError):
                pass
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return raw
    return node


def base_context(app, body):
    """The bindings the router installs, modelled. CONFIG comes from the real
    application.yaml, so a spec CONFIG lookup with no backing property shows up
    here as a render failure rather than at 3am."""
    return {
        "DATA": body,
        "CONFIG": app,
        "TOKEN": {"sonar": {"accessToken": "test-token", "expiresIn": 3600}},
        "CONTEXT": {"traceId": "0" * 32, "spanId": "0" * 16},
        "RECEIVED": {},
        "CURRENT": body,
    }


# --------------------------------------------------------------------------
# L1 — schema conformance
# --------------------------------------------------------------------------

def stage_schema(rep, bundle, fixtures):
    print("\n[L1 schema] fixture bodies against the published bundle")
    for want in ("accept", "reject"):
        for case in fixtures[want]:
            name = case["definition"]
            if name not in bundle["definitions"]:
                rep.check("L1", case["note"], False, f"unknown definition {name}")
                continue
            schema = {"allOf": [{"$ref": f"#/definitions/{name}"}],
                      "definitions": bundle["definitions"]}
            errs = list(Draft7Validator(schema).iter_errors(case["body"]))
            ok = (not errs) if want == "accept" else bool(errs)
            rep.check("L1", f"{want}: {case['note']}", ok,
                      "validated when it should not have" if want == "reject"
                      else "; ".join(e.message[:90] for e in errs[:2]))
    rep.stage_summary("L1")


# --------------------------------------------------------------------------
# L2 — offline render, GraphQL parse, SDL conformance, narrowing probe
# --------------------------------------------------------------------------

def check_document(rep, label, doc_text, variables, queries, mutations):
    try:
        doc = gql_parse(doc_text)
    except Exception as exc:
        rep.check("L2", f"{label} parses as GraphQL", False, str(exc)[:120])
        return
    rep.check("L2", f"{label} parses as GraphQL", True)

    op = doc.definitions[0]
    table = queries if op.operation.value == "query" else mutations
    declared = {v.variable.name.value: v for v in (op.variable_definitions or ())}

    field = op.selection_set.selections[0]
    root = field.name.value
    if not rep.check("L2", f"{label} root `{root}` exists in the SDL",
                     root in table, "absent from the SDL"):
        return
    sdl_args = dict(table[root]["args"])

    for arg in field.arguments:
        rep.check("L2", f"{label} argument `{arg.name.value}`",
                  arg.name.value in sdl_args,
                  f"`{arg.name.value}` is not an argument of {root}")

    for vname, vdef in declared.items():
        want = sdl_args.get(vname)
        got = _type_str(vdef.type)
        if want is None:
            continue                       # named after the arg it feeds
        rep.check("L2", f"{label} ${vname} type", got == want,
                  f"declared {got}, SDL says {want}")

    supplied = set(variables or {})
    extra = supplied - set(declared)
    rep.check("L2", f"{label} no undeclared variable is sent", not extra,
              f"sent but not declared: {sorted(extra)}")
    required = {n for n, d in declared.items()
                if isinstance(d.type, gast.NonNullTypeNode)}
    missing = {n for n in required
               if (variables or {}).get(n) in (None, "")}
    rep.check("L2", f"{label} every non-null variable is supplied", not missing,
              f"missing or null: {sorted(missing)}")


def _type_str(node) -> str:
    if isinstance(node, gast.NonNullTypeNode):
        return _type_str(node.type) + "!"
    if isinstance(node, gast.ListTypeNode):
        return "[" + _type_str(node.type) + "]"
    return node.name.value


def render_chain(spec, app, body, scenarios, force_empty_hop=None):
    """Render every receiveFrom mapping for an action in declaration order,
    feeding each hop's canned response into RECEIVED for the next — exactly the
    ordering the router uses."""
    rule = rule_for(spec, body["action"])
    mappings = spec["components"]["x-mappings"]
    ctx = base_context(app, body)
    out = []
    for alias, entry in (rule.get("receiveFrom") or {}).items():
        mname = entry["mapping"].rsplit("/", 1)[-1]
        tpl = mappings[mname]["mapping-template"]
        text = JM.render(tpl, ctx, mname)
        payload = json.loads(text)
        out.append((alias, mname, payload))

        op = payload["operationName"]
        if force_empty_hop == alias:
            resp = {"data": {}, "errors": []}
        else:
            sc = scenarios["sonar"].get(op, {})
            resp = sc.get("default", {"data": {}})
            for case in sc.get("cases", []):
                v = case["when"]["variable"]
                if (payload.get("variables") or {}).get(v) == case["when"]["equals"]:
                    resp = case["respond"]
        ctx["RECEIVED"][alias] = resp
    return out, ctx


def stage_render(rep, cfg, spec, app, fixtures, scenarios, queries, mutations):
    print("\n[L2 render] templates -> documents, checked against the SDL")

    used = set()
    for tpl in spec["components"]["x-mappings"].values():
        for m in re.finditer(r"CONFIG((?:\['[^']+'\])+)",
                             tpl.get("mapping-template", "")):
            used.add(".".join(re.findall(r"\['([^']+)'\]", m.group(1))))
    for problem in app.get("__placeholder_problems__", []):
        path = problem.split(":")[0]
        if any(path == u or path.startswith(u + ".") for u in used):
            rep.check("L2", "CONFIG placeholder resolves", False, problem)

    # A CONFIG value that lands in a JSON number position must resolve to a
    # number, or the rendered document is not JSON at all.
    for name, ispec in cfg["identifiers"].items():
        scopes = ([ispec["resolver"]["scope"]] if "resolver" in ispec else [])
        scopes += [b["scope"] for b in ispec.get("bindings", {}).values()
                   if "scope" in b]
        for sc in scopes:
            node = app
            for key in sc["config"].split("."):
                node = node.get(key) if isinstance(node, dict) else None
            want_num = isinstance(sc["default"], int)
            got_num = isinstance(node, (int, float)) and not isinstance(node, bool)
            rep.check("L2", f"CONFIG {sc['config']} resolves to the right type",
                      node is not None and (got_num == want_num),
                      f"resolved to {node!r}; "
                      f"{'numeric' if want_num else 'string'} required")

    for case in fixtures["accept"]:
        body = case["body"]
        if rule_for(spec, body["action"]) is None:
            rep.check("L2", f"route for {body['action']}", False, "no route")
            continue
        try:
            chain, ctx = render_chain(spec, app, body, scenarios)
        except JM.UnsupportedConstruct as exc:
            rep.check("L2", f"{body['action']} template subset", False, str(exc))
            continue
        except Exception as exc:
            rep.check("L2", f"{body['action']} renders", False,
                      f"{type(exc).__name__}: {exc}"[:140])
            continue
        rep.check("L2", f"{body['action']} renders to JSON", True)
        for alias, mname, payload in chain:
            check_document(rep, f"{body['action']}/{alias}", payload["query"],
                           payload.get("variables"), queries, mutations)

        env = spec["components"]["x-mappings"]["Sonar-Result-Envelope"][
            "mapping-template"]
        final = ctx["RECEIVED"].get("sonar", {})
        want = "ERROR" if (final.get("errors")
                           or final.get("data") is None) else "OK"
        try:
            result = json.loads(JM.render(env, ctx, "Sonar-Result-Envelope"))
            rep.check("L2", f"{body['action']} result envelope is JSON", True)
            rep.check("L2", f"{body['action']} status discriminates ({want})",
                      result["status"] == want,
                      f"scenario implies {want}, envelope said {result['status']}")
        except Exception as exc:
            rep.check("L2", f"{body['action']} result envelope is JSON", False,
                      str(exc)[:120])

    # ---- narrowing probe -------------------------------------------------
    # The failure this exists to catch: an unresolved upstream id renders
    # empty, the filter argument disappears, and the query returns the table.
    print("    narrowing probe (unresolved upstream id must not widen)")
    for case in fixtures["accept"]:
        body = case["body"]
        rule = rule_for(spec, body["action"])
        if not rule:
            continue
        aliases = list(rule.get("receiveFrom") or {})
        if len(aliases) < 2:
            continue
        chain, _ = render_chain(spec, app, body, scenarios,
                                force_empty_hop=aliases[0])
        for alias, mname, payload in chain[1:]:
            got = (payload.get("variables") or {}).get("id", "<absent>")
            rep.check("L2", f"{body['action']}/{alias} narrows on a miss",
                      got == -1,
                      f"emitted id={got!r}; must be -1 so the filter still "
                      f"binds and matches nothing")

    # ---- transport-failure envelope --------------------------------------
    empty_ctx = base_context(app, {"action": "account.read",
                                   "correlationId": "probe"})
    env = spec["components"]["x-mappings"]["Sonar-Result-Envelope"][
        "mapping-template"]
    try:
        r = json.loads(JM.render(env, empty_ctx, "Sonar-Result-Envelope"))
        rep.check("L2", "unbound RECEIVED yields status ERROR",
                  r["status"] == "ERROR", f"status={r['status']}")
        rep.check("L2", "unbound RECEIVED yields null data",
                  r["data"] is None, f"data={r['data']!r}")
    except Exception as exc:
        rep.check("L2", "result envelope on transport failure", False,
                  str(exc)[:120])

    # ---- reject fixtures must never reach a document ---------------------
    for case in fixtures["reject"]:
        body = case["body"]
        if not isinstance(body.get("action"), str):
            continue
        if rule_for(spec, body["action"]) is None:
            continue
        # These are stopped by schema validation upstream of routing; this
        # check records that no reject fixture is *also* structurally renderable
        # into something dangerous, which would make a schema regression fatal.
        try:
            chain, _ = render_chain(spec, app, body, scenarios)
            doc = chain[-1][2]["query"]
            widened = ("(search: $search" in doc
                       and chain[-1][2]["variables"].get("search") in (None, []))
            rep.check("L2", f"reject fixture cannot widen: {case['note'][:48]}",
                      not widened,
                      "renders an unfiltered query if schema validation is lost")
        except Exception:
            rep.check("L2", f"reject fixture cannot widen: {case['note'][:48]}",
                      True)

    rep.stage_summary("L2")


# --------------------------------------------------------------------------
# L3 / L4 — against a running gateway
# --------------------------------------------------------------------------

def http(method, url, body=None, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:
        return 0, str(exc)


def stage_spec(rep, gateway, fixtures, admin_token):
    print(f"\n[L3 spec] {gateway}")
    hdr = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}

    spec_bytes = (ROOT / "sonar-crud-gateway.yaml").read_bytes()
    boundary = "----harness"
    part = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"sonar-crud-gateway.yaml\"\r\n"
            f"Content-Type: application/yaml\r\n\r\n").encode()
    payload = part + spec_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{gateway}/specification/upload", data=payload, method="POST",
        headers={**hdr,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, text = r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read().decode()
    except Exception as exc:
        code, text = 0, str(exc)
    # A 400 here is the definitive answer to G-18: remote $ref resolution.
    rep.check("L3", "spec upload accepted", code == 200, f"{code} {text[:220]}")

    for case in fixtures["accept"]:
        code, text = http("POST",
                          f"{gateway}/admin/validate?channel=sonar-crud-in",
                          case["body"], hdr)
        ok = code == 200 and json.loads(text or "{}").get("matched") is True
        rep.check("L3", f"validate accepts: {case['note'][:52]}", ok,
                  f"{code} {text[:160]}")

    for case in fixtures["reject"]:
        code, text = http("POST",
                          f"{gateway}/admin/validate?channel=sonar-crud-in",
                          case["body"], hdr)
        matched = json.loads(text or "{}").get("matched") if text else None
        rep.check("L3", f"validate rejects: {case['note'][:52]}",
                  matched is not True, f"{code} matched={matched}")
    rep.stage_summary("L3")


def stage_route(rep, gateway, webhook, mock, fixtures, hmac_secret):
    print(f"\n[L4 route] {webhook} -> {mock}")
    import hashlib
    import hmac as hmaclib

    code, _ = http("POST", f"{mock}/__reset", {})
    if not rep.check("L4", "mock reachable", code == 200, "mock_sonar not up"):
        rep.stage_summary("L4")
        return

    for case in fixtures["accept"]:
        body = case["body"]
        raw = json.dumps(body).encode()
        hdr = {"Content-Type": "application/json"}
        if hmac_secret:
            sig = hmaclib.new(hmac_secret.encode(), raw, hashlib.sha256).hexdigest()
            hdr["X-Signature-256"] = "sha256=" + sig
        req = urllib.request.Request(webhook, data=raw, headers=hdr,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, text = r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            code, text = exc.code, exc.read().decode()
        except Exception as exc:
            code, text = 0, str(exc)
        ok = code == 200 and body["correlationId"] in text
        rep.check("L4", f"ack echoes correlationId: {case['note'][:44]}", ok,
                  f"{code} {text[:140]}")

    time.sleep(2)                       # routing is @Async
    code, text = http("GET", f"{mock}/__recording")
    recording = json.loads(text)["requests"] if code == 200 else []
    rep.check("L4", "mock received traffic", bool(recording), "no requests")

    for entry in recording:
        rep.check("L4", f"document well-formed: {entry.get('operationName')}",
                  not entry.get("malformed"),
                  "gateway sent a body the stub could not parse")
        if entry.get("query"):
            try:
                gql_parse(entry["query"])
                ok, why = True, ""
            except Exception as exc:
                ok, why = False, str(exc)[:120]
            rep.check("L4", f"live document parses: {entry['operationName']}",
                      ok, why)
        rep.check("L4", f"bearer sent: {entry.get('operationName')}",
                  entry.get("auth", "").startswith("Bearer "), "no bearer token")

    ops = [e.get("operationName") for e in recording]
    three_hop = ["AccountServiceReadBySerialNumberSerialLookup",
                 "AccountServiceReadBySerialNumberItemLookup",
                 "AccountServiceReadBySerialNumberSonar"]
    if all(o in ops for o in three_hop):
        rep.check("L4", "three-hop chain ran in declaration order",
                  [o for o in ops if o in three_hop] == three_hop,
                  f"observed {[o for o in ops if o in three_hop]}")
    rep.stage_summary("L4")


# --------------------------------------------------------------------------
# L5 — read-only tenant probe
# --------------------------------------------------------------------------

def stage_probe(rep, sonar_url, token, scenarios):
    print(f"\n[L5 probe] {sonar_url} (read-only)")
    findings = {}
    for gap, spec in sorted(scenarios["introspection"].items()):
        if gap.startswith("_"):
            continue
        code, text = http("POST", sonar_url, {"query": spec["query"]},
                          {"Authorization": f"Bearer {token}",
                           "Accept": "application/json"})
        if code != 200:
            rep.check("L5", f"{gap} {spec['gap']}", False, f"{code} {text[:140]}")
            continue
        payload = json.loads(text)
        if payload.get("errors"):
            rep.check("L5", f"{gap} {spec['gap']}", False,
                      str(payload["errors"])[:160])
            continue
        findings[gap] = payload["data"]
        want = spec.get("expect_fields")
        if want:
            flat = json.dumps(payload["data"])
            missing = [w for w in want if f'"{w}"' not in flat]
            rep.check("L5", f"{gap} {spec['gap']}", not missing,
                      f"missing {missing} — the SSOT assumption is wrong")
        else:
            rep.check("L5", f"{gap} {spec['gap']}", True)
        if "writes_config" in spec:
            print(f"    -> set {spec['writes_config']} from: "
                  f"{json.dumps(payload['data'])[:180]}")
    (HERE / "probe-findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    print(f"    findings written to {HERE / 'probe-findings.json'}")
    rep.stage_summary("L5")


# --------------------------------------------------------------------------
# selftest — the harness must be able to fail
# --------------------------------------------------------------------------

def _drop_guard(spec, app, bundle):
    m = spec["components"]["x-mappings"][
        "InventoryItemReadBySerialNumber-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        " | default(-1, true)", "")


def _bad_root(spec, app, bundle):
    m = spec["components"]["x-mappings"]["AccountRead-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        "accounts(", "acounts(")


def _bad_argument(spec, app, bundle):
    m = spec["components"]["x-mappings"]["AccountServiceReadByAccountNumber-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        "account_id: $account_id", "acct_id: $account_id")


def _config_wrong_type(spec, app, bundle):
    app["sonar"]["identifiers"]["serialNumber"]["fieldId"] = "not-a-number"


def _config_missing(spec, app, bundle):
    del app["sonar"]["identifiers"]["accountNumber"]["inventoryAssigneeType"]


def _loosen_schema(spec, app, bundle):
    for k, d in bundle["definitions"].items():
        if k.endswith("Request"):
            d["additionalProperties"] = True
            d.pop("required", None)


def _envelope_always_ok(spec, app, bundle):
    m = spec["components"]["x-mappings"]["Sonar-Result-Envelope"]
    m["mapping-template"] = re.sub(r'"status": "\{\{.*?\}\}"',
                                   '"status": "OK"', m["mapping-template"])


def _business_conditional(spec, app, bundle):
    m = spec["components"]["x-mappings"]["AccountRead-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        '"variables":', '{% if DATA.fields %}{% endif %}"variables":')


def _undeclared_variable(spec, app, bundle):
    m = spec["components"]["x-mappings"]["InventoryItemReadByAccountNumber-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        '"variables": {', '"variables": { "bogus_arg": 1, ')


SELFTESTS = [
    ("narrowing guard removed", _drop_guard),
    ("root field misspelled", _bad_root),
    ("argument not on the SDL root", _bad_argument),
    ("CONFIG value resolves to the wrong type", _config_wrong_type),
    ("CONFIG key absent from application.yaml", _config_missing),
    ("request schemas unsealed", _loosen_schema),
    ("result envelope hardcodes OK", _envelope_always_ok),
    ("business conditional added to a template", _business_conditional),
    ("undeclared variable sent", _undeclared_variable),
]


def stage_selftest(rep, cfg, spec, bundle, app, fixtures, scenarios,
                   queries, mutations):
    import contextlib
    import copy
    import io

    print("\n[selftest] the harness must fail when the interface is broken")
    for name, mutate in SELFTESTS:
        ms, mb, ma = (copy.deepcopy(spec), copy.deepcopy(bundle),
                      copy.deepcopy(app))
        mutate(ms, ma, mb)
        probe = Report()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                stage_schema(probe, mb, fixtures)
                stage_render(probe, cfg, ms, ma, fixtures, scenarios,
                             queries, mutations)
        except Exception:
            probe.failures.append("raised")
        rep.check("selftest", f"detects: {name}", bool(probe.failures),
                  "harness stayed green on a broken interface")
    rep.stage_summary("selftest")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", action="append",
                    choices=["schema", "render", "spec", "route", "probe",
                             "selftest", "offline"],
                    help="repeatable; `offline` = schema + render (default)")
    ap.add_argument("--gateway", default=None,
                    help="e.g. https://gw.internal/event-gateway")
    ap.add_argument("--webhook", default=None,
                    help="e.g. https://gw.internal/event-gateway/webhooks/sonar/crud")
    ap.add_argument("--mock", default="http://127.0.0.1:8099")
    ap.add_argument("--sonar-url", default=None)
    ap.add_argument("--sonar-token", default=None)
    ap.add_argument("--admin-token", default=None)
    ap.add_argument("--hmac-secret", default=None)
    args = ap.parse_args()

    stages = set(args.stage or ["offline"])
    if "offline" in stages:
        stages |= {"schema", "render", "selftest"}

    cfg, spec, bundle, fixtures, scenarios, app, queries, mutations = load()
    rep = Report()

    if "selftest" in stages:
        stage_selftest(rep, cfg, spec, bundle, app, fixtures, scenarios,
                       queries, mutations)
    if "schema" in stages:
        stage_schema(rep, bundle, fixtures)
    if "render" in stages:
        stage_render(rep, cfg, spec, app, fixtures, scenarios, queries, mutations)
    if "spec" in stages:
        if not args.gateway:
            rep.check("L3", "gateway url supplied", False, "--gateway required")
        else:
            stage_spec(rep, args.gateway.rstrip("/"), fixtures, args.admin_token)
    if "route" in stages:
        if not args.webhook:
            rep.check("L4", "webhook url supplied", False, "--webhook required")
        else:
            stage_route(rep, args.gateway, args.webhook, args.mock.rstrip("/"),
                        fixtures, args.hmac_secret)
    if "probe" in stages:
        if not (args.sonar_url and args.sonar_token):
            rep.check("L5", "tenant credentials supplied", False,
                      "--sonar-url and --sonar-token required")
        else:
            stage_probe(rep, args.sonar_url, args.sonar_token, scenarios)

    online = stages & {"spec", "route", "probe"}
    if online:
        for problem in app.get("__placeholder_problems__", []):
            rep.check("PRE", "required env var set", False, problem)

    print("\n" + "=" * 68)
    total = len(rep.rows)
    print(f"HARNESS {'PASS' if not rep.failures else 'FAIL'} "
          f"({total - len(rep.failures)}/{total} checks, "
          f"stages: {','.join(sorted(stages - {'offline'}))})")
    for fail in rep.failures[:20]:
        print(f"  - {fail}")
    return 0 if not rep.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
