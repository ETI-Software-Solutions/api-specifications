# Sonar GraphQL CRUD Facade on the ETI Event Gateway

## Artifact Set

| File | Role |
|---|---|
| `sonar-crud.config.yaml` | SSOT for entity-to-root-field mappings, hosts, and policy knobs. Only hand-edited file. |
| `build.py` | Deterministic generator: SDL + SSOT → schema bundle + spec. |
| `sonar-crud.schemas.json` | JSON Schema draft-07 bundle containing 16 request envelopes. External reference target. |
| `sonar-crud-gateway.yaml` | AsyncAPI 3.0.0 specification. |
| `fixtures.json` | Golden request fixtures: 6 accepted, 10 rejected. |
| `audit.py` | Fail-closed structural audit and determinism checks. |

```bash
python3 build.py && python3 audit.py
```

Current state: 0 structural failures; artifacts reproduce byte-identically; mutation score 27/27; AsyncAPI 3.0.0 meta-schema validation passes.

## GraphQL Schema Referencing

The gateway resolves `$ref` in `components.schemas` locally or from an absolute `http(s)` URL. The runtime external-reference mechanism is `RemoteSchemaResolver.resolveRemoteSchema`.

`$ref` cannot target a `.graphql` file. JSON Pointer requires a JSON/YAML document, and non-JSON schemas must be inlined as strings.

The SDL remains the source of truth. `build.py` generates JSON Schema from the SDL, and the AsyncAPI specification references the generated definitions:

```yaml
components:
  schemas:
    AccountUpdateRequest:
      $ref: 'https://schemas.etisoftware.com/sonar/1.0.0/sonar-crud.schemas.json#/definitions/AccountUpdateRequest'
```

The YAML does not duplicate the Sonar type system. Regenerating from the SDL updates the schema bundle and the 16 GraphQL documents together.

GraphQL documents are generated inline under `components.x-mappings`. They are not stored in Spring configuration.

## Runtime Architecture

`WebhookResponseRenderer.buildVars` binds exactly:

- `DATA`
- `EVENT_ID`
- `RETRY_AFTER`
- `ERROR`
- `REQUEST`

`RECEIVED` and `{% gset %}` values are unavailable to the webhook response renderer. Therefore, Sonar responses cannot be returned directly in the HTTP acknowledgement.

Flow:

```text
client
  POST /event-gateway/webhooks/sonar/crud
    HMAC-SHA256
    rate limit
    idempotency
    schema match
    action classification
        |
        v
receiveFrom sonar
  POST /api/graphql
        |
        v
publishTo (parallel)
  ├─ sonar-crud-results
  │    Kafka: gateway.sonar.crud.results
  └─ sonar-crud-callback
       HTTPS: retry + circuit breaker + time limiter

ack:
200 { correlationId, status: ACCEPTED, eventId }
```

`x-ack-style: sync` is used so `DATA` is available to the response renderer and the caller's `correlationId` can be echoed. On the async path, `DATA` is empty.

## Request Contract

All 16 actions use one envelope shape:

```json
{
  "action": "account.read",
  "correlationId": "prov-2026-09-02-0001",
  "requestedAt": "2026-09-02T14:00:00Z",
  "fields": "id name account_status_id",
  "variables": { "paginator": { "page": 1, "records_per_page": 25 } },
  "callbackPath": "/results/provisioning"
}
```

| Field | Contract |
|---|---|
| `action` | `<entity>.<read\|create\|update\|delete>`. Pinned with `const` in each schema. |
| `correlationId` | Required. Idempotency key at `$.correlationId`, TTL 1 hour, and result-channel correlation handle. |
| `requestedAt` | Request timestamp. |
| `fields` | Flat GraphQL selection set. Defaults to `id`. |
| `variables` | GraphQL variables. `additionalProperties: false` against the SDL argument list. |
| `callbackPath` | Callback-server path suffix. Must match `^/`. |

### Actions

| Action | GraphQL root | Returns |
|---|---|---|
| `account.read` / `.create` / `.update` / `.delete` | `accounts` / `createAccount` / `updateAccount` / `archiveAccount` | Connection / Account ×3 |
| `account_service.read` / `.create` / `.update` / `.delete` | `account_services` / `addServiceToAccount` / `updateAccountService` / `deleteAccountService` | Connection / AccountService ×2 / SuccessResponse |
| `inventory_item.read` / `.create` / `.update` / `.delete` | `inventory_items` / `createInventoryItems` / `updateInventoryItem` / `deleteInventoryItem` | Connection / [InventoryItem] / InventoryItem / SuccessResponse |
| `ip_assignment.read` / `.create` / `.update` / `.delete` | `ip_assignments` / `createIpAssignment` / `updateIpAssignment` / `deleteIpAssignment` | Connection / IpAssignment ×2 / SuccessResponse |

`account.delete` maps to `archiveAccount`; Sonar has no `deleteAccount` root field. Callers must not assume physical row deletion.

## Schema Validation Limits

The supplied SDL files contain only one top-level definition each: `type Query` and `type Mutation`. No input, object, scalar, or Connection type definitions are supplied.

The generated schemas validate:

- action discriminator
- argument names
- argument scalar types
- argument nullability

They do not validate the interior of `input:` objects. Those are emitted as:

```json
{
  "type": "object",
  "additionalProperties": true,
  "x-graphql-input-type": "CreateAccountMutationInput"
}
```

Full input validation requires the corresponding Sonar input type definitions.

## Selection Sets

`fields` is interpolated directly into generated GraphQL and is therefore constrained to a flat identifier list.

Schema pattern:

```text
^[A-Za-z_][A-Za-z0-9_]*( [A-Za-z_][A-Za-z0-9_]*)*$
```

Allowed:

- GraphQL identifiers
- single spaces between identifiers

Rejected:

- braces
- parentheses
- quotes
- colons
- commas
- newlines
- fragments
- nested selections

Nested selection sets are unsupported.

## Runtime Invariants

| Invariant | Requirement |
|---|---|
| HTTP `publishTo` / `receiveFrom` | `method` must be set. |
| Resilience durations | ISO-8601 duration format. |
| `receiveFrom` | Must not declare `resilience`. |
| `discriminator` | Uses the required dotted JSON Pointer form. |
| `x-idempotency-key.key` | Uses JSONPath. |
| Custom `rateLimited` headers | Must re-declare `Retry-After`. |
| `onNoMatch.action` | Must be `publish`. |
| Webhook authentication | Requires `secretEnv`. |
| Classification | Every action requires a classification route. |
| Match mode | `FIRST`; every request schema pins `action` with `const`. |

