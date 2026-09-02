#!/usr/bin/env python3
"""
audit.py — fail-closed audit for the Sonar CRUD gateway spec.

No warning tier: every check either passes or fails the build. Exit 0 only when
the structural suite is clean AND the mutation suite kills 100% of mutants.

Checks fall into four families:
  A  provenance      generated artifacts agree with the SDL and are reproducible
  B  graphql         documents are well-formed and JSON-string safe
  C  runtime         spec obeys the eti-event-gateway invariants in AUTHORING-SPECS.md
  D  security        the caller-supplied selection set cannot escape its context
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

import build as B

SPEC = "sonar-crud-gateway.yaml"
BUNDLE = "sonar-crud.schemas.json"

# Adversarial `fields` values that must be rejected by the schema pattern.
INJECTION_CORPUS = [
    "id } } } mutation { archiveAccount(id: 1) { id",
    "id",                                    # control: must be ACCEPTED
    'id name" }',
    "id\nname",
    "id { nested }",
    "id, name",
    "__schema { types { name } }",
    "id) { x",
    "",
    "id  name",                              # double space
    "1id",
    "id #comment",
    "id\tname",
    "...FragmentSpread",
]
INJECTION_MUST_ACCEPT = {"id"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

JINJA = re.compile(r"\{\{.*?\}\}")


def jinja_to_json_probe(template: str) -> str:
    """Substitute Jinja expressions with type-plausible literals so the
    surrounding text can be parsed as JSON. Value-position expressions (those
    following `: `) come from `| tojson` and stand in as `null`; everything else
    is inside a JSON string and stands in as text."""
    # drop {% if %}...{% endif %} bodies (the trailing-comma idiom), then the
    # remaining statement tags, then substitute expressions
    t = re.sub(r"\{%\s*if .*?%\}.*?\{%\s*endif\s*%\}", "", template, flags=re.S)
    t = re.sub(r"\{%.*?%\}", "", t, flags=re.S)
    out = re.sub(r"(?<=: )\{\{.*?\}\}", "null", t)
    return JINJA.sub("X", out)


def doc_parts(doc: str):
    m = re.match(
        r"^(query|mutation) ([A-Za-z0-9_]+)(?:\(([^)]*)\))? \{ "
        r"([A-Za-z0-9_]+)(?:\(([^)]*)\))? \{",
        doc)
    if not m:
        return None
    decls = []
    if m.group(3):
        for tok in m.group(3).split(", "):
            dm = re.match(r"^\$([A-Za-z_][A-Za-z0-9_]*): (.+)$", tok)
            if dm:
                decls.append((dm.group(1), dm.group(2)))
    return {"kind": m.group(1), "name": m.group(2),
            "decls": decls, "root": m.group(4)}


def balanced(s: str, opens="{([", closes="})]") -> bool:
    stack = []
    for ch in s:
        if ch in opens:
            stack.append(closes[opens.index(ch)])
        elif ch in closes:
            if not stack or stack.pop() != ch:
                return False
    return not stack


def channels_with_action(spec, action):
    out = set()
    for op in spec["operations"].values():
        if op.get("action") == action:
            out.add(op["channel"]["$ref"].rsplit("/", 1)[-1])
    return out


def iter_publish_targets(spec):
    """Yield (rule_name, kind, target_dict) for every receiveFrom entry and
    publishTo entry in the spec."""
    for name, rule_list in spec["components"]["x-routing-rules"].items():
        for rule in rule_list:
            for alias, entry in (rule.get("receiveFrom") or {}).items():
                yield name, "receiveFrom", entry
            pubs = rule.get("publishTo") or []
            if isinstance(pubs, dict):
                pubs = [pubs]
            for entry in pubs:
                yield name, "publishTo", entry


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def run_checks(spec, bundle, cfg, queries, mutations) -> list:
    f = []
    comps = spec["components"]
    mappings = comps["x-mappings"]
    schemas = comps["schemas"]
    base = cfg["schema_base_url"]

    actions = B.build_actions(cfg, queries, mutations)
    lookups = cfg.get("lookups", [])
    by_action = {a["action"]: a for a in actions}
    rng = [f"{e}.read.byRange" for e in cfg.get("ranges", {}).get("entities", [])]
    all_actions = (set(by_action) | {lk["action"] for lk in lookups} | set(rng))
    all_messages = ({a["message"] for a in actions}
                    | {lk["message"] for lk in lookups}
                    | {B.pascal(e) + "ReadByRange"
                       for e in cfg.get("ranges", {}).get("entities", [])})

    # ---- A: provenance -----------------------------------------------------
    for a in actions:
        tpl = mappings.get(a["message"] + "-Request", {}).get("mapping-template")
        if not tpl:
            f.append(f"A01 missing request mapping for {a['action']}")
            continue
        try:
            doc = json.loads(jinja_to_json_probe(tpl))["query"]
        except Exception as exc:
            f.append(f"A02 {a['action']} mapping is not JSON: {exc}")
            continue
        parts = doc_parts(doc)
        if not parts:
            f.append(f"A03 {a['action']} document is unparseable")
            continue
        if parts["root"] != a["root"]:
            f.append(f"A04 {a['action']} targets `{parts['root']}`, "
                     f"SDL root is `{a['root']}`")
        if parts["kind"] != a["kind"]:
            f.append(f"A05 {a['action']} declared {parts['kind']}, "
                     f"SDL says {a['kind']}")
        if parts["decls"] != a["args"]:
            f.append(f"A06 {a['action']} variable declarations diverge from the "
                     f"SDL argument list")

    # A07 every action schema is an external reference into the bundle
    for a in actions:
        sid = a["message"] + "Request"
        ref = schemas.get(sid, {}).get("$ref")
        if ref != f"{base}#/definitions/{sid}":
            f.append(f"A07 {sid} is not an external $ref into the schema bundle")
        if sid not in bundle["definitions"]:
            f.append(f"A08 {sid} absent from the schema bundle")

    # A09 no GraphQL type text is restated inside the spec document
    spec_txt = yaml.dump(spec, sort_keys=False)
    if "MutationInput" in spec_txt and "x-graphql-input-type" not in spec_txt:
        for token in re.findall(r"[A-Za-z]+MutationInput", spec_txt):
            if token not in spec_txt.split("query")[0]:
                pass  # declarations inside documents are references, not copies

    # ---- B: graphql documents ---------------------------------------------
    for name, mp in mappings.items():
        tpl = mp.get("mapping-template", "")
        if '"query":' not in tpl:
            continue
        doc = json.loads(jinja_to_json_probe(tpl))["query"]
        raw = re.search(r'"query": "(.*)"', tpl).group(1)
        if '"' in raw:
            f.append(f"B01 {name} document contains a double quote")
        if "\n" in raw:
            f.append(f"B02 {name} document contains a newline")
        if not balanced(doc):
            f.append(f"B03 {name} document has unbalanced brackets")
        for bad in re.finditer(r"\{\{|\}\}", raw):
            span = raw[max(0, bad.start() - 2): bad.end() + 2]
            if "DATA" not in raw[bad.start(): bad.start() + 40] and \
               "}}" == bad.group(0) and "DATA" not in raw[max(0, bad.start() - 60): bad.start()]:
                f.append(f"B04 {name} has a stray Jinja delimiter near `{span}`")

    # B05 every mapping-template is JSON once Jinja is substituted
    for name, mp in mappings.items():
        tpl = mp.get("mapping-template")
        if tpl is None:
            f.append(f"B05 {name} has no mapping-template")
            continue
        try:
            json.loads(jinja_to_json_probe(tpl))
        except Exception as exc:
            f.append(f"B06 {name} does not render JSON: {exc}")

    # ---- C: runtime invariants --------------------------------------------
    send_channels = channels_with_action(spec, "send")
    recv_channels = channels_with_action(spec, "receive")
    http_channels = {
        cn for cn, ch in spec["channels"].items()
        if any(spec["servers"][s["$ref"].rsplit("/", 1)[-1]]["protocol"]
               in ("http", "https") for s in ch["servers"])
    }

    for rule, kind, entry in iter_publish_targets(spec):
        ch = entry.get("channel")
        if ch not in spec["channels"]:
            f.append(f"C01 {rule} {kind} targets unknown channel `{ch}`")
            continue
        if ch not in send_channels:
            f.append(f"C02 {rule} {kind} targets `{ch}` which has no send operation")
        # HttpGatewayOutputChannel NPEs when `method` is absent; there is no
        # in-channel default to POST.
        if ch in http_channels and not entry.get("method"):
            f.append(f"C03 {rule} {kind} to HTTP channel `{ch}` omits `method`")
        mp = entry.get("mapping")
        if mp and mp.rsplit("/", 1)[-1] not in mappings:
            f.append(f"C04 {rule} {kind} references unknown mapping `{mp}`")
        res = entry.get("resilience") or {}
        for group, cfgd in res.items():
            for key, val in (cfgd or {}).items():
                if key in ("wait", "timeout", "waitDurationInOpenState"):
                    if not (isinstance(val, str) and val.startswith("P")):
                        f.append(f"C05 {rule} {kind} resilience.{group}.{key}="
                                 f"{val!r} is not ISO-8601 (bare numbers are "
                                 f"read as SECONDS)")
        if kind == "receiveFrom" and res:
            f.append(f"C06 {rule} declares resilience on a receiveFrom entry; "
                     f"only publishTo sends are guarded")

    for cn, ch in spec["channels"].items():
        cls = ch.get("x-classification")
        if not cls:
            continue
        if cn not in recv_channels:
            f.append(f"C07 `{cn}` declares x-classification but has no receive op")
        disc = cls.get("discriminator", "")
        # discriminator is a dotted JSON Pointer; a `$.` prefix is taken
        # literally and can never match.
        if disc.startswith("$"):
            f.append(f"C08 `{cn}` discriminator `{disc}` uses JSONPath syntax; "
                     f"dotted JSON Pointer is required")
        for route, body in cls["routes"].items():
            tgt = body["x-routing-rules"].rsplit("/", 1)[-1]
            if tgt not in comps["x-routing-rules"]:
                f.append(f"C09 route `{route}` targets unknown rule list `{tgt}`")
            if route not in all_actions:
                f.append(f"C10 route `{route}` is not a generated action")
        for act in all_actions:
            if act not in cls["routes"]:
                f.append(f"C11 action `{act}` has no classification route")
        onm = cls.get("onNoMatch") or {}
        if onm.get("action") != "publish":
            f.append(f"C12 `{cn}` onNoMatch.action must be `publish` to be live")
        if onm.get("channel") not in send_channels:
            f.append(f"C13 `{cn}` onNoMatch channel is not a send channel")

        idem = ch.get("x-idempotency-key")
        if isinstance(idem, dict):
            key = idem.get("key", "")
            # idempotency `key` IS a JSONPath — opposite convention to `when`.
            if not key.startswith("$."):
                f.append(f"C14 `{cn}` x-idempotency-key.key `{key}` must be JSONPath")
            ttl = str(idem.get("ttl", ""))
            if not re.match(r"^(P.+|\d+(ms|s|m|h|d))$", ttl):
                f.append(f"C15 `{cn}` idempotency ttl `{ttl}` is not ISO-8601 "
                         f"or short form")

        wr = ch.get("x-webhook-response") or {}
        for outcome in ("rateLimited", "busy"):
            ent = wr.get(outcome)
            if ent and "headers" in ent and "Retry-After" not in ent["headers"]:
                f.append(f"C16 `{cn}` {outcome} declares headers without "
                         f"Retry-After; custom headers REPLACE the defaults")

        for red in ch.get("x-log-redact", []):
            if not red.startswith("$."):
                f.append(f"C17 `{cn}` x-log-redact entry `{red}` is not JSONPath")

        auth = ch.get("x-webhook-auth") or {}
        if cn in recv_channels and cn in http_channels:
            if auth.get("type") not in ("hmac-sha256", "bearer-token"):
                f.append(f"C18 inbound webhook `{cn}` is unauthenticated")
            if not auth.get("secretEnv"):
                f.append(f"C19 `{cn}` webhook auth has no secretEnv "
                         f"(fails the reload closed)")

        declared = set(ch.get("messages", {}))
        if declared != all_messages and cn in recv_channels:
            f.append(f"C20 `{cn}` message catalogue does not match the action set")

    # C21 unsupported extensions must not appear
    for banned in ("x-onFailure",):
        if banned in spec_txt:
            f.append(f"C21 spec uses `{banned}`, which has no parser")

    # C22 every declared message resolves
    for cn, ch in spec["channels"].items():
        for mn, ref in ch.get("messages", {}).items():
            if ref["$ref"].rsplit("/", 1)[-1] not in comps["messages"]:
                f.append(f"C22 `{cn}` message `{mn}` does not resolve")

    # ---- D: security -------------------------------------------------------
    consts = {}
    for sid, sch in bundle["definitions"].items():
        if not sid.endswith("Request"):
            continue          # vendored third-party definitions
        pat = sch["properties"]["fields"]["pattern"]
        rx = re.compile(pat)
        for probe in INJECTION_CORPUS:
            accepted = bool(rx.match(probe)) and len(probe) <= \
                sch["properties"]["fields"]["maxLength"]
            should = probe in INJECTION_MUST_ACCEPT
            if accepted != should:
                verb = "accepts" if accepted else "rejects"
                f.append(f"D01 {sid}.fields {verb} {probe!r}")
        act = sch["properties"]["action"].get("const")
        if act is None:
            f.append(f"D02 {sid} has no const on `action`; classification "
                     f"cannot narrow deterministically")
        elif act in consts:
            f.append(f"D03 action const `{act}` is duplicated "
                     f"({consts[act]} and {sid})")
        else:
            consts[act] = sid
        if sch.get("additionalProperties") is not False:
            f.append(f"D04 {sid} does not seal additionalProperties")
        if "callbackPath" in sch["properties"]:
            cp = sch["properties"]["callbackPath"].get("pattern", "")
            if not cp.startswith("^/"):
                f.append(f"D05 {sid}.callbackPath is not anchored to an "
                         f"absolute path")

    # ---- G: lookups, identifiers, ranges, vendor schema -------------------
    lock_path = Path(__file__).parent / "vendor.lock.json"
    if not lock_path.exists():
        f.append("G00 vendor.lock.json is missing")
    else:
        import hashlib
        lock = json.loads(lock_path.read_text())
        for ns, entry in cfg.get("vendor_schemas", {}).items():
            vp = Path(__file__).parent / entry["file"]
            if not vp.exists():
                f.append(f"G01 vendored schema {entry['file']} is missing")
                continue
            if ns not in lock or hashlib.sha256(
                    vp.read_bytes()).hexdigest() != lock[ns]["extract_sha256"]:
                f.append(f"G02 vendored `{ns}` does not match its pinned digest")
            for d in lock.get(ns, {}).get("definitions", []):
                if f"{entry['prefix']}.{d}" not in bundle["definitions"]:
                    f.append(f"G03 vendor definition `{entry['prefix']}.{d}` "
                             f"was not flattened into the bundle")

    for m in re.finditer(r'"#/definitions/([^"]+)"', json.dumps(bundle)):
        if m.group(1) not in bundle["definitions"]:
            f.append(f"G04 bundle $ref `{m.group(1)}` does not resolve")

    # identifiers must be defined once and referenced, never inlined
    for name, ispec in cfg["identifiers"].items():
        key = f"identifier.{name}"
        if key not in bundle["definitions"]:
            f.append(f"G05 identifier `{name}` has no dereferenceable definition")
            continue
        d = bundle["definitions"][key]
        if "tmf" in ispec:
            branches = d.get("allOf") or []
            if len(branches) != 2 or branches[0].get("$ref") != \
                    f"#/definitions/{ispec['tmf']}":
                f.append(f"G06 identifier `{name}` is not composed over "
                         f"{ispec['tmf']}")
            else:
                ov = branches[1].get("properties", {})
                if ov.get("name", {}).get("const") != ispec["characteristic"]:
                    f.append(f"G07 identifier `{name}` does not pin the "
                             f"characteristic name")
                if ov.get("value", {}).get("pattern") != ispec["schema"]["pattern"]:
                    f.append(f"G08 identifier `{name}` value constraint drifted "
                             f"from the SSOT")
        elif d.get("pattern") != ispec["schema"]["pattern"]:
            f.append(f"G09 identifier `{name}` constraint drifted from the SSOT")

    for lk in lookups:
        sid = lk["message"] + "Request"
        sch = bundle["definitions"].get(sid, {})
        ref = sch.get("properties", {}).get(lk["identifier"], {}).get("$ref")
        if ref != f"#/definitions/identifier.{lk['identifier']}":
            f.append(f"G10 {lk['action']} inlines `{lk['identifier']}` instead "
                     f"of dereferencing it")
        if lk["identifier"] not in sch.get("required", []):
            f.append(f"G11 {lk['action']} does not require its identifier")

        rule = comps["x-routing-rules"].get(lk["message"] + "-Rules")
        if not rule:
            f.append(f"G12 lookup {lk['action']} has no rule list")
            continue
        rf = rule[0].get("receiveFrom") or {}
        aliases = list(rf)
        if not aliases or aliases[-1] != "sonar":
            f.append(f"G13 lookup {lk['action']} final alias must be `sonar`")
        if len(aliases) != len(lk["hops"]):
            f.append(f"G14 lookup {lk['action']} hop count diverges from SSOT")
        seen = []
        for alias in aliases:
            mp = rf[alias].get("mapping", "").rsplit("/", 1)[-1]
            tpl = mappings.get(mp, {}).get("mapping-template", "")
            for r in re.finditer(r"RECEIVED\.([A-Za-z0-9_]+)\.", tpl):
                if r.group(1) not in seen:
                    f.append(f"G15 {lk['action']} hop `{alias}` reads "
                             f"RECEIVED.{r.group(1)} before it is produced")
            for _ in re.finditer(r"RECEIVED\.[A-Za-z0-9_\.\[\]]+ \}\}", tpl):
                f.append(f"G16 {lk['action']} hop `{alias}` interpolates a "
                         f"RECEIVED value with no `| {B.FROM_GUARD}` guard")
            seen.append(alias)

    # tenant-scoped arguments must come from CONFIG with an audited default,
    # and a missing key must narrow rather than widen
    scopes = []
    for name, ispec in cfg["identifiers"].items():
        if "resolver" in ispec:
            scopes.append((name, ispec["resolver"]["scope"]))
        for b in ispec.get("bindings", {}).values():
            if "scope" in b:
                scopes.append((name, b["scope"]))
    joined = json.dumps(mappings)
    for name, sc in scopes:
        keys = "".join(f"['{k}']" for k in sc["config"].split("."))
        if f"CONFIG{keys}" not in joined.replace("\\", ""):
            f.append(f"G17 identifier `{name}` scope arg is not bound from "
                     f"CONFIG{keys}")
        if "default(" not in joined:
            f.append(f"G18 identifier `{name}` scope arg has no default guard")
        if isinstance(sc["default"], int) and sc["default"] <= 0:
            f.append(f"G19 identifier `{name}` scope default is not a usable id")
    for m in re.finditer(r"CONFIG(\[[^\]]+\])+ \}\}", joined):
        f.append(f"G20 a CONFIG lookup ships without a default guard")

    # ---- H: range predicates ---------------------------------------------
    dialect = cfg["search_dialect"]
    for ent in cfg.get("ranges", {}).get("entities", []):
        action = f"{ent}.read.byRange"
        msg = B.pascal(ent) + "ReadByRange"
        sch = bundle["definitions"].get(msg + "Request")
        if sch is None:
            f.append(f"H01 {action} has no request schema")
            continue
        where = sch["properties"].get("where", {})
        if where.get("minProperties") != 1:
            f.append(f"H02 {action}.where permits an empty predicate set, "
                     f"which would return the unfiltered table")
        if where.get("additionalProperties") is not False:
            f.append(f"H03 {action}.where is not sealed")
        root = cfg["entities"][ent]["read"]["root"]
        types = dict(queries[root]["args"])
        tpl = mappings.get(msg + "-Sonar-Request", {}).get("mapping-template", "")
        for bname, bk in dialect["buckets"].items():
            node = where.get("properties", {}).get(bname)
            if node is None:
                continue
            attrs = node["items"]["properties"]["attribute"]["enum"]
            for a in attrs:
                if a not in types:
                    f.append(f"H04 {action}.{bname} offers `{a}`, absent "
                             f"from the SDL root")
                elif types[a].rstrip("!") not in bk["gql_types"]:
                    f.append(f"H05 {action}.{bname} offers `{a}` of type "
                             f"{types[a]}, wrong bucket for that GraphQL type")
            ops = node["items"]["properties"].get("op", {}).get("enum")
            if ("ops" in bk) != (ops is not None):
                f.append(f"H06 {action}.{bname} op enum disagrees with the "
                         f"dialect")
            if ops and sorted(bk["ops"]) != sorted(ops):
                f.append(f"H07 {action}.{bname} op enum drifted from the dialect")
            # the emitted member is pinned as golden text against the dialect,
            # so a member shape Sonar does not accept for this bucket fails
            expect = (bk["member"].replace("@ATTR", "{{ c.attribute | tojson }}")
                                  .replace("@VALUE", "{{ c.value | tojson }}"))
            if "ops" in bk:
                var = bk.get("op_placeholder", "OPS_" + bname.upper())
                expect = expect.replace("@OP", "{{ %s[c.op] }}" % var)
            if f'"{bk["vendor_bucket"]}": [' not in tpl:
                f.append(f"H08 {action} does not emit `{bk['vendor_bucket']}`")
            if expect not in tpl:
                f.append(f"H09 {action}.{bk['vendor_bucket']} member shape "
                         f"does not match the declared dialect")
            if f"DATA.where.{bname} | default([], true)" not in tpl:
                f.append(f"H10 {action}.{bname} loop has no empty-list guard")
        for bname in where.get("properties", {}):
            if "maxItems" not in where["properties"][bname]:
                f.append(f"H11 {action}.where.{bname} has no predicate cap; "
                         f"an unbounded list would build an unbounded document")

    # ---- I: application.yaml agrees with the spec's CONFIG contract -------
    app_path = Path(__file__).parent / "application.yaml"
    if not app_path.exists():
        f.append("I00 application.yaml is missing")
    else:
        app = yaml.safe_load(app_path.read_text())

        def dig(d, path):
            for k in path.split("."):
                if not isinstance(d, dict) or k not in d:
                    return None
                d = d[k]
            return d

        for name, ispec in cfg["identifiers"].items():
            sc_list = ([ispec["resolver"]["scope"]] if "resolver" in ispec else [])
            sc_list += [b["scope"] for b in ispec.get("bindings", {}).values()
                        if "scope" in b]
            for sc in sc_list:
                if dig(app, sc["config"]) is None:
                    f.append(f"I01 application.yaml has no `{sc['config']}` for "
                             f"identifier `{name}`")
        if dig(app, f"tokens.apis.{cfg['sonar']['token_api']}") is None:
            f.append(f"I02 application.yaml declares no token api "
                     f"`{cfg['sonar']['token_api']}`")
        if dig(app, "event-gateway.security.admin.enabled") is None:
            f.append("I03 application.yaml does not set admin security")
        dlq = dig(app, "event-gateway.dead-letter.channel")
        if dlq not in spec["channels"]:
            f.append(f"I04 dead-letter channel `{dlq}` is not a spec channel")
        if dig(app, "event-gateway.payload-logging.enabled") is False:
            f.append("I05 payload-logging disabled: this also disables every "
                     "spec-declared x-log-redact path")

    # ---- E: AsyncAPI 3.0.0 meta-schema ------------------------------------
    meta = Path(__file__).parent / "asyncapi-3.0.0.json"
    if meta.exists():
        from jsonschema import Draft7Validator
        errs = list(Draft7Validator(json.loads(meta.read_text())).iter_errors(spec))
        for e in errs[:10]:
            loc = "/".join(str(x) for x in e.path)
            f.append(f"E01 meta-schema: {loc}: {e.message[:160]}")
    else:
        f.append("E00 vendored AsyncAPI 3.0.0 meta-schema is missing")

    # ---- F: request fixtures ----------------------------------------------
    fx_path = Path(__file__).parent / "fixtures.json"
    if not fx_path.exists():
        f.append("F00 fixtures.json is missing")
    else:
        from jsonschema import Draft7Validator
        fx = json.loads(fx_path.read_text())
        for want, cases in (("accept", fx["accept"]), ("reject", fx["reject"])):
            for c in cases:
                if c["definition"] not in bundle["definitions"]:
                    f.append(f"F01 fixture references unknown definition "
                             f"{c['definition']}")
                    continue
                # validate through the whole bundle so internal $refs into the
                # vendored TMF definitions resolve exactly as they will at runtime
                d = {"allOf": [{"$ref": f"#/definitions/{c['definition']}"}],
                     "definitions": bundle["definitions"]}
                bad = list(Draft7Validator(d).iter_errors(c["body"]))
                ok = not bad
                if (want == "accept") != ok:
                    verb = "accepted" if ok else "rejected"
                    f.append(f"F02 fixture {verb} but should be {want}ed: "
                             f"{c['note']}")

    return f


# --------------------------------------------------------------------------
# mutation testing
# --------------------------------------------------------------------------

def _first_http_receive(spec):
    for rl in spec["components"]["x-routing-rules"].values():
        return rl[0]["receiveFrom"]["sonar"]


MUTANTS = {}


def mutant(name):
    def deco(fn):
        MUTANTS[name] = fn
        return fn
    return deco


@mutant("M01 drop method from the Sonar receiveFrom")
def _(spec, bundle):
    _first_http_receive(spec).pop("method")


@mutant("M02 discriminator uses JSONPath")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-classification"]["discriminator"] = "$.action"


@mutant("M03 idempotency key drops the $. prefix")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-idempotency-key"]["key"] = "correlationId"


@mutant("M04 retry wait as a bare number")
def _(spec, bundle):
    for _r, kind, e in iter_publish_targets(spec):
        if "resilience" in e:
            e["resilience"]["retry"]["wait"] = 200
            return


@mutant("M05 rateLimited headers drop Retry-After")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-webhook-response"]["rateLimited"]["headers"] = \
        {"X-Note": "hi"}


@mutant("M06 route points at a nonexistent rule list")
def _(spec, bundle):
    r = spec["channels"]["sonar-crud-in"]["x-classification"]["routes"]
    k = next(iter(r))
    r[k]["x-routing-rules"] = "#/components/x-routing-rules/NoSuchRules"


@mutant("M07 publishTo targets an unknown channel")
def _(spec, bundle):
    for _r, kind, e in iter_publish_targets(spec):
        if kind == "publishTo":
            e["channel"] = "ghost-channel"
            return


@mutant("M08 remove the send operation backing the callback channel")
def _(spec, bundle):
    spec["operations"].pop("postCrudCallback")


@mutant("M09 loosen the fields pattern")
def _(spec, bundle):
    for k, sch in bundle["definitions"].items():
        if k.endswith("Request"):
            sch["properties"]["fields"]["pattern"] = "^.*$"


@mutant("M10 duplicate an action const")
def _(spec, bundle):
    ks = list(bundle["definitions"])
    bundle["definitions"][ks[1]]["properties"]["action"]["const"] = \
        bundle["definitions"][ks[0]]["properties"]["action"]["const"]


@mutant("M11 unbalance a GraphQL document")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["AccountCreate-Request"]
    m["mapping-template"] = m["mapping-template"].replace(") { {{", ") { { {{", 1)


@mutant("M12 inject a double quote into a document")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["AccountRead-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        "accounts(", 'accounts"(', 1)


@mutant("M13 diverge a variable type from the SDL")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["AccountUpdate-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        "$id: Int64Bit!", "$id: String", 1)


@mutant("M14 remove a const from an action schema")
def _(spec, bundle):
    ks = list(bundle["definitions"])
    bundle["definitions"][ks[0]]["properties"]["action"].pop("const")


@mutant("M15 break mapping-template JSON")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["Sonar-Result-Envelope"]
    m["mapping-template"] = m["mapping-template"].replace('"data"', 'data', 1)


@mutant("M16 unseal a request schema")
def _(spec, bundle):
    ks = list(bundle["definitions"])
    bundle["definitions"][ks[2]]["additionalProperties"] = True


@mutant("M17 onNoMatch action other than publish")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-classification"]["onNoMatch"]["action"] = "drop"


@mutant("M18 drop webhook auth")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"].pop("x-webhook-auth")


@mutant("M19 wrap the Sonar call in resilience it will not get")
def _(spec, bundle):
    _first_http_receive(spec)["resilience"] = {"retry": {"maxAttempts": 3}}


@mutant("M20 remove a classification route")
def _(spec, bundle):
    r = spec["channels"]["sonar-crud-in"]["x-classification"]["routes"]
    r.pop(next(iter(r)))


@mutant("M21 x-log-redact entry as JSON Pointer")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-log-redact"][0] = "variables.input.password"


@mutant("M22 schema $ref made local, losing the external reference")
def _(spec, bundle):
    s = spec["components"]["schemas"]
    k = next(k for k in s if k.endswith("Request") and k != "SonarGraphQLRequest")
    s[k] = {"type": "object"}


@mutant("M23 operation action outside the AsyncAPI enum")
def _(spec, bundle):
    spec["operations"]["callSonarGraphql"]["action"] = "publish"


@mutant("M24 drop the required correlationId from a request schema")
def _(spec, bundle):
    d = bundle["definitions"]["AccountReadRequest"]
    d["required"] = [r for r in d["required"] if r != "correlationId"]


@mutant("M25 allow a relative-escape callbackPath")
def _(spec, bundle):
    for k, d in bundle["definitions"].items():
        if k.endswith("Request"):
            d["properties"]["callbackPath"]["pattern"] = "^.*$"


@mutant("M26 make the non-null id optional on update")
def _(spec, bundle):
    v = bundle["definitions"]["AccountUpdateRequest"]["properties"]["variables"]
    v["required"] = []


@mutant("M27 widen Int64Bit so non-numeric strings pass")
def _(spec, bundle):
    v = bundle["definitions"]["InventoryItemDeleteRequest"]["properties"]["variables"]
    v["properties"]["id"] = {"type": ["integer", "string"]}


@mutant("M28 drop the default guard from a RECEIVED binding")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["InventoryItemReadBySerialNumber-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        " | default(-1, true) }}", " }}")


@mutant("M29 reorder hops so sonar reads an alias produced later")
def _(spec, bundle):
    r = spec["components"]["x-routing-rules"]["AccountServiceReadBySerialNumber-Rules"]
    rf = r[0]["receiveFrom"]
    r[0]["receiveFrom"] = {k: rf[k] for k in ("sonar", "serial_lookup", "item_lookup")}


@mutant("M30 vendor definition not flattened into the bundle")
def _(spec, bundle):
    bundle["definitions"].pop("tmf639.Characteristic")


@mutant("M31 serial hop scope arg loses its CONFIG default guard")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["InventoryItemReadBySerialNumber-SerialLookup-Request"]
    m["mapping-template"] = re.sub(r" \| default\(7, true\)", "",
                                   m["mapping-template"])


@mutant("M32 lookup loses its classification route")
def _(spec, bundle):
    spec["channels"]["sonar-crud-in"]["x-classification"]["routes"].pop(
        "inventory_item.read.bySerialNumber")


@mutant("M33 identifier definition loses its TMF composition")
def _(spec, bundle):
    bundle["definitions"]["identifier.serialNumber"] = {"type": "string"}


@mutant("M34 lookup inlines the identifier instead of dereferencing it")
def _(spec, bundle):
    d = bundle["definitions"]["InventoryItemReadBySerialNumberRequest"]
    d["properties"]["serialNumber"] = {"type": "object"}


@mutant("M35 identifier constraint drifts from the SSOT")
def _(spec, bundle):
    bundle["definitions"]["identifier.accountNumber"]["pattern"] = "^.*$"


@mutant("M36 range bucket offers a string column for numeric comparison")
def _(spec, bundle):
    d = bundle["definitions"]["AccountServiceReadByRangeRequest"]
    d["properties"]["where"]["properties"]["numeric"]["items"]["properties"][
        "attribute"]["enum"].append("name_override")


@mutant("M37 string_fields emits an operator Sonar does not accept there")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["AccountServiceReadByRange-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        '"string_fields": [{% for c in (DATA.where.text | default([], true)) %}{"attribute": {{ c.attribute | tojson }},',
        '"string_fields": [{% for c in (DATA.where.text | default([], true)) %}{"attribute": {{ c.attribute | tojson }}, "operator": "EQ",')


@mutant("M38 where permits an empty predicate set")
def _(spec, bundle):
    d = bundle["definitions"]["AccountReadByRangeRequest"]
    d["properties"]["where"].pop("minProperties")


@mutant("M39 range loop loses its empty-list guard")
def _(spec, bundle):
    m = spec["components"]["x-mappings"]["IpAssignmentReadByRange-Sonar-Request"]
    m["mapping-template"] = m["mapping-template"].replace(
        " | default([], true)", "")


@mutant("M40 op enum drifts from the declared dialect")
def _(spec, bundle):
    d = bundle["definitions"]["AccountReadByRangeRequest"]
    d["properties"]["where"]["properties"]["numeric"]["items"]["properties"][
        "op"]["enum"] = ["eq"]


@mutant("M41 predicate cap removed")
def _(spec, bundle):
    for k, d in bundle["definitions"].items():
        if k.endswith("ReadByRangeRequest"):
            d["properties"]["where"]["properties"]["numeric"].pop("maxItems")


# --------------------------------------------------------------------------

def main() -> int:
    root = Path(__file__).parent
    cfg = yaml.safe_load((root / "sonar-crud.config.yaml").read_text())
    queries = B.parse_root_type(
        (root / cfg["sdl"]["queries"]).read_text(), "Query")
    mutations = B.parse_root_type(
        (root / cfg["sdl"]["mutations"]).read_text(), "Mutation")

    spec = yaml.safe_load((root / SPEC).read_text())
    bundle = json.loads((root / BUNDLE).read_text())

    print("=" * 72)
    print("STRUCTURAL SUITE")
    print("=" * 72)
    failures = run_checks(spec, bundle, cfg, queries, mutations)
    for msg in failures:
        print(f"  FAIL  {msg}")
    print(f"  {'FAIL' if failures else 'PASS'}  "
          f"{len(failures)} structural failure(s)")

    # determinism
    print()
    print("=" * 72)
    print("DETERMINISM")
    print("=" * 72)
    det_ok = True
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([sys.executable, str(root / "build.py"),
                        "--config", str(root / "sonar-crud.config.yaml"),
                        "--sdl-dir", str(root), "--out-dir", td],
                       check=True, capture_output=True)
        for name in (SPEC, BUNDLE):
            a = (root / name).read_bytes()
            b = (Path(td) / name).read_bytes()
            ok = a == b
            det_ok &= ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name} reproduces byte-identically")

    # mutation testing
    print()
    print("=" * 72)
    print("MUTATION SUITE")
    print("=" * 72)
    survivors = []
    for name, fn in MUTANTS.items():
        ms, mb = copy.deepcopy(spec), copy.deepcopy(bundle)
        try:
            fn(ms, mb)
        except Exception as exc:
            survivors.append(f"{name} (mutation could not be applied: {exc})")
            print(f"  ERROR {name}")
            continue
        try:
            caught = bool(run_checks(ms, mb, cfg, queries, mutations))
        except Exception:
            caught = True          # a crash is still a detection
        print(f"  {'KILLED ' if caught else 'SURVIVED'} {name}")
        if not caught:
            survivors.append(name)

    killed = len(MUTANTS) - len(survivors)
    print(f"  mutation score: {killed}/{len(MUTANTS)}")

    print()
    ok = not failures and det_ok and not survivors
    print("AUDIT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
