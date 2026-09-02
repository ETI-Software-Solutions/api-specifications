# HARNESS.md — testing the Sonar CRUD gateway interface

`audit.py` proves the spec is internally coherent. It has never rendered a
template or sent a request. This harness covers everything downstream of that.

Five stages, in increasing order of cost and of evidential weight. **Read the
"does not prove" column before you trust a green run.**

| Stage | Needs | Proves | Does **not** prove |
|---|---|---|---|
| `selftest` | nothing | the harness fails when the interface is broken | anything about the interface itself |
| `schema` (L1) | nothing | fixture bodies accept/reject exactly as intended | that a valid body produces a valid GraphQL document |
| `render` (L2) | nothing | templates render to JSON, documents parse as GraphQL and match the SDL, an unresolved id narrows, the envelope discriminates OK/ERROR | that **Jinjava** renders them the same way — L2 uses a *model* of Jinjava (§4) |
| `spec` (L3) | a gateway | the spec uploads, remote `$ref`s resolve, classification narrows every fixture to one message | that routing works, or that any document reaches Sonar |
| `route` (L4) | a gateway + `mock_sonar` | real Jinjava rendering, real `receiveFrom` chaining, hop ordering, bearer propagation, documents that parse | that **Sonar** accepts the documents — the stub validates nothing |
| `probe` (L5) | a Sonar tenant | the vendor assumptions in the SSOT are true; closes G-01/02/15/16/22/23/25 | anything about the gateway |

Nothing below L5 can tell you whether Sonar accepts a document. Nothing below
L4 can tell you what Jinjava does. Plan accordingly.

---

## 1. Quick start

```bash
pip install jinja2 graphql-core jsonschema pyyaml --break-system-packages

# offline: selftest + L1 + L2. No gateway, no tenant, ~2 seconds.
python3 harness/harness.py

# everything offline, plus a gateway
python3 harness/harness.py --stage offline --stage spec \
  --gateway https://gw.internal/event-gateway \
  --admin-token "$EEG_ADMIN_JWT"
```

Exit code is 0 only when every check in every selected stage passed. There is
no warning tier, by design: a check that is allowed to be yellow is a check
nobody reads.

---

## 2. Running each stage

### L1 / L2 — offline

```bash
python3 harness/harness.py --stage offline
```

Runs on a laptop with no credentials. This is the loop you should be in while
editing `sonar-crud.config.yaml`: `python3 build.py && python3 audit.py &&
python3 harness/harness.py`.

L2 reads the **real `application.yaml`** and resolves its Spring
`${VAR:default}` placeholders exactly as Spring would, so a `CONFIG` lookup the
spec makes with no backing property fails here rather than in production. It
also checks that a value landing in a JSON number position actually resolves to
a number — `SONAR_SERIAL_FIELD_ID=serial-field` would render bare into the
document and break it (gap G-27).

### L3 — spec acceptance against a gateway

```bash
python3 harness/harness.py --stage spec \
  --gateway https://gw.internal/event-gateway \
  --admin-token "$EEG_ADMIN_JWT"
```

Uploads the spec, then runs every fixture through
`POST /admin/validate?channel=sonar-crud-in`. Accept fixtures must come back
`matched: true`; reject fixtures must not.

**This stage is the definitive answer to gap G-18** — whether the validator
resolves an internal `#/definitions/tmf639.Characteristic` ref inside a
remotely fetched bundle. If the upload 400s naming a schema, rebuild with the
vendored definitions dereferenced inline. Run L3 before any deployment that
changes the schema bundle.

Point it at a scratch gateway. It replaces the running spec.

### L4 — routing, through the mock

```bash
# terminal 1
python3 harness/mock_sonar.py --port 8099 --record-to /tmp/sonar.jsonl

# point the gateway's sonar-api server host at the mock, then:
python3 harness/harness.py --stage route \
  --gateway https://gw.internal/event-gateway \
  --webhook https://gw.internal/event-gateway/webhooks/sonar/crud \
  --mock http://127.0.0.1:8099 \
  --hmac-secret "$SONAR_CRUD_WEBHOOK_SECRET"
```