## Result Envelope

Results are published to `gateway.sonar.crud.results` and posted to the callback channel.

```json
{
  "correlationId": "prov-2026-09-02-0001",
  "action": "account.read",
  "status": "OK",
  "traceId": "...",
  "spanId": "...",
  "data": { "accounts": { "entities": [...], "page_info": {...} } },
  "errors": null
}
```

`{{ missing | tojson }}` renders `null`, allowing `data` and `errors` to be emitted as valid JSON.

`status` is `OK` when `RECEIVED.sonar.data` is defined and `errors` is empty. GraphQL-level errors and transport failures both produce `ERROR`; the envelope does not distinguish them.

## Targeted Lookups

### SDL Constraints

- `inventory_items` has no serial-number argument.
- Serial data is represented by `inventory_model_field_data`.
- `inventory_model_field_data` exposes `value: String` and `inventory_model_field_id: Int64Bit`.
- `account_services` exposes `account_id: Int64Bit`.
- `inventory_items` uses `inventoryitemable_type` and `inventoryitemable_id`.
- There is no `account_number` argument; the Sonar account number is the account `id`.

Lookup depth:

```text
account lookup: 1 hop
serial lookup: 2 hops
serial → service: 3 hops
```

### Lookup Actions

| Action | Hops | Chain |
|---|---:|---|
| `account_service.read.byAccount` | 1 | `account_services(account_id:)` |
| `inventory_item.read.byAccount` | 1 | `inventory_items(inventoryitemable_id:, inventoryitemable_type: Account)` |
| `inventory_item.read.bySerial` | 2 | `inventory_model_field_data(value:, inventory_model_field_id: N)` → `inventory_items(id:)` |
| `account_service.read.bySerial` | 3 | field data → `inventory_items(id:)` → `account_services(id:)` |

`receiveFrom` entries execute in declaration order. Each entry can reference only `RECEIVED.<alias>` values produced by earlier entries.

### Miss Protection

An unresolved cross-hop reference renders as an empty string. Removing an `id:` filter can widen a Sonar query to the entire table.

All cross-hop bindings therefore use:

```jinja
"id": {{ RECEIVED.serial_lookup.data.inventory_model_field_data.entities[0].inventory_item_id | default(-1, true) }}
```

`-1` matches no row. The `true` argument applies the default to falsy values as well as undefined values.

The serial lookup pins `inventory_model_field_id` to the tenant's serial-number field. The value must be configured per tenant.

## Reuse Strategy

### Build-Time Polymorphism

Selectors are separate actions, schemas, GraphQL document chains, and classification routes. Templates remain branch-free.

### Selector-Level Reuse

Selectors are modelled independently of vendors. The SSOT `lookups` block defines hops and arguments; `build.py` derives GraphQL types from each vendor schema.

### TMF639 Characteristic

`TMF639 Characteristic` has the structure `{name, value, valueType}` with `name` and `value` required. The serial selector composes that definition and pins the name:

```json
{
  "allOf": [
    { "$ref": "#/definitions/tmf639.Characteristic" },
    {
      "required": ["name", "value"],
      "properties": {
        "name": { "const": "serialNumber" },
        "value": {
          "type": "string",
          "pattern": "^[A-Za-z0-9._:-]{1,128}$"
        }
      }
    }
  ]
}
```

Changing `serialNumber` to `macAddress` reuses the same base model with a different pinned name.

### External Schema Vendoring

Runtime schema resolution uses an unauthenticated, uncached HTTPS GET. To avoid runtime dependency on upstream availability, `build.py`:

1. fetches the upstream schema;
2. extracts the transitive closure of required definitions;
3. rewrites internal references into the project namespace;
4. inlines the definitions into `sonar-crud.schemas.json`.

The runtime then dereferences one remote `$ref` into the published bundle.

`vendor.lock.json` records:

- source URL
- upstream SHA-256
- extracted SHA-256
- definitions included
- fetch date

`build.py` verifies the extracted digest and fails on drift. `--refresh-vendor` performs an explicit vendor refresh.

## Range Queries

Sonar `Search` supports:

- `integer_fields`: `{attribute, operator, search_value}`
- `string_fields`: `{attribute, search_value, match, partial_matching}`
- `boolean_fields`: `{attribute, search_value}`

Same-attribute predicates OR; different attributes AND.

`<entity>.read.byRange` is generated for all four entities.

Example:

```json
{
  "action": "account_service.read.byRange",
  "correlationId": "prov-0001",
  "fields": "id data_usage_percentage",
  "where": {
    "numeric": [
      { "attribute": "data_usage_percentage", "op": "gte", "value": 80 },
      { "attribute": "data_usage_percentage", "op": "lte", "value": 100 }
    ],
    "text": [
      { "attribute": "name_override", "op": "contains", "value": "Fiber" }
    ],
    "flag": [
      { "attribute": "is_additional", "value": true }
    ]
  }
}
```

Bucket mapping is derived from SDL argument types:

| SDL type | Bucket |
|---|---|
| `Int64Bit`, `Int`, `Float`, `Numeric` | `numeric` |
| `String`, `ID` | `text` |
| `Boolean` | `flag` |

The `attribute` value is restricted to columns of the corresponding type.

Range constraints:

- `where` requires at least one property.
- Each bucket allows at most 25 predicates.
- Each bucket uses `default([], true)` when omitted.
- Date/datetime ranges are not currently supported.

The range template uses bounded loops because predicate counts are caller-supplied. Operator translation is performed through the SSOT operator map.

## Identifiers

`accountNumber` and `serialNumber` are declared once in the SSOT `identifiers` block.

| Consumer | Resolution |
|---|---|
| Request schemas | `$ref` to the identifier definition |
| Schema bundle | One definition per identifier |
| Lookup hops | `args: { "@identifier": accountNumber }` |
| `application.yaml` | `sonar.identifiers.<name>.<scope>` |

`accountNumber` has different per-entity bindings because Sonar has no `account_number` field:

- `accounts.id`
- `account_services.account_id`
- `inventory_items.inventoryitemable_id`

