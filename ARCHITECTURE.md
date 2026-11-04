# Accobiz Excel Format Conversion Utility — Framework Design

## 1. Core Idea

```
   MANY INPUTS                     ONE ENGINE                     FIXED OUTPUTS
┌──────────────┐            ┌──────────────────────┐          ┌───────────────────┐
│ Tally        │──┐         │ Read → Detect → Map  │          │ Accobiz Ledger    │
│ Busy         │──┼──────▶  │ → Transform →        │ ──────▶  │ Import Template   │
│ Marg         │──┤ adapter │ Validate → Write     │          ├───────────────────┤
│ QuickBooks…  │──┘ (YAML)  │                      │          │ Accobiz Stock     │
└──────────────┘            └──────────────────────┘          │ Import Template   │
                                     ▲                         └───────────────────┘
                                     │ suggestions only
                              ┌──────┴──────┐
                              │ AI Assistant│
                              └─────────────┘
```

Three things vary independently:

| Concept | Varies by | Lives in |
|---|---|---|
| **Target Schema** (what Accobiz wants) | Data type (Ledger / Stock) | `schemas/` — code, versioned, changes rarely |
| **Source Profile** (what software X exports) | Software × Data type × Export variant | `profiles/` — YAML, added freely |
| **Engine** (how conversion happens) | Nothing — fully generic | `engine/` — never touched for new software |

**Rule:** Adding a new software = add YAML files only. Adding a new data type (e.g. Party, Tax masters) = add one Target Schema + profiles. Engine never changes.

---

## 2. Target Schemas (the fixed output)

Defined once per data type. Single source of truth for output column order, headers, types, mandatory rules, and allowed values.

### 2.1 Field definition model

```python
class TargetField(BaseModel):
    key: str                 # internal id, e.g. "ledger_name"
    header: str              # exact Accobiz header, e.g. "Ledger Name"
    order: int               # column position in output
    dtype: Literal["text", "decimal", "integer", "date", "enum", "boolean"]
    required: bool
    enum_values: list[str] | None = None     # e.g. ["Dr", "Cr"]
    max_length: int | None = None
    decimals: int | None = None              # for decimal fields
    critical: bool = False                   # financial/qty/tax → never altered, only parsed
    description: str                         # also fed to AI for semantic matching
    synonyms: list[str] = []                 # generic hints (software-agnostic)

class TargetSchema(BaseModel):
    data_type: Literal["ledger", "stock"]
    version: str                             # tracks Accobiz template version
    template_file: str                       # official .xlsx template to fill
    sheet_name: str
    header_row: int
    fields: list[TargetField]
    unique_keys: list[str]                   # duplicate detection, e.g. ["ledger_name"]
    cross_rules: list[str] = []              # row-level rules, see §5
```

### 2.2 Output = "Accobiz" profile (fixed)

Accobiz is modelled as the **single output software**. Its two templates are stored as-is in
`templates/accobiz/` and the Writer fills them — so the `Options` sheet and all dropdown
data-validations (rows 2–500) are preserved in the downloaded file.

Template facts (from the official samples):
- Sheet `Ledgers` / `StockAccounts`, header row 1, data from row 2, lowercase machine headers.
- Sheet `Options` holds the allowed values for every dropdown column → loaded at startup as enum lists (single source of truth, no hard-coding).
- Values are written **as text** (`'0.00'`, `'141001'`), except `gstrate`/`cessrate` which are numeric. Writer follows the same per-column cell type.

### 2.3 Accobiz Ledger schema — sheet `Ledgers` (39 columns)