Fires every accept fixture at the webhook with a valid HMAC, waits for the
async routing fan-out, then pulls `/__recording` and asserts on what the
gateway actually sent: every body parsed, every document parses as GraphQL,
every request carried a bearer, and the three-hop serial chain arrived in
declaration order.

L4 is the only stage that exercises real Jinjava. Anything L2 predicted that
L4 contradicts is a defect **in the model**, not in the gateway — fix
`jinjava_model.py` and add the divergence to its `BANNED` list.

### L5 — tenant probe, read-only

```bash
python3 harness/harness.py --stage probe \
  --sonar-url https://example.sonar.software/api/graphql \
  --sonar-token "$SONAR_READ_TOKEN"
```

Runs only introspection and one `inventory_model_fields` read. No mutation is
ever sent. Writes `harness/probe-findings.json` and prints the SSOT keys to
update:

| Gap | What it settles |
|---|---|
| G-01 | `AccountConnection` really has `entities` / `page_info` |
| G-02 | `SuccessResponse` really has `success` / `message` |
| G-15 | the serial-number `inventory_model_field_id` for this tenant |
| G-16 | the `InventoryitemableType` member naming an account assignee |
| G-22 | the `SearchOperator` spellings beyond the documented `EQ` / `LEQ` |
| G-23 | whether a datetime search bucket exists |
| G-25 | whether `integer_fields.search_value` is `Int` or `Int64Bit` |

Feed the findings into `sonar-crud.config.yaml` (shapes and dialect) and
`application.yaml` (tenant ids), then rebuild and re-audit. **Do not** edit the
generated spec.

---

## 3. The narrowing probe

If you read one section, read this one.

The failure mode is this: a serial that resolves to nothing leaves the
downstream id unbound, an unresolved Jinja reference renders as the empty
string, the `id:` argument disappears from the document, and a Sonar query with
no filter returns the whole table. A lookup miss silently becomes a full
extract.

L2 renders every multi-hop lookup a second time with the first hop's response
forced empty, then asserts the next hop emitted `id: -1` — a value that binds
the filter and matches nothing:

```
FAIL account_service.read.bySerialNumber/item_lookup narrows on a miss:
     emitted id=''; must be -1 so the filter still binds and matches nothing
```

`mock_sonar` mirrors it: the `id == -1` case returns an empty entity list, so
L4 sees the same behaviour end to end through real Jinjava.

Any new hop that reads from `RECEIVED` must ship with `| default(-1, true)`.
`audit.py` check G16 fails the build without it; this probe proves the guard
actually fires.

---

## 4. `jinjava_model.py` — what it is, and its limits

**It is not Jinjava.** It is a Jinja2 environment shimmed to reproduce the
handful of Jinjava 2.8.3 behaviours the templates depend on:

- a missing key, and any chain through one, renders as the empty string
- `{{ missing | tojson }}` renders the literal four characters `null`
- `default(x, true)` fires on any falsy value, not only on undefined

Every shim cites the behaviour it reproduces. Two rules keep it honest:

1. **No citation, no shim.** If a Jinjava behaviour cannot be cited, the
   construct goes in `BANNED` instead and the harness refuses to render.
2. **`assert_supported()` enforces the shared subset.** Anything outside it —
   `gset`, `for/else`, tuple unpacking, `is true`, `| safe`, `include`, macros
   — fails the render rather than being silently mis-modelled.

The allow-list also enforces the design rule from `SONAR-CRUD-GATEWAY.md` §11.1:
the **only** conditional a template may contain is the `{% if not loop.last %}`
separator idiom. Add a business `{% if %}` and the harness rejects it by
construction. That is selftest case 8.

A green L2 is a prediction. L4 is the evidence.

---

## 5. `mock_sonar.py` and scenario drift

The stub answers on `operationName`, records everything, and can branch on a
variable value — which is how the not-found path is driven. It validates
nothing.