Adding another vendor requires a `bindings:` entry rather than a new schema.

## Tenant Configuration

Tenant-specific values are GraphQL variables bound from `CONFIG`, with SSOT fallbacks:

```jinja
"inventory_model_field_id": {{ CONFIG['sonar']['identifiers']['serialNumber']['fieldId'] | default(7, true) }}
"inventoryitemable_type": "{{ CONFIG['sonar']['identifiers']['accountNumber']['inventoryAssigneeType'] | default('Account', true) }}"
```

Configuration:

```text
sonar.identifiers.serialNumber.fieldId
sonar.identifiers.accountNumber.inventoryAssigneeType
```

The specification is tenant-portable across environments.

`CONFIG` is bound from the Spring `Environment` during rendering. Startup configuration is available; hot rebinding through `POST /actuator/refresh` is not verified.

The audit cross-validates:

- referenced configuration paths;
- token API declaration;
- dead-letter channel resolution;
- `payload-logging.enabled: false` prohibition.

## Deployment

```bash
export SONAR_CRUD_WEBHOOK_SECRET=...
# tokens.apis.sonar.{authUrl,clientId,clientSecret} in external configuration

curl -F file=@sonar-crud-gateway.yaml   https://gateway.etisoftware.com/event-gateway/specification/upload
```

The upload validates in three stages and leaves the live machine unchanged on failure. `POST /specification/rollback` restores the previous good specification from a five-spec ring.

Mounted endpoint:

```text
/event-gateway/webhooks/sonar/crud
```

Path components:

```text
context-path: /event-gateway
callback-sub-path: /
server pathname: /webhooks
channel address: /sonar/crud
```

Monitor:

```text
eventgateway.inbound.events{channel=sonar-crud-in}
eventgateway.output.send.duration{output_type=http}
```

A rise in `no_schema_match` after an SDL change indicates schema/spec drift. Rerun:

```bash
python3 build.py && python3 audit.py
```

## Test Harness

`audit.py` verifies static consistency but does not render templates or send requests. Runtime testing is under `harness/`.

| Stage | Requirement | Coverage |
|---|---|---|
| `selftest` | None | Harness integrity |
| `schema` (L1) | None | Fixture acceptance/rejection |
| `render` (L2) | None | Jinjava rendering, SDL validation, narrowing, envelope discrimination |
| `spec` (L3) | Gateway | Spec upload, remote `$ref` resolution, classification |
| `route` (L4) | Gateway + `mock_sonar` | Real Jinjava, `receiveFrom` chaining, hop ordering |
| `probe` (L5) | Tenant | Tenant/vendor-specific verification |

Current offline state: 374/374 checks, approximately 2 seconds, no credentials required.

The harness verifies that:

- `CONFIG` uses resolved Spring properties rather than literal `${VAR:default}` text;
- numeric `CONFIG` values render as numbers when inserted into JSON numeric positions;
- the Jinjava model implements only the verified shared subset and refuses unsupported constructs.

A non-numeric tenant override in a numeric `CONFIG` position breaks the generated GraphQL document and is caught by L2 before deployment.

**[FACT]** `WebhookResponseRenderer.buildVars` (`:83-99`) binds exactly five
variables: `DATA`, `EVENT_ID`, `RETRY_AFTER`, `ERROR`, `REQUEST`. Neither
`RECEIVED` nor `{% gset %}` values are available.

**Consequence:** the Sonar response *cannot* ride back on the HTTP response to
the caller. A CRUD facade on this runtime is necessarily asynchronous
request/response. Read that again before designing a client against it.

**[DESIGN]** The flow is therefore:

```
client ──POST /event-gateway/webhooks/sonar/crud──> sonar-crud-in
            │  hmac-sha256 → rate limit → idempotency → schema match
            │  x-classification discriminator: action  (16 routes)
            ▼
       receiveFrom sonar ──POST──> Sonar /api/graphql
            │  RECEIVED.sonar = parsed GraphQL response body
            ▼
       publishTo (parallel)
            ├─> sonar-crud-results   (Kafka gateway.sonar.crud.results)
            └─> sonar-crud-callback  (HTTPS, retry+CB+timeLimiter)

client ack: 200 { correlationId, status: ACCEPTED, eventId }
```

**[DESIGN]** `x-ack-style: sync` rather than the `async` default, purely so `DATA`
is bound in the response renderer and the caller's own `correlationId` can be
echoed on the ack. **[FACT]** on the async path `DATA` is an empty object, so a
202 could only return the gateway's `EVENT_ID`, which the caller has no way to
correlate.

---

## 4. Request contract

One envelope shape across all 16 actions:

```json
{
  "action": "account.read",
  "correlationId": "prov-2026-09-02-0001",
  "requestedAt": "2026-09-02T14:00:00Z",
  "fields": "id name account_status_id",
  "variables": { "paginator": { "page": 1, "records_per_page": 25 } },
  "callbackPath": "/results/provisioning"
}
```

| Field | Notes |
|---|---|
| `action` | `<entity>.<read\|create\|update\|delete>`. Pinned with `const` in each schema. |
| `correlationId` | Required. Doubles as the idempotency key (`$.correlationId`, TTL 1h) and the result-channel correlation handle. |
| `fields` | Flat GraphQL selection set. See §5. Defaults to `id`. |
| `variables` | GraphQL variables, sealed with `additionalProperties: false` against the SDL argument list. |
| `callbackPath` | Path suffix on the callback server. Anchored to `^/`. |

### 4.1 Action table

Every row is derived from the SDL, not from memory.

| Action | GraphQL root | Returns |
|---|---|---|
| `account.read` / `.create` / `.update` / `.delete` | `accounts` / `createAccount` / `updateAccount` / `archiveAccount` | Connection / Account ×3 |
| `account_service.read` / `.create` / `.update` / `.delete` | `account_services` / `addServiceToAccount` / `updateAccountService` / `deleteAccountService` | Connection / AccountService ×2 / SuccessResponse |
| `inventory_item.read` / `.create` / `.update` / `.delete` | `inventory_items` / `createInventoryItems` / `updateInventoryItem` / `deleteInventoryItem` | Connection / [InventoryItem] / InventoryItem / SuccessResponse |
| `ip_assignment.read` / `.create` / `.update` / `.delete` | `ip_assignments` / `createIpAssignment` / `updateIpAssignment` / `deleteIpAssignment` | Connection / IpAssignment ×2 / SuccessResponse |