| # | Header | Type | Allowed values / rule | Critical |
|---|---|---|---|---|
| A | code | text | optional; unique if given | |
| B | **name** | text | **required, unique** | |
| C | type | text | *(meaning to confirm — see §13)* | |
| D | gstin | text(15) | GSTIN format; state code ↔ `state` | |
| E | pan | text(10) | PAN format; should equal gstin[2:12] | |
| F | udyam | text | UDYAM-XX-00-0000000 format | |
| G | openingbalance | decimal(2) as text | non-negative | ✔ |
| H | balancetype | enum | DEBIT, CREDIT — required if balance ≠ 0 | ✔ |
| I | annexuredr | enum (28) | Options!B — Dr-side BS/P&L heads | |
| J | annexurecr | enum (16) | Options!C — Cr-side BS/P&L heads | |
| K | cashbank | enum | YES, NO | |
| L–Q | address, city, district, state, pin, country | text / enum(state: 36) / 6-digit pin | |
| R–T | contact, whatsapp, email | phone(10) / email format | |
| U | agent | text | | |
| V | qtydetails | text | | |
| W–Z | bankname, accountno, ifsc, accounttype | IFSC format; accounttype: SAVINGS, CURRENT, CC, OD | |
| AA–AB | tdsno, gsttdsno | text | | |
| AC–AE | tdsonsales, composition, nri | YES / NO | |
| AF | msme | '', MICRO, SMALL, MEDIUM | |
| AG | c27 | YES / NO | |
| AH–AM | areacode, aotype, rangecode, aono, ward, lastassessed | text (income-tax AO details) | |

Note: Accobiz uses **annexure heads** (not a free "Under Group"). Mapping a source group
(e.g. Tally "Sundry Debtors") → `annexuredr`/`annexurecr` is the key accounting-aware step:
handled by a per-software **group → annexure value map**, with AI suggestions for unknown groups.

### 2.4 Accobiz Stock schema — sheet `StockAccounts` (21 columns)

| # | Header | Type | Allowed values / rule | Critical |
|---|---|---|---|---|
| A | code | text | optional; unique if given | |
| B | **description** | text | **required, unique** (item name) | |
| C | type | enum | Goods, Service | |
| D | hsn | text | 4/6/8 digits (SAC for services); keep leading zeros | |
| E | unit | enum (45 UQC) | BAG … YDS — source units normalized (Nos→NOS, Kg→KGS) | |
| F | gstrate | number enum | 0, 5, 12, 18, 28 | ✔ |
| G | cessrate | number | | ✔ |
| H–J | salerate, purchaserate, mrp | decimal(2) as text | | ✔ |
| K | openingqty | decimal as text | | ✔ |
| L | openingvalue | decimal(2) as text | check ≈ qty × rate (warning only) | ✔ |
| M–N | batchno, expirydate | text / date | | |
| O | gsttype | enum | REGULAR, NIL, EXEMPT (derive NIL/EXEMPT when gstrate = 0 per source flag) | |
| P–Q | gsttdsapplicable, gsttdsrate | Yes/No, number | | ✔ |
| R–S | tcsapplicable, tcsrate | Yes/No, number | | ✔ |
| T–U | purchaseaccount, salesaccount | enum — **company-specific ledger names** | |

Note: `purchaseaccount`/`salesaccount` lists come from the Accobiz company's own ledgers
(the Options sheet in the sample has 195 of them). These lists must be supplied per company
(uploaded Ledger file, or Accobiz-exported template) — the engine treats them as a **dynamic enum**.

### 2.5 Variable row counts (50 – 5,000+ records)

- No fixed row ranges anywhere: the reader reads until the data ends.
- The Accobiz templates only have dropdown validation on rows 2–500. The Writer **extends every
  data-validation range to the last written row**, so the dropdowns work on all rows of large files.
- AI classification is batched (e.g. 200 names per call) and only sent names the rules could not
  classify confidently, so cost and time stay flat as files grow.
- The preview grid pages and virtualizes rows; the "errors only" filter makes large files reviewable.

### 2.6 Yes/No casing

Ledger uses `YES/NO`, Stock uses `Yes/No`. Each field's enum carries its exact casing; the
Transformer normalizes any source variant (`Y`, `true`, `1`, `yes`) to the target's casing.

---

## 3. Source Profiles (the plug-in point)

One YAML file per **software × data type × export variant**.

```
profiles/
  tally/
    ledger.default.yaml
    stock.default.yaml
  busy/
    ledger.default.yaml
  _generic/
    ledger.yaml            # fallback when software is "Other"
    stock.yaml
```

### 3.1 Profile format