`scenarios.json` is deliberately shared by L2 and L4, so the two stages cannot
disagree about response shapes. That is also its weakness: **if a shape in
`scenarios.json` is wrong, L2 and L4 are wrong together and both stay green.**
Only L5 catches that. So:

- Refresh scenario shapes from `probe-findings.json` after every L5 run.
- Treat the shapes tagged UNVERIFIED in the file header as provisional until
  G-01 and G-02 close.
- Never add a scenario that asserts Sonar *accepts* something. The stub cannot
  know that.

---

## 6. Selftest, and the rule for adding checks

`--stage selftest` breaks the interface nine ways in memory and asserts the
harness goes red each time: guard removed, root field misspelled, argument not
on the SDL root, CONFIG wrong type, CONFIG key missing, schemas unsealed,
envelope hardcoded to OK, business conditional added, undeclared variable sent.

**The rule: a new check ships with a new selftest case.** A check with no
matching mutant has never been observed failing, and an unobserved check is
indistinguishable from a check that cannot fail. This is the same discipline
`audit.py` applies with its 41 mutants, applied to the harness itself.

---

## 7. CI wiring

```yaml
# every commit — no credentials, ~5 seconds
- run: python3 build.py
- run: git diff --exit-code sonar-crud-gateway.yaml sonar-crud.schemas.json
- run: python3 audit.py
- run: python3 harness/harness.py --stage offline

# on merge to main, against the staging gateway
- run: python3 harness/mock_sonar.py --port 8099 &
- run: |
    python3 harness/harness.py --stage spec --stage route \
      --gateway "$STAGING_GATEWAY" --webhook "$STAGING_WEBHOOK" \
      --admin-token "$EEG_ADMIN_JWT" --hmac-secret "$WEBHOOK_SECRET"

# weekly, and before any tenant cutover
- run: |
    python3 harness/harness.py --stage probe \
      --sonar-url "$SONAR_URL" --sonar-token "$SONAR_READ_TOKEN"
```

The `git diff --exit-code` line matters: it fails the build if the generated
artifacts were hand-edited rather than rebuilt from the SSOT.

Schedule L5 weekly even when nothing changed. It is the only stage that
notices when the vendor changes something under you.

---

## 8. Using it improperly

Each of these has cost somebody a production incident on some project.

- **Treating L2 as proof.** It is a model of Jinjava. A construct outside the
  verified subset is refused, not approximated, precisely so you cannot mistake
  the model for the engine. Ship on L4.
- **Running L3 against production.** It uploads a spec. Use a scratch gateway.
- **Running L5 against production with a write-capable token.** The probe is
  read-only by construction, but the token you hand it need not be. Mint a
  read-only one.
- **Adding a mutation fixture to the accept set and running L4 against a real
  Sonar host.** The accept fixtures are fired for real. Against the mock this
  is free; against a tenant it writes. L4 points at `mock_sonar` for a reason.
- **Fixing a red harness by editing the generated spec.** The spec is an
  output. Edit `sonar-crud.config.yaml` or `application.yaml` and rebuild;
  otherwise the next `build.py` silently reverts your fix and CI's
  `git diff --exit-code` catches you late.
- **Letting a check go yellow.** There is no warning tier. If a check is not
  worth failing on, delete it.
- **Skipping selftest because it is "just testing the tests".** It is the only
  thing standing between you and a harness that passes because it stopped
  checking.

---

## 9. Adding a new action

1. Edit `sonar-crud.config.yaml` — a `lookups` entry, an `identifiers`
   binding, or a `ranges.entities` addition.
2. `python3 build.py` — never hand-edit the spec or the bundle.
3. Add fixtures to `fixtures.json`: at least one accept, and one reject per
   constraint the new action introduces.
4. Add a `scenarios.json` entry keyed by the emitted `operationName`. Include a
   miss case for any hop that feeds a downstream id.
5. `python3 audit.py` — add a mutant for any new invariant.
6. `python3 harness/harness.py` — add a selftest case for any new check.
7. L3 and L4 against staging before merge.

Steps 5 and 6 are not optional. An invariant with no mutant, and a check with
no selftest, are both just comments.