**[DESIGN]** `account.delete` maps to `archiveAccount`, not a hard delete —
Sonar exposes no `deleteAccount` root field, and archive is the closest
semantic. Callers should not assume row removal.

### 4.2 What the schemas can and cannot validate

**[FACT]** Both uploaded SDL files contain exactly one top-level definition each
(`type Query`, `type Mutation`). There are no input, object, scalar or Connection
type definitions in the corpus.

So the generated schemas validate, with full fidelity: the action discriminator,
argument *names*, argument *scalar types*, and argument *nullability* — all read
straight off the root-field signatures. They cannot validate the interior of
`input:` objects, which are emitted as:

```json
{ "type": "object", "additionalProperties": true,
  "x-graphql-input-type": "CreateAccountMutationInput" }
```

That is deliberate. Guessing at `CreateAccountMutationInput`'s fields would be
speculation presented as validation; Sonar rejects malformed input authoritatively
and the error surfaces on the result channel. The `x-graphql-input-type` annotation
records which definition would need to be supplied to close the gap (**G-14**).

---

## 5. Selection-set injection, and why `fields` is flat

`fields` is interpolated directly into the GraphQL document, which is an
injection surface. A value like

```
id } } } mutation { archiveAccount(id: 1) { id
```

would close the query and append a destructive mutation.

**[DESIGN]** The schema pattern is
`^[A-Za-z_][A-Za-z0-9_]*( [A-Za-z_][A-Za-z0-9_]*)*$` — single-space-separated
identifiers, nothing else. No braces, parens, quotes, colons, commas, newlines or
`...`. That makes escape structurally impossible rather than filtered-against.
The trade is that **nested selection sets are unsupported** (**G-09**).

`audit.py` check **D01** runs a 14-case adversarial corpus against every one of
the 16 definitions and fails the build if any is accepted, or if the control case
`id` is rejected. Mutant **M09** confirms that loosening the pattern to `^.*$`
is caught.

---

## 6. Runtime invariants encoded in the audit

Each of these is a way the spec would fail silently in production. Every one has
a check and a mutant.

| # | Invariant | Source | Mutant |
|---|---|---|---|
| C03 | HTTP `publishTo`/`receiveFrom` must set `method` | **[FACT]** no in-channel default; `HttpUriRequestBase` NPEs at `HttpGatewayOutputChannel.java:241` | M01 |
| C05 | Resilience durations must be ISO-8601 | **[FACT]** Jackson reads bare numbers as *seconds*; `wait: 200` = 3m20s | M04 |
| C06 | `receiveFrom` must not declare `resilience` | **[FACT]** only `publishTo` sends are guarded — declaring it is a false comfort | M19 |
| C08 | `discriminator` is a dotted JSON Pointer | **[FACT]** `$.action` becomes pointer `/$/action` and never matches | M02 |
| C14 | `x-idempotency-key.key` **is** JSONPath | **[FACT]** opposite convention to `when`/`discriminator` in the same file | M03 |
| C16 | Custom `rateLimited` headers must re-declare `Retry-After` | **[FACT]** custom headers replace, not merge | M05 |
| C12 | `onNoMatch.action` must be `publish` | **[FACT]** any other value makes the block inert | M17 |
| C19 | Webhook auth needs `secretEnv` | **[FACT]** the only extension that fails the reload *closed* | M18 |
| C11 | Every action has a classification route | **[FACT]** no rules → `CLASSIFICATION_NO_MATCH` → DLQ | M20 |

**[DESIGN]** `matchMode: FIRST` rather than the `ALL` default. Every request
schema pins `action` with a `const`, so at most one candidate can validate and
first-match is order-insensitive — but it avoids validating each body against all
16 schemas. Audit check **D02** fails the build if any schema loses its `const`,
because that assumption is what makes `FIRST` safe.

---

## 7. Result envelope

Published to `gateway.sonar.crud.results` and POSTed to the callback channel:

```json
{
  "correlationId": "prov-2026-09-02-0001",
  "action": "account.read",
  "status": "OK",
  "traceId": "...", "spanId": "...",
  "data": { "accounts": { "entities": [...], "page_info": {...} } },
  "errors": null
}
```

**[FACT]** `{{ missing | tojson }}` renders the literal four characters `null`,
which is what lets `data` and `errors` be emitted unguarded and still produce
valid JSON.

**[DESIGN]** `status` is `OK` only when `RECEIVED.sonar.data is defined` *and*
the `errors` array is empty. A GraphQL-level failure (Sonar returns HTTP 200 with
`errors[]`) and a transport failure (`RECEIVED.sonar` never bound) both land as
`ERROR`. They are not distinguishable in the envelope — see **G-07**.

---

## 8. Gap register

Blocking gaps must be closed before this reaches a production Sonar tenant.