```yaml
id: tally.ledger.default
software: Tally Prime
data_type: ledger
variant: default
target_schema_version: "1.0"

# How to find the table inside the uploaded file
layout:
  sheet: auto                 # or name / index
  header_detection: auto      # scans first N rows for best header match
  scan_rows: 20
  skip_rows_matching:         # report titles, company name, totals
    - "^Grand Total"
    - "^Total$"
  stop_at_blank_rows: 3

# Fingerprint used to auto-detect software/variant
fingerprint:
  required_headers: ["Name", "Under"]
  optional_headers: ["Opening Balance", "Dr/Cr"]

# Column → target field mapping
columns:
  name:
    aliases: ["Name", "Ledger Name", "Particulars"]
  annexuredr:            # source group → Accobiz annexure head
    aliases: ["Under", "Group", "Parent"]
    value_map_ref: tally_group_to_annexure         # see value maps
  openingbalance:
    aliases: ["Opening Balance", "Op. Bal"]
    parse: { thousands: ",", negative_style: ["-", "()"] }
  balancetype:
    aliases: ["Dr/Cr", "Type"]
    value_map: { "Dr": "DEBIT", "Debit": "DEBIT", "Cr": "CREDIT", "Credit": "CREDIT" }

# Derived fields — when source encodes info differently
derive:
  - when: "balance_type is missing and opening_balance has suffix"
    rule: split_amount_suffix          # "12,500.00 Dr" → 12500.00 + Dr
    from: openingbalance
  - when: "balance_type is missing and opening_balance is signed"
    rule: sign_to_drcr                  # -500 → 500 + Cr (convention set below)
    convention: { negative: CREDIT }

# Columns we know about but intentionally ignore
ignore: ["Sl No", "Alias", "Closing Balance"]

value_maps:
  tally_group_to_annexure:
    "Sundry Debtors": "SUNDRY DEBTORS/TRADE REVEIVABLES"
    "Bank Accounts": "BALANCES  WITH SCHEDULED BANKS"
    "Sundry Creditors": "SUNDRY CREDITORS/TRADE PAYABLES"   # → annexurecr
    # unmapped values are flagged, not guessed
```

### 3.2 Built-in transform library (reusable by all profiles)

Profiles reference these by name; they're implemented once in `engine/transforms/`.

| Transform | Purpose |
|---|---|
| `trim`, `collapse_spaces` | text cleanup (non-critical fields only) |
| `parse_decimal` | Indian/intl number formats, `1,23,456.00`, `(500)` → Decimal |
| `split_amount_suffix` | `"5000 Dr"` → amount + type |
| `sign_to_drcr` | signed amount → abs amount + Dr/Cr |
| `merge` | Address1 + Address2 → Address |
| `split` | "Qty Unit" → qty + unit |
| `value_map` | enum normalization |
| `date_parse` | Excel serials / dd-mm-yyyy etc. |
| `default` | fill optional field with default |
| `uqc_normalize` | "Nos", "NOS", "Numbers" → `NOS` |

---

## 4. Conversion Engine (generic pipeline)

```
Upload ─▶ ① Reader ─▶ ② Table Locator ─▶ ③ Profile Detector ─▶ ④ Mapper
                                                                   │
        ⑧ Writer ◀─ ⑦ Validator ◀─ ⑥ Transformer ◀─ ⑤ User Review ◀┘
```

| Stage | Responsibility | Output |
|---|---|---|
| ① **Reader** | Read .xlsx/.xls/.xlsb/.csv **as text** (calamine). Original file never written to. Compute file hash. | `RawWorkbook` |
| ② **Table Locator** | Pick sheet, find header row (scores rows vs. known headers), drop title/total/blank rows. Track source row numbers. | `RawTable` (headers + rows + source_row_no) |
| ③ **Profile Detector** | Score table against profile fingerprints of selected software; pick variant. If user chose "Other" → generic profile. | `Profile` + confidence |
| ④ **Mapper** | 3-tier column mapping (§6). | `MappingProposal` |
| ⑤ **User Review** | User confirms/corrects mapping; confirmed mapping is saved. | `ConfirmedMapping` |
| ⑥ **Transformer** | Apply value maps, derivations, parsing. Critical fields: parse only, never round/alter. Keeps original value alongside. | `CanonicalRecords` |
| ⑦ **Validator** | Field + row + dataset rules (§5). | `ValidationReport` |
| ⑧ **Writer** | Fill official Accobiz template with records in schema order. | Output `.xlsx` (+ optional error report) |