| # | Severity | Gap | Close it by |
|---|---|---|---|
| **G-01** | **BLOCKING** | **[UNVERIFIED]** The Connection selection wrapper `entities { … } page_info { records_per_page page page_count total_count }` is not derivable from the corpus — no `*Connection` or `PageInfo` type definitions were supplied. Wrong subfields fail all four read actions. | Run `{ __type(name: "AccountConnection") { fields { name } } }` against the tenant; correct `connection_wrapper` in the SSOT and rebuild. |
| **G-02** | **BLOCKING** | **[UNVERIFIED]** `SuccessResponse` selection `success message`. Affects the three `delete` actions returning it. | Same introspection route, `__type(name: "SuccessResponse")`. |
| **G-03** | **BLOCKING** | **[UNVERIFIED]** `TOKEN.sonar.accessToken` assumes Sonar issues OAuth2 `client_credentials` tokens. If the tenant only supports static personal access tokens, `TokenService` cannot drive it. | Confirm with Sonar; if PAT-only, the token must move to an env-var-backed mechanism, which the gateway does not currently expose to templates — that becomes a Java change. |
| **G-04** | non-blocking | **[FACT]** `RemoteSchemaResolver` does a plain GET with no auth and no cache. The bundle host must be reachable from the pod on every schema compile, and unauthenticated. | Serve the bundle from an in-cluster static origin, not the public internet. |
| **G-05** | non-blocking | **[FACT]** `receiveFrom` is not wrapped by Resilience4j and failures are not written to `FailureSink`. The Sonar call — the entire point of the facade — has no retry, circuit breaker or timeout, and `POST /admin/replay` cannot recover it. | Accepted. Mitigated by caller-side retry on `correlationId` plus the 1h idempotency window. Monitor the `Send failed for: sonar-graphql` span event. |
| **G-06** | non-blocking | **[FACT]** Idempotency is in-process (`CaffeineIdempotencyStore`); duplicates are not suppressed across replicas. | Accepted at current replica count. Revisit if scaled out. |
| **G-07** | non-blocking | Transport failure and an empty Sonar body both yield an unbound `RECEIVED.sonar`. **[FACT]** the alias binds the parsed body only, never the `{statusCode, success, …}` envelope. | Correlate the result-channel `ERROR` against the `output.send.duration` timer tagged `output=sonar-graphql`. |
| **G-08** | non-blocking | **[UNVERIFIED]** whether `DiscriminatorConstExtractor` resolves `const` through a *remote* `$ref`. If not, the index is absent. | Fails open to a full 16-message scan — correctness unaffected, cost only. Confirm via `POST /admin/diagnose`. |
| **G-09** | non-blocking | **[DESIGN]** Nested selection sets unsupported by construction (§5). | Add named server-side persisted queries, or extend the SSOT with per-entity curated nested selections once G-01 lands. |
| **G-10** | non-blocking | **[FACT]** `AUTHORING-SPECS.md` §4.1 and §12.1 give different outbound-URL formulas (`pathname` + `address` vs `pathname`/`address`). | Mitigated: the full path sits on `channel.address` with `server.pathname` unset, which yields `/api/graphql` under either reading. Confirm with one smoke POST. |
| **G-11** | non-blocking | **[FACT]** Sonar payloads cannot return on the webhook response (§3). Architectural. | Not closable in spec. Documented in the client contract. |
| **G-12** | non-blocking | **[UNVERIFIED]** In Jinjava 2.8.3, `RECEIVED.sonar.data is defined` over a fully-absent chain, and `| default([])` supplying a list default. | Add a `JinjavaAuthoringResearchTests` case for both; until then treat `status` as advisory and trust `errors`/`data` directly. |
| **G-13** | non-blocking | **[UNVERIFIED]** whether `CONFIG` rebinds on `POST /actuator/refresh` (§2.1). Only matters if the rejected option is revisited. | Test with a ConfigMap change plus refresh. |
| **G-14** | non-blocking | **[FACT]** `input:` object interiors are unvalidated at the gateway because their type definitions are absent from the corpus. | Supply the full Sonar SDL (all `input` types); `build.py` will emit real property schemas with no code change beyond extending `gql_to_schema`. |

---

## 9. Deployment

```bash
export SONAR_CRUD_WEBHOOK_SECRET=...        # fails the reload closed if unset
# tokens.apis.sonar.{authUrl,clientId,clientSecret} in the external config source

curl -F file=@sonar-crud-gateway.yaml \
     https://gateway.etisoftware.com/event-gateway/specification/upload
```

**[FACT]** Upload validates in three stages and leaves the live machine untouched
on any failure; `POST /specification/rollback` restores the previous good spec
(ring of 5).

Endpoint after mount: `/event-gateway/webhooks/sonar/crud` — context-path `/event-gateway`,
callback-sub-path `/`, server pathname `/webhooks`, channel address `/sonar/crud`.

Watch `eventgateway.inbound.events{channel=sonar-crud-in}` split by `match_status`,
and `eventgateway.output.send.duration{output_type=http}`. A rising
`no_schema_match` after an SDL bump means the bundle and the spec drifted —
rerun `build.py` and `audit.py`.

---

## 10. Targeted lookups: serial, account, conditionals

### 10.1 What the SDL actually permits

**[FACT]** `inventory_items` has **no serial-number argument**. Its 34 root
arguments carry ids, statuses and geo, nothing device-identifying. Serial numbers
live one table over, in `inventory_model_field_data` — the SDL's own comment on
that root reads "Data stored on an inventory field (e.g. a MAC address or serial
number.)" (`sonar-queries.graphql:7265`), and it exposes `value: String` plus
`inventory_model_field_id: Int64Bit` as top-level arguments.

**[FACT]** `account_services` **does** expose `account_id: Int64Bit` directly.
`inventory_items` does not; it carries the polymorphic pair
`inventoryitemable_type: InventoryitemableType` / `inventoryitemable_id`.

**[FACT]** There is no `account_number` argument anywhere. Sonar's account number
is the account `id`.

So: account lookup is one hop, serial lookup is two, and serial → service is three.

### 10.2 The four new actions

| Action | Hops | Chain |
|---|---|---|
| `account_service.read.byAccount` | 1 | `account_services(account_id:)` |
| `inventory_item.read.byAccount` | 1 | `inventory_items(inventoryitemable_id:, inventoryitemable_type: Account)` |
| `inventory_item.read.bySerial` | 2 | `inventory_model_field_data(value:, inventory_model_field_id: N)` → `inventory_items(id:)` |
| `account_service.read.bySerial` | 3 | field data → `inventory_items(id:)` → `account_services(id:)` |

**[FACT]** Multi-hop works because `receiveFrom` entries run in declaration order
on one thread and each entry's templates see `RECEIVED.<alias>` from *prior*
entries only (`EventRoutingServiceImpl.receiveFrom:130-173`). Audit check **G08**
fails the build if any hop reads an alias produced later; mutant **M29** proves it.

The pre-existing `<entity>.read` action is unchanged and remains the conditional
form — equality across any typed root argument, plus `general_search` /
`general_search_mode` for partial text. **[FACT]** Richer predicates (ranges, IN,
LIKE) require `search: [Search]`, and `Search` is not in the SDL corpus, so it is
passed opaquely and cannot be validated at the gateway (**G-14**).

### 10.3 The failure mode that shapes hop 2

A serial that resolves to nothing leaves the upstream id unbound. **[FACT]** an
unresolved Jinja reference renders as the empty string, which would delete the
`id:` argument from the document — and a Sonar query with no filter returns the
whole table. A miss must narrow to nothing, never widen to everything.

**[DESIGN]** Every cross-hop binding is emitted with a two-argument default:

```
"id": {{ RECEIVED.serial_lookup.data.inventory_model_field_data.entities[0].inventory_item_id | default(-1, true) }}
```

`-1` matches no row. The `true` second argument makes the default fire on any
falsy value, not only on `undefined`. Audit check **G09** fails the build if any
`RECEIVED` interpolation ships without the guard; mutant **M28** proves it.

**[DESIGN]** The serial hop also pins `inventory_model_field_id` to a tenant
constant. Without it, any inventory field whose value happens to equal the string
would match — a MAC that collides with a serial would resolve the wrong device.
Checks **G10/G11**, mutant **M31**.

---

## 11. Reuse strategy, and where external vendor schema fits

Five rules, in the order they pay off.

### 11.1 Resolve polymorphism at build time, never in a template

`fields`, selectors and hop chains all look like things you would branch on with
`{% if %}`. Don't. **[FACT]** a fatal Jinjava error renders as an empty string
with no exception — `JinjaTemplate.renderParsed:101-105` — so a mistyped
conditional produces a silently truncated GraphQL document, not a failure.

Instead each selector becomes its own `action` const, its own schema, its own
precompiled document chain, and its own classification route. The templates stay
branch-free. The payoffs compound: every path is independently schema-validated,
independently auditable, independently visible as
`eventgateway.routing.rules.triggered{rule=...}`, and adding a fifth selector is
a config edit rather than a template rewrite.

### 11.2 Make the reusable unit the *selector*, not the entity

`byAccount` means the same thing against Sonar, MXK, Calix and Adtran Mosaic —
all four of which appear as service-detail entities in this same SDL. Modelling
the selector once and binding it per vendor is what makes a second vendor cheap.
Here that is the SSOT `lookups` block: hops name a root and its arguments, and
`build.py` reads every GraphQL type off the vendor's own schema. Nothing about
the selector contract is Sonar-specific.

### 11.3 Borrow the vendor-neutral vocabulary rather than inventing one

**[FACT]** TMF639 `Characteristic` is a `{name, value, valueType}` triple with
`name` and `value` required. That is structurally identical to Sonar's
`inventory_model_field_data` row. So the serial selector is not a bespoke ETI
type — it is a TMF Characteristic with the name pinned:

```json
{ "allOf": [
    { "$ref": "#/definitions/tmf639.Characteristic" },
    { "required": ["name","value"],
      "properties": { "name": { "const": "serialNumber" },
                      "value": { "type": "string",
                                 "pattern": "^[A-Za-z0-9._:-]{1,128}$" } } } ] }
```

`allOf` composition means the base stays borrowed and the constraint stays yours.
Swapping the constant to `macAddress` gives a MAC selector with no new modelling,
and a TMF-speaking client already knows the shape. Checks **G13–G16** fail the
build if the composition loses either the vendored base or the pinned name;
mutant **M33** proves it.

### 11.4 Reference external schema at build time; publish one flattened bundle

This is the rule most likely to be got wrong. **[FACT]** `RemoteSchemaResolver`
does a plain HTTPS GET with no authentication and no cache. **[UNVERIFIED]**
whether the validator then follows a `$ref` *out of* the fetched document to a
third host (**G-18**). A chain of live remote refs across tmforum.org, a vendor
CDN and your own host is a schema-compile-time dependency on three availabilities
you do not control.

**[DESIGN]** So `build.py` fetches upstream, extracts the transitive closure of
the definitions actually used, rewrites their internal refs into a namespace
(`tmf639.Characteristic`), and inlines them into `sonar-crud.schemas.json`. The
runtime dereferences exactly one remote `$ref`, one hop deep, into a document you
publish. External schema reuse is real; external schema *availability* is not on
the critical path.

### 11.5 Pin third-party schema by digest, and fail closed on drift

`vendor.lock.json` records, per namespace: source URL, upstream sha256, extracted
sha256, the definitions taken, and the fetch date. `build.py` verifies the
extract digest on every run and **refuses to build** on mismatch — a silent
upstream edit cannot reach a spec you are about to upload. `audit.py` re-verifies
independently (**G01–G03**), and **G04** fails if any internal `$ref` in the
published bundle does not resolve within it. Refreshing is a deliberate,
reviewable act (`--refresh-vendor`), not a side effect of building.

---

## 12. Gap register — additions

| # | Severity | Gap | Close it by |
|---|---|---|---|
| **G-15** | **BLOCKING** | `constants.serial_inventory_model_field_id: 7` is a placeholder. Wrong value silently resolves serials against the wrong inventory field. | `{ inventory_model_fields(name: "Serial Number") { entities { id inventory_model_id unique } } }` per tenant, then edit the SSOT and rebuild. |
| **G-16** | **BLOCKING** | **[UNVERIFIED]** `InventoryitemableType` enum member `Account`. Enum value sets are absent from the SDL corpus; a wrong literal is a GraphQL validation error on every `inventory_item.read.byAccount`. | `{ __type(name: "InventoryitemableType") { enumValues { name } } }`. |
| **G-17** | **BLOCKING** | **[UNVERIFIED]** whether `inventory_model_field_data(value:)` is exact or partial match. Partial matching would let a short serial over-match, and hop 2 takes `entities[0]`. | Same introspection pass; if partial, add a `unique` assertion or move the filter into `search:` once G-14 closes. |
| **G-18** | **BLOCKING** | **[UNVERIFIED]** whether networknt resolves an internal `#/definitions/...` ref inside a *remotely fetched* schema. If not, the four selector schemas fail to compile and every request DLQs. | Cheap pre-deploy check: `POST /admin/validate?channel=sonar-crud-in` with `fixtures.json` accept case `c-0101`. If it fails, rebuild with the vendored definitions dereferenced inline. |
| **G-19** | non-blocking | **[UNVERIFIED]** the two-argument `default(-1, true)` form in Jinjava 2.8.3. If the second argument is ignored, an empty-string miss would widen the query. | Add a `JinjavaAuthoringResearchTests` case. Until then, confirm with one `/admin/diagnose` run against a serial known to be absent. |
| **G-20** | non-blocking | **[DESIGN]** Multi-hop lookups take `entities[0]`; a duplicate serial silently resolves to one arbitrary device. | Accepted if the serial field carries Sonar's `unique` flag — verify alongside G-15. |
| **G-21** | non-blocking | Vendor URLs track `master`, not a tagged release. The digest pin catches drift but the build breaks rather than pinning a version. | Repoint `vendor.lock.json` URLs at a release tag once TMF publishes one for v4.0.0. |