### 4.1 Canonical record (internal format between stages)

```python
class Cell(BaseModel):
    raw: str | None          # exactly as in source
    value: Any               # parsed (Decimal, str, date…)
    status: Literal["ok", "warning", "error"]
    messages: list[str] = []

class CanonicalRecord(BaseModel):
    source_row: int          # traceability to original Excel row
    fields: dict[str, Cell]  # keyed by TargetField.key
```

Every output cell is traceable to its source row & raw value — this is what powers the preview highlighting and audit.

### 4.2 Data integrity guarantees

- Row count in == rows out + rows explicitly excluded (each exclusion listed with reason).
- Control totals: sum of Opening Balance (Dr − Cr), sum of qty/value — shown **before vs after**; must match.
- Critical fields: `Decimal` only, no float; no rounding; if unparseable → error, never guessed.
- AI output never writes to values — only to the mapping.

---

## 5. Validation Framework

Rules are declared in the schema; engine runs them in three levels.

| Level | Examples | Severity |
|---|---|---|
| **Field** | required missing; not a number; enum not allowed; GSTIN/PAN format; length | error / warning |
| **Row** | Balance ≠ 0 but no Dr/Cr; Qty × Rate ≠ Value (tolerance); GSTIN state code ≠ State | error / warning |
| **Dataset** | duplicate Ledger/Item names; group not in Accobiz master; Dr total ≠ Cr total (info, opening TB difference) | error / warning / info |
| **Structure** | mandatory target field has no mapped column; unrecognized source columns | error / warning |

Errors block download (unless user excludes those rows); warnings don't.

---

## 6. AI Layer (assistive, bounded)

### 6.1 Where AI is used

| Use | Input to AI | Output | Trust |
|---|---|---|---|
| **Column mapping** (tier 3) | unmatched headers + 5 sample rows + target field descriptions | `{source_col, target_key, confidence, reason}` | user confirms |
| **Group/enum value mapping** | unknown values (e.g. source groups "Debtors-Local") + Accobiz group list | suggested mapping + reason | user confirms |
| **Profile bootstrap** | a new software's sample file | draft YAML profile | developer reviews & commits |
| **Error explanation** (later) | validation errors | plain-language fix hints | informational |

### 6.2 Mapping tiers

```
Tier 1  Profile aliases (exact, normalized)        → confidence 1.0
Tier 2  Learned mappings (user-confirmed history)  → confidence 0.95
Tier 3  Fuzzy match on header (rapidfuzz ≥ 85)     → confidence 0.7–0.9
Tier 4  AI semantic match (headers + data shape)   → model-reported, capped 0.85
        Unmatched                                   → "Unrecognized" flag
```

UI shows confidence badges; anything < 0.9 is highlighted for review.

### 6.3 Guardrails

- AI never sees full dataset — headers + ≤5 sample rows only (configurable, can be disabled for privacy).
- Strict structured output (tool use / JSON schema) validated by Pydantic; invalid → ignored.
- AI cannot map to a field that doesn't exist; cannot create transforms on critical fields.
- Every AI suggestion logged (prompt hash, model, response) for audit.
- Utility works fully **without** AI (tiers 1–3 only).

---

## 7. Persistence

| Store | Contents |
|---|---|
| `learned_mappings` | (software, data_type, normalized_header_signature) → confirmed mapping |
| `learned_value_maps` | (software, field, source_value) → target value |
| `jobs` | job id, file hash, profile used, status, counts, control totals, timestamps |
| `ai_audit` | suggestions + acceptance/rejection (improves profiles over time) |

SQLite in v1 → Postgres later. Uploaded files are temporary (auto-deleted after N hours).

---

## 8. API