---

## 13. Ranges

**[FACT — vendor documented]** Sonar's `Search` input takes typed buckets:
`integer_fields` with `{attribute, operator, search_value}`, `string_fields`
with `{attribute, search_value, match, partial_matching}`, and `boolean_fields`
with `{attribute, search_value}`. Same attribute ORs, different attributes AND.
`EQ` and `LEQ` appear by name in Sonar's GraphiQL guide. Note the shapes differ:
`string_fields` and `boolean_fields` do **not** carry an `operator`.

**[DESIGN]** `<entity>.read.byRange` is generated for all four entities. The
request is vendor-neutral; the vendor dialect lives in one SSOT block.

```json
{
  "action": "account_service.read.byRange",
  "correlationId": "prov-0001",
  "fields": "id data_usage_percentage",
  "where": {
    "numeric": [ { "attribute": "data_usage_percentage", "op": "gte", "value": 80 },
                 { "attribute": "data_usage_percentage", "op": "lte", "value": 100 } ],
    "text":    [ { "attribute": "name_override", "op": "contains", "value": "Fiber" } ],
    "flag":    [ { "attribute": "is_additional", "value": true } ]
  }
}
```

**Bucket membership is derived from the SDL argument types**, not declared by
hand. `Int64Bit|Int|Float|Numeric` → `numeric`, `String|ID` → `text`,
`Boolean` → `flag`. So `attribute` is an enum of exactly the columns of that
type on that root, and a range predicate against a string column is rejected at
the gateway rather than by Sonar. Checks **H04/H05**, mutant **M36**.

Three guards worth naming:

- `where` requires `minProperties: 1`. An empty predicate object would compile
  to an empty `search` and return the unfiltered table (**H02**, **M38**).
- Each bucket is capped at 25 predicates so a caller cannot build an unbounded
  document (**H11**, **M41**).
- Each loop is guarded with `| default([], true)` so an omitted bucket emits an
  empty array rather than iterating undefined (**H10**, **M39**).

### 13.1 Where I bend the no-logic-in-templates rule, and why it is safe

§11.1 says branching belongs in classification, not in Jinja. Ranges are
caller-supplied and variable-length, so they cannot be unrolled at build time —
the range mapping contains three `{% for %}` loops, an operator map lookup, and
the documented `{% if not loop.last %}` trailing-comma idiom.

That is a different risk class from business-logic branching, and it is
contained deliberately:

- Zero business conditionals. The only `if` is the separator idiom, which
  `AUTHORING-SPECS.md` §10.6 documents as the canonical form.
- Operator translation is a map index (`OPS_NUMERIC[c.op]`), not a conditional,
  and `op` is an `enum` in the schema so the index cannot miss.
- **The emitted member is pinned as golden text.** `audit.py` reconstructs the
  expected string from the SSOT dialect and fails if the template does not
  contain it verbatim (**H09**). Mutant **M37** — adding an `operator` key to
  `string_fields`, which Sonar does not accept there — is killed by that check.
  It was also a real bug in the first draft of this generator.

Date ranges are **not** offered. The vendor documentation confirms integer,
string and boolean buckets; a datetime bucket name is not confirmed, and the
SDL corpus has no `Search` type to read it from (**G-23**).

---

## 14. Dereferenced identifiers

`accountNumber` and `serialNumber` are declared once in the SSOT
`identifiers` block and dereferenced everywhere else:

| Consumer | How it dereferences |
|---|---|
| Request schemas | `{"$ref": "#/definitions/identifier.serialNumber"}` |
| Bundle | one definition, composed over `tmf639.Characteristic` for `serialNumber` |
| Lookup hops | `args: { "@identifier": accountNumber }` — build.py resolves the per-entity binding |
| `application.yaml` | `sonar.identifiers.<name>.<scope>` |

**[FACT]** `accountNumber` binds to a different argument per entity —
`accounts.id`, `account_services.account_id`,
`inventory_items.inventoryitemable_id` — because Sonar has no `account_number`
field. One identifier, three bindings, declared in one place. Adding a second
vendor is a `bindings:` entry, not a schema.

Checks **G05–G11** fail the build if an identifier is inlined rather than
`$ref`d, if its constraint drifts from the SSOT, or if a lookup stops requiring
it. Mutants **M33–M35**.

---

## 15. How the blocking gaps were closed

The two tenant-specific values are no longer baked into GraphQL documents as
literals. They are now **GraphQL variables bound from `CONFIG`** with the SSOT
value as an audited fallback:

```
"inventory_model_field_id": {{ CONFIG['sonar']['identifiers']['serialNumber']['fieldId'] | default(7, true) }}
"inventoryitemable_type": "{{ CONFIG['sonar']['identifiers']['accountNumber']['inventoryAssigneeType'] | default('Account', true) }}"
```

| Was | Now |
|---|---|
| **G-15** serial field id hardcoded as `7`; wrong tenant meant a spec rebuild | `sonar.identifiers.serialNumber.fieldId` in `application.yaml` |
| **G-16** `Account` enum member baked in as a GraphQL literal | `sonar.identifiers.accountNumber.inventoryAssigneeType` in `application.yaml` |

The spec is now **tenant-portable** — identical bytes in dev, staging and prod.

This is deliberately *not* the option rejected in §2.1. That rejection was
about putting **GraphQL documents** in `CONFIG`: documents change with the
spec, and the spec has a verified hot-reload path that `CONFIG` does not.
Tenant identity changes with the environment, and an environment change already
implies a restart. **[FACT]** `CONFIG` is bound from the Spring `Environment` on
every render, so a value set in `application.yaml` is live from startup; only
*hot* rebinding via `POST /actuator/refresh` is unverified (**G-13**).

`audit.py` check family **I** cross-validates the two files: every scope path
the spec dereferences must exist in `application.yaml`, the token api must be
declared, the dead-letter channel must resolve to a real spec channel, and
`payload-logging.enabled: false` is rejected because it silently disables every
`x-log-redact` path in the spec.

---

## 16. Gap register — additions and closures

| # | Severity | Status |
|---|---|---|
| **G-15** | ~~BLOCKING~~ | **Closed.** Now `sonar.identifiers.serialNumber.fieldId`. Still discover the real value per tenant — the default is a placeholder — but a wrong value is a ConfigMap edit, not a rebuild. |
| **G-16** | ~~BLOCKING~~ | **Closed.** Now `sonar.identifiers.accountNumber.inventoryAssigneeType`. |
| **G-14** | non-blocking (was) | **Partly closed.** `Search` is now modelled from vendor documentation rather than passed opaquely, for integer/string/boolean buckets. Mutation `input:` interiors remain opaque. |
| **G-22** | non-blocking | **[UNVERIFIED]** Operator spellings. `EQ` and `LEQ` are vendor-documented; `NEQ`, `GT`, `GEQ`, `LT` are inferred by symmetry. Isolated in `search_dialect.buckets.*.ops` — a correction is one SSOT edit. Confirm with `{ __type(name: "SearchOperator") { enumValues { name } } }`. |
| **G-23** | non-blocking | No date/datetime range bucket. The vendor docs confirm three buckets; a datetime bucket is not confirmed and the SDL has no `Search` type to read. Add a fourth bucket to `search_dialect` once introspected. |
| **G-24** | non-blocking | **[UNVERIFIED]** whether Sonar tolerates an empty `integer_fields: []` when only one bucket is populated. All three buckets are always emitted. If it objects, gate bucket emission on presence. |
| **G-25** | non-blocking | **[UNVERIFIED]** whether `integer_fields.search_value` is `Int` rather than `Int64Bit`. Account ids beyond 2³¹ may need the `text` bucket. Confirm alongside G-22. |
| **G-26** | non-blocking | `api-source.type: FILE` in `application.yaml` makes `POST /specification/upload` validate-then-revert. Switch to `MEMORY` if you want hot spec upload; keep `FILE` if you want GitOps to be the only path. |

---

## 17. Test harness

`audit.py` is static analysis: it proves the spec is internally coherent but
never renders a template or sends a request. Everything downstream of that lives
in `harness/`, documented in **HARNESS.md**.

| Stage | Needs | Covers |
|---|---|---|
| `selftest` | nothing | the harness fails when the interface is broken (9 mutants) |
| `schema` (L1) | nothing | fixtures accept/reject as intended |
| `render` (L2) | nothing | templates → GraphQL, parsed and checked against the SDL; narrowing probe; envelope discrimination |
| `spec` (L3) | a gateway | spec upload, remote `$ref` resolution, per-fixture classification |
| `route` (L4) | gateway + `mock_sonar` | real Jinjava, real `receiveFrom` chaining, hop ordering |
| `probe` (L5) | a tenant | closes G-01/02/15/16/22/23/25 |

Current offline state: **374/374 checks**, ~2s, no credentials required.

Two things the harness surfaced that the static audit could not:

- **`CONFIG` sees resolved Spring properties, not `${VAR:default}` text.** A
  harness reading `application.yaml` literally rendered the placeholder straight
  into a GraphQL document. L2 now resolves placeholders the way Spring does,
  which turns "does `application.yaml` satisfy the spec's `CONFIG` contract"
  into an executable check rather than a claim.
- **A `CONFIG` value in a JSON number position must resolve to a number.**
  `SONAR_SERIAL_FIELD_ID=serial-field` renders bare and breaks the document
  (**G-27**).

`jinjava_model.py` is explicitly a *model* of Jinjava 2.8.3, not the engine:
every shim cites the behaviour it reproduces, and any construct outside the
verified shared subset is refused rather than approximated. A green L2 is a
prediction; L4 is the evidence.

## Sonar Response Fixtures & Tenant Checks

### Response Fixtures

Canned Sonar responses are keyed to the GraphQL operations generated by `build.py`. They are shared by offline rendering (L2) and live gateway routing (L4). Real-tenant verification happens in L5.

#### Serial Lookup

* **Serial found:** returns inventory field data with `inventory_item_id = 4242`.
* **Serial missing:** returns no entities.
* **Item lookup with `id = -1`:** returns no inventory items.
* **Valid item:** returns inventory item `4242`, status `DEPLOYED`.

#### Account-Service Lookup

* **Serial → item:** returns inventory item `4242` with `account_service_id = 777`.
* **Item lookup with `id = -1`:** returns no items.
* **Valid service:** returns account service `777`.
* **By account number:** returns services `777` and `778`.
* **By account number, inventory:** returns inventory item `4242`.

#### Range Queries

* **Account service:** returns service `777` with `data_usage_percentage = 91`.
* **Account:** returns account `12345`.

### Basic Operations

* **Account read:** returns account `12345`, `"Fiber Test Account"`.
* **Account update:** returns a permission error.

### Tenant Verification

These checks require a real Sonar tenant and are used by L5 to confirm vendor-specific details.

| Gap  | What it confirms                                                | Result                                                                                   |
| ---- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| G-01 | Connection results contain `entities` and `page_info`.          | Confirm fields with `AccountConnection`.                                                 |
| G-02 | Delete responses contain `success` and `message`.               | Confirm fields with `SuccessResponse`.                                                   |
| G-15 | Which inventory field represents a serial number.               | Save the field ID to `sonar.identifiers.serialNumber.fieldId`.                           |
| G-16 | Enum value used when an inventory item belongs to an account.   | Save to `sonar.identifiers.accountNumber.inventoryAssigneeType`.                         |
| G-17 | Whether serial lookup requires an exact or partial value match. | Compare full serial and shortened serial results.                                        |
| G-22 | Valid search operator names.                                    | Confirm `EQ`, `NEQ`, `GT`, `GEQ`, `LT`, `LEQ`; save to the numeric search configuration. |
| G-23 | Available search buckets.                                       | Confirm integer, string, and boolean search inputs.                                      |
| G-25 | Numeric type accepted by `integer_fields.search_value`.         | Inspect the `IntegerSearch` input type.                                                  |

### Trust Rule

Fixtures are suitable for offline and gateway tests but do **not** establish that the response shapes match a real Sonar tenant. Any fixture or shape marked as unverified must be refreshed from the L5 probe before being treated as authoritative.

| # | Severity | Gap |
|---|---|---|
| **G-27** | non-blocking | A tenant override that resolves to a non-numeric value renders bare into a JSON number position and breaks the document. Caught by harness L2 pre-deploy; not catchable by the request schema, since `CONFIG` is outside its reach. |