```
GET  /api/meta/data-types                     → ["ledger", "stock"]
GET  /api/meta/softwares?data_type=ledger     → list from profiles/
GET  /api/meta/schema/{data_type}             → target schema (for UI)

POST /api/jobs                                → upload file + data_type + software
                                                → job_id, detected sheet/header, profile
GET  /api/jobs/{id}/mapping                   → MappingProposal (with confidence, reasons)
PUT  /api/jobs/{id}/mapping                   → ConfirmedMapping (+ value maps)
POST /api/jobs/{id}/convert                   → runs transform + validate
GET  /api/jobs/{id}/preview?page=&filter=errors → records + cell statuses
GET  /api/jobs/{id}/report                    → validation summary + control totals
GET  /api/jobs/{id}/download                  → Accobiz .xlsx
GET  /api/jobs/{id}/download/errors           → error report .xlsx
```

---

## 9. UI Flow (6-step wizard)

1. **Data Type** — Ledger / Stock cards
2. **Source Software** — dropdown (from profiles) + "Other"
3. **Upload** — drag & drop; shows detected sheet & header row, allow override
4. **Review Mapping** — two-column table: *Accobiz Field ← Source Column*, confidence badge, AI reason tooltip; unmapped source columns listed separately; unknown group/unit values mapping sub-step
5. **Preview & Validate** — grid in Accobiz column order, error cells red / warnings amber, filter "errors only", control totals before vs after
6. **Download** — Accobiz file + optional error report

---

## 10. Project Structure

```
accobiz-converter/
├─ backend/
│  ├─ app/
│  │  ├─ api/                 # FastAPI routers
│  │  ├─ engine/
│  │  │  ├─ reader.py
│  │  │  ├─ locator.py
│  │  │  ├─ detector.py
│  │  │  ├─ mapper.py
│  │  │  ├─ transformer.py
│  │  │  ├─ transforms/       # transform library (§3.2)
│  │  │  ├─ validator.py
│  │  │  ├─ rules/            # field/row/dataset rules
│  │  │  └─ writer.py
│  │  ├─ schemas/
│  │  │  ├─ base.py           # TargetField, TargetSchema
│  │  │  ├─ ledger.py
│  │  │  ├─ stock.py
│  │  │  └─ templates/        # official Accobiz .xlsx templates
│  │  ├─ profiles/            # YAML source profiles (§3)
│  │  ├─ ai/
│  │  │  ├─ client.py         # Claude API wrapper
│  │  │  ├─ column_mapper.py
│  │  │  └─ value_mapper.py
│  │  ├─ store/               # SQLAlchemy models
│  │  └─ main.py
│  └─ tests/
│     ├─ fixtures/<software>/<data_type>/*.xlsx   # real sample exports
│     └─ golden/              # expected outputs → regression tests
├─ frontend/                  # React + TS + Vite wizard
└─ docker-compose.yml
```

---

## 11. Extensibility Checklist

| To add… | Do this | Engine change? |
|---|---|---|
| New software | Add `profiles/<sw>/ledger.*.yaml`, `stock.*.yaml` + fixtures | No |
| New export variant of same software | Add `profiles/<sw>/ledger.<variant>.yaml` with fingerprint | No |
| New data type (e.g. Parties, Tax) | Add `schemas/<type>.py` + template + profiles | No |
| New Accobiz template version | Bump schema version; profiles declare compatible version | No |
| New transform | Add function to `transforms/` + register | Minor, additive |
| New migration utility | Reuse engine as library under a new module | No |

---

## 12. Testing Strategy

- **Golden-file tests**: each fixture input → expected Accobiz output, byte-level compare of values.
- **Integrity tests**: row counts & control totals preserved for every fixture.
- **Profile lint**: CI validates every YAML against profile schema & target schema version.
- **Property tests** on `parse_decimal` (Indian formats, negatives, brackets).
- **AI tests**: recorded responses (no live calls in CI); tier 1–3 must pass without AI.

---

## 13. Inputs Needed to Finalize

1. ~~Official Accobiz templates~~ ✅ received (`templates/accobiz/`).
2. Meaning of ledger column **`type`** (blank in sample) — and is there an Accobiz "group" concept, or are `annexuredr/annexurecr` the only classification?
3. Annexure rule: does a ledger fill **both** `annexuredr` and `annexurecr` (used depending on balance side), or only one?
4. `purchaseaccount` / `salesaccount`: will the user supply the company's ledger list, or should the utility use the converted Ledger file's names?
5. Which fields are mandatory for Accobiz import besides `name` / `description`?
6. v1 source software + 2–3 real sample exports per data type.
