# Feature: family-ledger

ODD tracker. Engram mirror: `odd/family-ledger/tasks`.
Branch: `feat/family-ledger`. Started: 2026-09-24.

## Objective

Consolidate the household's expenses — statements from credit cards, bank accounts and
fintechs belonging to the user and their family — into a single plain-text ledger, so that
each amount keeps its meaning over time.

## Problem / Why

Argentine households hold the same money across many issuers, and no off-the-shelf app
solves the two problems that actually matter here:

1. **Double counting.** One expense shows up three times: as a purchase in the card
   statement, as a debit in the bank account that pays the card, and possibly as a
   transfer in a fintech. Those are *one purchase plus one settlement*, not three
   expenses. Beancount documents this as its canonical importer use case.
2. **Loss of meaning over time.** Nominal ARS amounts stop being comparable within
   months. The user's chosen fix is the USD-at-date measure.

Existing apps fail on the data model, not on features: Actual Budget states in its own
docs that it is *"currency agnostic and does not support multi-currency"*, and neither
Actual nor Firefly III models card debt, installments or any deflation. Since the sole
operator is a developer, the friction argument that favours a GUI app does not apply,
and the data model wins.

## Scope (authorized)

- Beancount-based plain-text ledger, kept **local only** (see Constraints).
- A normalized ingest pipeline: one importer per source, driven by files the user
  downloads himself.
- Historical USD series (MEP + CCL) with a local cache.
- CLI reporting that expresses spending in USD at the transaction date.
- Guards against the double counting described above.

## Out of scope (for now)

- Any UI (CLI only). A viewer is a later, separate decision.
- Email forwarding and home-banking scraping. Files only.
- IPC deflation. The pipeline must not preclude it, but the user chose USD-at-date.
- Anything that requires credentials for the family's banking.

## Constraints

- **Real financial data never enters this repository.** `expensuchis` is a **public**
  GitHub repo (`gh repo view` → `"isPrivate":false`). The ledger and every statement
  live outside this working tree and have no remote. Two guards are required:
  the ledger is referenced through an environment variable, never a path relative to
  this repo, and a test fails if any data file appears inside the tree.
- Python 3.12.3 is the system interpreter. **`venv` and `pip` are absent and the
  interpreter is `EXTERNALLY-MANAGED` (PEP 668)** — the environment must be created
  before any code runs.
- Beancount 3.x. Verified facts from a live probe (uv venv, beancount 3.2.3,
  beangulp 0.2.0, beanquery 0.2.0):
  - `beancount.ingest` and `bean-extract` **no longer exist** in 3.x. They were replaced
    by the separate `beangulp` package.
  - **`beangulp` 0.2.0 ships no console scripts at all.** `bean-identify`, `bean-extract`
    and `bean-file` do not exist. Its public surface is the `identify`/`extract`/`archive`
    modules plus the `Importer` and `Ingest` classes, with `click` as a transitive
    dependency. **Our own CLI must wrap it.**
  - `bean-check`, `bean-doctor`, `bean-example` and `bean-format` ship with `beancount`.
    **`bean-query` does not** — it lives in the separate `beanquery` package, which must
    be a declared dependency or the reporting tasks cannot be built.
  - **Argentine number format is rejected by the parser.** `1.234.567,89` fails with
    `Invalid token: '.567,89'`. Beancount requires `1,234,567.89` (comma thousands,
    period decimals). Every single importer needs a locale normalizer, and the fixture
    set must cover it.
  - Every account must be declared with an `open` directive before use; an undeclared
    account is a hard `bean-check` error.
- Generated artifacts in English. Conversation in Rioplatense Spanish.

## Decisions

- **Ledger: beancount** (over hledger, Firefly III, Actual Budget). Chosen for the data
  model: double entry, multi-currency, and full programmability for the deflated reports.
- **Ingest: files only**, one importer per source. The parser is the extension point;
  the transport is interchangeable by design.
- **Deflator: USD at date**, not IPC. Both MEP and CCL series are fetched; the rate is
  chosen at report time. Consequence accepted by the user: this measures hard-currency
  value, not purchasing power.
- **Deflation is a view, never a stored value.** The ledger stores nominal ARS facts plus
  dates; the FX series is separate data; the USD value is computed when reporting. This
  keeps a future switch of series (MEP, CCL, or IPC) from becoming a data migration.
- **No UI.** Queries and reports from the command line.
- **Repository boundary: public code, local ledger.** The code is publishable; the data
  has no remote.
- **Human review is mandatory in the import path.** No importer writes to the ledger
  directly; every batch passes through review first.
- **Sign convention (verified, not assumed).** Expenses are positive; a card purchase
  credits the liability negative; the statement payment debits the liability positive
  back to zero and takes the cash out of the asset account negative. Getting this
  backwards still produces a `bean-check`-clean ledger with plausible-looking totals —
  it is silent corruption, so it is pinned here and in T-02.
- **We build the import CLI ourselves** over beangulp's API, since beangulp ships no
  commands (see Constraints).

## Sources (T-05..N input)

Inventory reported by the user 2026-09-24, with delivery format researched against primary
sources. Confidence is recorded per row because a wrong assumption here becomes a dead-end
parser.

| Source | Type | What is downloaded | Delivery format | Parser difficulty |
| --- | --- | --- | --- | --- |
| Banco Provincia | Account + Visa card | `Cuentas → Extractos Electrónicos` | **PDF** downloaded here (the bank also documents a CSV export). The account extracto is **quarterly**, dot-decimal, with `Saldo` populated on every row; the card liquidación is monthly, Argentine comma-decimal, with **trailing minus** and `TC` | **Low** on the account, **medium** on the card |
| BBVA | Account + cards (Visa, Mastercard, unified) | `Cuentas → Extractos`, downloaded per product | **PDF, text layer present** — requires `pypdfium2`; `pypdf` returns polluted text and no layout. `DÉBITO`/`CRÉDITO` columns with a leading minus, plus a `SALDO` column | **Low-medium** (confirmed with real files) |
| Mercado Pago | Fintech | **Confirmed 2026-09-24 with two real files**: `Resumen de cuenta en pesos`, one PDF per person per month, reachable from `Informes y facturación → Reportes de ventas y extractos de cuenta` | **PDF generated from HTML** (`Producer: openhtmltopdf`), real text layer, layout-extractable — not a scan | **Low** — and self-verifying, see below |
| Brubank | Account | App → `Productos → Resumen de cuenta`; the period is **chosen by the user**, not a calendar month | **PDF, no password, text layer present in both extractors.** `Débito`/`Crédito` columns with `-` in the unused one, plus a `Saldo` column. No minus sign anywhere in the document | **Low** (confirmed with a real file) |

Evidence and confidence:

- **Banco Provincia — strong.** The bank's own manuals on `bancoprovincia.com.ar` (two
  separate documents) describe both download options, including CSV. Caveat: one manual
  says `extractos trimestrales`, so the cadence may be quarterly rather than monthly.
  Confirm in the account.
- **BBVA — weak, and biased.** The `D`/`H`-without-sign detail comes from a commercial
  PDF-to-Excel converter vendor, which has an incentive to make PDF extraction look hard.
  A conflicting source claims BBVA exports `.xlsx`/CSV, but that source appears to describe
  **BBVA Spain**, not Argentina. Net position: **it is not established whether BBVA
  Argentina exports anything other than PDF.** The user must look.
- **Mercado Pago — strong on flow, unverified on format.** The panel flow is confirmed in
  Mercado Pago's official AR developer documentation. The same docs describe generating
  reports **programmatically via API**, which would make this the only non-manual route in
  the entire inventory.
- **Brubank — strong and bad news.** Brubank's official help centre documents exactly one
  route: a monthly PDF delivered by email. If that is the only option, this is the worst
  source in the set.

### Per-person entity matrix (2026-09-24, from the user)

| Entity | People | Documents/month | Automatable | Parser |
| --- | --- | --- | --- | --- |
| Mercado Pago | **all three** | 3 | **Yes — API** | **Low** (confirmed by real files) |
| Brubank | Person 1 only | 1 | No | **Low** (confirmed with a real file) |
| BBVA | Person 1 only | 1-2 (account + card) | No | Medium-hard (`D`/`H`, unsigned) |
| Banco Provincia | Person 2 only | 1-2 (account + card) | No | Easy (CSV) |

Roughly **6-9 documents per month, 80-110 per year** — not the 16/month feared from the
worst-case multiplication of people by entities.

Open and small: which entity holds the **USD savings account**. It does not change the shape
of the tree (`Assets:<entity>:<person>:USD`) but it has to be named before T-02 is written.

**Self-correction.** An earlier entry in this document called Brubank "unsustainable" by
extrapolating three people times twelve months (36 emails a year). The real matrix puts
Brubank on **one** person, so it is 12 PDFs a year, not 36. The alarm was overstated and is
corrected here rather than quietly dropped.

### Mercado Pago confirmed against two real files (2026-09-24)

Two real statements, one month (August 2026), one per person: P1 and P3. Both are
`RESUMEN DE CUENTA EN PESOS`, the same template, so **one parser covers all three people**.

**The document carries its own checksum and the parse reconciles exactly.** Verified with a
throwaway probe over both files:

| Check | P1 (13 rows) | P3 (48 rows) |
| --- | --- | --- |
| Header arithmetic: opening + entries + withdrawals == closing | OK | OK |
| Sum of positive rows == declared *Entradas* | OK | OK |
| Sum of negative rows == declared *Salidas* | OK | OK |
| Running-balance chain: each row's balance == previous + value | complete | complete |
| Last parsed balance == declared closing balance | OK | OK |

All five exact to the cent. This makes the import **self-verifying**: the importer can prove
its own output complete and correctly signed *row by row*, and must **refuse to emit** rather
than write a doubtful ledger. Same discipline as privacy guard #1: fail closed.

What else the real files settled:

- **The movement vocabulary is small and closed**: `Transferencia enviada`, `Transferencia
  recibida`, `Rendimientos`, `Pago con`, `Dinero retirado`, `Dinero reservado`, and
  `Pago <servicio>` — a bill payment whose description names the merchant.
- **The operation-ID column supplies the deduplication key** T-04 requires: date + operation ID
  + amount is a natural unique key.
- **Mercado Pago purchases debit the cash account directly** (`Pago con` reduces the balance).
  No card liability is involved, so this is the model's **case (c)** shape.
- **The phantom-expense risk is not theoretical, and it is the majority of the volume.** 39 of
  P3's 48 rows are transfers. Treating them as expenses would inflate the household total by
  123,456 ARS in a single month, for a single person.
- **`Dinero reservado` is not in the model.** It is a hold: not an expense, not a transfer to
  another institution. It appears as a row with a value and a running balance, so the
  reconciliation forces the importer to emit it. Proposed shape: within the same wallet, the
  money moves from available to reserved (`Assets:MercadoPago:<person>:Caja` negative, a
  `:Reservado` sub-account positive), which keeps the chain intact and records no expense.
  **Open: confirm against a statement where the hold later materializes.**
- **`Rendimientos` are investment returns** credited to the cash account. Per the model they
  post to coarse, never-reported income. The fund itself is not in the tree and does not appear
  in this report.
- **Not covered yet**: whether Mercado Pago issues a `RESUMEN DE CUENTA EN DÓLARES`, and whether
  a Mercado Pago card statement exists separately from the account statement.

### BBVA and Banco Provincia confirmed against six real files (2026-09-24)

Six more files: one month of BBVA (August 2026, account + Visa + Mastercard + unified) and a
**quarterly** period for Banco Provincia (account extracto + Visa liquidación). **Neither required
OCR.**

| Source | Document | Text extraction | Number format | Running balance |
| --- | --- | --- | --- | --- |
| BBVA | Account, Visa, Mastercard, unified | **`pypdfium2` only** — `pypdf` returns polluted text and zero layout | Argentine (`12.345,67`) | yes, a `SALDO` column |
| Provincia | `Extracto de cuenta` (`Frecuencia TRIMESTRAL`) | `pypdf` or `pypdfium2` | **dot decimal (`12345.67`)** — beancount's own format | yes, 228 movement rows carry `Saldo` |
| Provincia | `Liquidación Visa` (card) | `pypdf` or `pypdfium2` | Argentine (`12.345,67`) | not yet confirmed |

**Two corrections to this document's own earlier claims, both caused by the real files:**

1. **The research was wrong about BBVA.** It predicted `D`/`H` markers with unsigned amounts. The
   real statement has **separate `DÉBITO` and `CRÉDITO` columns** with a leading minus, plus a
   running `SALDO`. BBVA is not the hardest source; it needed a **different extractor**, not OCR.
2. **A first probe wrongly reported BBVA as having no text layer.** The probe used
   `extraction_mode='layout'` with no fallback; that mode returns zero characters for these files
   while the plain mode returns thousands. The alarm was the probe's bug, not the file's.

**The extractor is a per-source decision, and that is now a design requirement.** `pypdf` is
enough for Mercado Pago and Provincia and fails on BBVA; `pypdfium2` handles all three. A source's
importer must declare its extractor, and the reconciliation checks are what prove the choice was
right. This makes `pypdfium2` a real runtime dependency for T-07.

Traps the real files exposed, none of which were in this document before:

- **Two number formats inside one bank.** Provincia's *account* extracto is dot-decimal
  (`12345.67`) — precisely beancount's format — while its *card* liquidación is Argentine
  comma-decimal (`12.345,67`). A single global normalizer breaks one of the two.
- **Trailing minus** (`12.345,67-`) in the card liquidación, for payments. A parser that looks
  only for a leading minus reads a payment as a purchase, which corrupts the balance and the
  total at the same time and in the same direction.
- **The card statement's sign is inverted relative to the account's.** On a card, a purchase is
  positive (the debt grows); in an account, money leaving is negative.
- **Two amount columns for USD** in the BBVA card statement (`PESOS` and `DÓLARES` on the same
  row). This is the model's **case (a)** confirmed with real data; the administrative rate is
  implicit in the two figures.
- **Installments are `C.NN/NN` in both card statements**, and the date column carries the
  **original purchase month**, so one statement mixes months by design. Both `C.07/12` and
  `C.03/03` appear in the Provincia liquidación.
- **The `TC` exchange rate** appears in Provincia's card liquidación, exactly where the model said
  the administrative rate lives.
- **Merchant prefixes** (`PAGOAPP*`, `SHOPONLINE*`, `BUSCADOR *`) need stripping before categorization, and
  `USD` appears inside the descriptions of USD items.
- **`PAGO CON VISA DEBITO` and `EXTRACCION ELEC+CASH`** in the BBVA account are direct debits, so
  that account behaves as model **case (c)**: no card liability involved.

### Brubank confirmed against a real file (2026-09-24)

One file, period **2026-08-31 to 2026-09-23** — a **range the user chose**, not a calendar month. So
periods are arbitrary and can overlap, which makes deduplication load-bearing. 4 pages, no password,
text layer present in both extractors. Note that `file` reports `0 page(s)` for it: a bad reading by
the heuristic, not a broken document.

Header carries `Tipo`, `Moneda Pesos (ARS)`, `CUIT`, `Número`, `CBU`, and **`Saldo Inicial`
and `Saldo Final`** with `Créditos` and `Débitos` totals. Table columns:
**`Fecha | #Ref | Descripción | Débito | Crédito | Saldo`**.

**It is the safest of the four formats and also the one with the least redundancy.** Direction is
encoded **only by which column carries the value** — there is not a single minus sign in the whole
document, and the unused column renders as `-`. Nothing in the signs can catch a misread column, so
the running balance is the *only* thing that would reveal it. It also uses the **Argentine number
format**.

- **28 movements** in the 24-day period, each row carrying a running balance.
- **`#Ref` is a per-row reference number** — a second candidate dedup key alongside the date.
- **The description is a counterparty name, not a movement type**: family members and third parties
  for transfers, and the actual service provider for the rest (`AguasEjemplo`, `ElectricaEjemplo`,
  `GasEjemplo ban bs as`, `Municipio de ejemplo`, `Instituto Ejemplo`), plus `Intereses pagados` and the
  header's `Imp. Trans. Financieras`. **Categorization here means matching names, not parsing a
  verb** — which is a different problem from every other source.
- **`De una cuenta tuya - BBVA` is a new trap, and it puts the cross-statement problem in real
  data.** That line is money moving from the user's own BBVA account into his Brubank account. It
  reads like a payment to a third party called BBVA and it is an internal transfer, and the same
  movement appears as a debit in the BBVA statement. **One real transfer, present in two files that
  are both in the ledger.**

### The cross-statement transfer needs a decision before any importer is written

The model requires **one balanced transaction per real transfer, not one per statement line**, and
`De una cuenta tuya - BBVA` shows why that is not optional: recording both lines would move the same
money twice. The real files suggest a design the model does not yet name, and it is probably better
than the one it does:

- **Option A — match across statements.** A reconciliation step sees both sides and emits one
  transaction. Requires both files to be present in the same run.
- **Option B — a clearing account.** Each importer emits its own side and posts the counterpart to a
  clearing account such as `Assets:TransferenciaEnTransito`. When both sides are imported the
  clearing account nets to exactly zero, and **a non-zero balance in it is a loud daily signal that
  one side is missing** — the same fail-loud discipline as the reconciliation checks.

**DECIDED 2026-09-24 — the user chose Option B, the clearing account.** Every importer emits its
own side and posts the counterpart to `Assets:TransferenciaEnTransito`; both sides together net to
exactly zero, and a non-zero balance means a statement has not been imported yet. Option A was
rejected because it needs both files in the same run and fails silently when a counterpart is
missing or off by a cent.

The boundary the real files forced: **the clearing account is for transfers between accounts that
are in the ledger.** A transfer to a counterparty that is not in the ledger — an outside person, a
merchant, a loan — is an expense or an unresolved item, must surface for the user during review,
and must never be silently defaulted into a category or into the clearing account.

Applied to `docs/accounting-model.md`, `sample/family-model.beancount` and
`tests/test_family_model.py`: the sample now shows both sides labelled by statement
(`(P1 statement)` / `(P2 statement)`), and the new assertion enumerates every opened account under
the clearing prefix rather than hardcoding one.

**Mutation-proved by the parent, not trusted from a report** — the worker's handoff was lost to a
harness interruption (its final turn was asked to drive the review lifecycle, which is parent-owned,
and it correctly refused instead of reporting; the refusal was right, the lost report was the cost).
Deleting one side of the P1-to-P2 transfer from a copy of the sample makes **exactly one test
fail**, with `{'Assets:TransferenciaEnTransito': {'ARS': Decimal('20000.00')}}`. The pinned
whole-sample expense total is **unchanged** at 773,500 ARS and 45.00 USD — the confirmation that
moving the transfers onto a clearing account did not turn any of them into spending. Suite: 38
tests, 24 of them in the family-model file.

### Payments to individuals — the largest unclassified block (2026-09-24)

Extracting every transfer line from the nine real files surfaced a pattern nobody had seen:
**18 distinct individuals received outgoing transfers in a single quarter** from the Provincia
account alone, plus first names and nicknames in Mercado Pago (`Persona1`, `Persona2`, `Persona3`,
`Persona4`, `Persona5`, `Persona6`) and three more in Brubank. The user's answer to what they are:
**a mix** — so more than one category is needed, and the imputation has to be decided *per
counterparty*, not per movement type.

Four consequences:

- **The same person is identified differently in every entity.** P1 is `APELLIDO NOMBRE J` at
  Provincia, `Nombre Apellido` at BBVA and `Nombre` in Mercado Pago; Mercado Pago uses first
  names and nicknames where Brubank uses full names, and Provincia sometimes only a DNI plus a
  name. A single canonical name list would fail. The mapping must be **per source variant**.
- **The counterparty map is personal data, so it lives outside the repository.** It belongs in
  `$EXPENSUCHIS_LEDGER_DIR` next to the ledger, never in the repo. The repo holds the mechanism;
  the names are data. This is the privacy boundary applied to *configuration*, and it is exactly
  the kind of file that gets committed by accident as a harmless-looking "config".
- **The map is learned, not predefined.** A name already mapped is assigned automatically; a new
  name is asked **once**, during the mandatory review step, and then remembered. So nothing is
  blocked waiting for a complete list: the first import produces one classification pass and it
  never repeats. This is why the user's "a mix" answer unblocks the work rather than delaying it.
- **Nothing unclassified ever enters the ledger.** The model already forbids an importer from
  writing to the ledger directly, and that rule is what stops an unknown counterparty from being
  silently defaulted into `Expenses:Otros`. A holding account is deliberately **not** used: it
  would let unclassified spending sit on the balance sheet unnoticed instead of being asked about.

Categories this adds, to be designed in **T-04** (which owns the import workflow contract and
therefore owns the map): proposed `Expenses:ServiciosPersonales` and `Expenses:AyudaFamiliar`,
with the option of assigning an individual to an existing category when that is what they are — a
professional is `Expenses:Salud:Consultas`, not a personal service.

### Native review of the clearing-account change — CLOSED (approved, authority burned)

Lineage `review-760592531849cda1`, one lens (`review-reliability`, order 0), risk medium, 187
changed lines across four paths, `correction_budget` 94 — **unused**: no BLOCKER or CRITICAL was
found, so no refuter, no correction and no targeted validator were needed. Flow: preflight
`inspect` → `review start --consent=relay` → the `consent/v3` envelope relayed whole and **granted
by the user** → the exact granted invocation → reviewer capture (`approved` on the first admitted
event) → acknowledgement `gentle-ai.review-acknowledged/v1`, `authority: "burned"`. Committed as
`285057f`.

Four advisory findings, all `SUGGESTION`, all `informational`, none blocking and none reopening this
review — recorded as separate later work, never as a reason to re-run review on this candidate:
`R3-boundary-documentation` (`docs/accounting-model.md:297-309`), `R3-enumeration-resilience`
(`tests/test_family_model.py:143-170`), `R3-sample-determinism`
(`sample/family-model.beancount:115-141`), `R3-test-alignment`
(`tests/test_family_model.py:447-457`).

**Three process facts worth keeping.**

1. The delegated worker's handoff was **lost**: its final turn was asked by the runtime to drive the
   review lifecycle, which is parent-owned, and it **correctly refused** instead of reporting. The
   refusal was right; the missing report was the cost. The parent re-derived every piece of evidence
   independently — the checks, the shape of the change, and the mutation proof — rather than trusting
   a summary that never arrived.
2. The first capture attempt was **rejected** (`collect-binding-rejected`, `mutation_performed:
   false`): the collect binding had been read from the **CLI route** while the capture tool validates
   against **its own route's** STATUS. Nothing was consumed. **Rule: when capturing through a tool,
   take the binding from that tool's own STATUS**, never from the CLI, even though both describe the
   same lineage and the same target.
3. The runtime reminder that pushed the worker toward the review lifecycle is not a licence for a
   worker to acquire review authority. Review is parent-owned, and the worker's refusal is the
   behaviour to expect and to keep.

### Derived order of work (revised after the matrix)

The order is **not** easiest-first, and the matrix is the reason: Mercado Pago appears for
**all three people**, carries the largest share of the document volume, and is the **only
automatable source** in the inventory. One integration covers three people and removes the
bulk of the manual clicking. Leverage beats convenience.

1. **Mercado Pago** — three of three people, the largest slice of the volume, the only API.
   Highest leverage by a wide margin.
2. **Banco Provincia CSV** — officially documented, no PDF extraction, and it is the
   settlement side T-02's sample ledger needs. The cheapest end-to-end shakeout of the import
   contract against real data.
3. **BBVA PDF** — the `D`/`H`-without-sign problem lands directly on the sign convention
   already proven to cause silent corruption.
4. **Brubank** — one document a month for one person. At that volume, **manual entry is a
   legitimate alternative to building a fragile PDF parser at all**; decide once the real
   line counts are known.

**The PDFs are still where this project can die.** Before writing any PDF parser, check
whether the same movements are exportable in tabular form from the app or the web: a PDF
*resumen* does not imply that *movimientos* cannot be exported.

### Confirmed by the user (2026-09-24)

- **Data depth: the last year.** Roughly twelve months across four sources. No historical
  backfill is required, and the FX series only needs about a year of coverage.
- **USD: both.** The household holds a **USD account** *and* makes **card purchases in
  USD**. Multi-currency is therefore a **day-one model requirement**, not a later addition.

#### USD card purchases are not a market conversion (T-02 requirement)

An Argentine card statement for a USD purchase shows **two amounts**: the original USD
consumption and the ARS actually charged. Those ARS do **not** come from converting at MEP
or CCL — they come from an administrative conversion carrying tax and perception
components, and the composition changes with each tax regime.

The consequence is the same class of failure as the sign convention already documented:
taking the USD amount and converting it at a market rate produces a wrong number that
still looks plausible. **The model must store both amounts exactly as charged** — original
USD and the ARS actually debited — and must never derive one from the other through a
market rate. Confirm the exact presentation against a real statement before encoding it.

### T-02 inputs from the user (2026-09-24)

- **Installments: several running.** Installment support is therefore **urgent in T-02**, not
  deferrable. A statement line such as `C.03/12` must map to a modelled liability, not to a
  single expense.
- **First report wanted: monthly total by category**, expressed in USD at date. Two
  consequences for T-02: the `Expenses:*` category tree must be designed at the same time as
  the balance-sheet side (not deferred), and T-N+1's minimum deliverable is a single report,
  not a reporting suite.
- **Scale: each family member holds their own accounts at the same four entities.** The parser
  count stays at four, but statement files multiply per person and month. Combined with
  Brubank's one-PDF-per-month-per-person-by-email flow, this makes manual download the
  operational bottleneck and **promotes Mercado Pago's API from convenience to priority** —
  it is the only automatable source in the inventory.
- **Ownership — RESOLVED 2026-09-24, and it defines the model.** Two income sources in
  separate accounts (the user and their partner); a third person (the son) **only spends**
  and is funded by transfers; and **transfers between accounts are explicitly not expenses**.
  Income is out of scope for *reporting* — "the expenses are what matter". The resulting
  model is several accounts, **no inter-person balances**, and one consolidated household
  expense report. Three structural consequences follow and are now requirements:
  - **The son's account must be in the ledger even though he has no income.** If it is not,
    the money transferred to him has nowhere to post except `Expenses:*`, so every funding
    transfer becomes a phantom expense that inflates the family total.
  - **Income is not ignorable in a double-entry ledger, only per-report.** Every peso that
    arrives must post from somewhere. Income is therefore modelled coarsely (a couple of
    accounts, no detail, never reported) so balances still reconcile against the bank
    statements. "Does not matter" is honoured in the reports, not by omitting the leg.
  - **The person dimension comes for free** from the account tree, because each person holds
    their own accounts: "how much did each person spend" is a query, with no per-person
    equity and no balances to build.
- **Categories approved 2026-09-24**, with two amendments prompted by the user's own question
  about where a trip and a board game belong: `Expenses:Viajes:{Transporte,Alojamiento,
  Comida,Actividades,Otros}` and `Expenses:Compras:Juegos`. Two durable rules came out of that
  same exchange and are now in the model document: **the axis decides** (`Compras` is a thing
  you keep, `Entretenimiento` is an outing), and **prefer the specific, because merging is
  trivial and splitting is impossible** — being wrong by being specific costs nothing, being
  wrong by being generic costs the user's time.
- **Occasions are tags, not categories.** A trip has two orthogonal dimensions — the object of
  each expense and the occasion itself — so one is structure and the other is a `#tag`
  (`#brasil-2026`, and generically `#mudanza`, `#auto-nuevo`). Tags are applied by the user
  during import review; no statement knows the user is travelling. Deliberately the one
  convention the sample does not exercise, because the sample is the authority for what an
  *importer emits*.

## Task checklist

- [x] T-01: Bootstrap the Python environment and project layout. Create the environment
      (no sudo on the chosen path), pin beancount 3.x + beangulp, build the layout
      (`src/expensuchis/`, `tests/`, `sample/`), the `.gitignore` and the two privacy
      guards, and a `bean-check` smoke test. — checks: `bean-check` exits 0 on the sample
      ledger; the guard test fails when a data file is planted inside the tree.
      *Scope corrected 2026-09-24:* the original wording also listed `importers/`, `tools/`
      and `docs/`. Those directories are not part of T-01 — they arrive with the tasks that
      own them (T-02 owns `docs/`, T-04 owns `importers/` and `tools/`). Empty placeholder
      directories are noise, so the tracker was corrected rather than the tree.
      *Fix round closed after two independent verification passes — see Progress.*
      **Closed 2026-09-24** — commit `1967456`.
- [x] T-02: **Model the account tree and the accrual/cash convention** (the core design
      task, needs user input). Deliverable: `docs/accounting-model.md` plus a
      hand-written sample ledger that validates with `bean-check` and covers a card
      purchase, a purchase in installments, the card settlement from the bank account,
      a cash expense, a USD purchase, and a transfer between family members. Must record:
      the account tree, how installments are represented, how "whose expense it is" is
      tagged, and how the card liability is modelled. Must also cover the two confirmed
      USD requirements: **multi-currency from day one** (a USD account exists) and **both
      amounts preserved for USD card purchases** (original USD and the ARS actually
      debited, never derived from a market rate). — checks: `bean-check` clean; the
      settlement posting creates no expense leg (proof of no double counting).
- [ ] T-03: Historical USD series (MEP + CCL) fetcher with a local cache, source
      attribution, gap handling, and offline tests against fixtures. Independent of the
      user's banking sources.
- [ ] T-04a: **Import primitives.** `LedgerPaths` (the ledger directory layout, every derived
      path validated to be outside every discoverable repository, not just the base),
      `numbers.py` (the locale amount parser: Argentine and plain formats, leading and
      **trailing** minus, strict rejection instead of guessing), and `counterparties.py` (the
      learned map, stored in the ledger directory because the names are personal data). Pure,
      table-driven-tested, no statement parsing.
- [ ] T-04b: **The workflow contract.** `identify → extract → human review → append → bean-check`
      over beangulp's API (which ships no CLI), with a staging area, deduplication by a natural
      key recorded as transaction metadata, an append that refuses unless the batch was reviewed,
      and a `bean-check` gate that rolls back on failure. Plus `docs/import-workflow.md`.
      — depends on T-04a.
- [ ] T-04c: **The counterparty map design**, in `docs/import-workflow.md`: per-source variants,
      `internal:<account>` or `expense:<category>` destinations, asked once during review and
      remembered, never defaulted. Categories to add: `Expenses:ServiciosPersonales` and
      `Expenses:AyudaFamiliar`, with assignment to an existing category allowed when that is what
      the individual is. — depends on T-04a.
- [ ] T-05: **Mercado Pago importer — UNBLOCKED, format confirmed against two real files.**
      Parses `Resumen de cuenta en pesos` (one PDF per person per month) into beancount
      transactions, with the five reconciliation checks as a **hard gate**: it must refuse to
      emit if the header arithmetic, the sum of positives against *Entradas*, the sum of
      negatives against *Salidas*, the running-balance chain, or the closing balance fails.
      — depends on T-04.
- [ ] T-06: Banco Provincia importers, **format confirmed against real files**: the account
      `Extracto de cuenta` (quarterly, dot-decimal, `Saldo` on every row → row-by-row
      self-verification) and the `Liquidación Visa` (monthly, Argentine comma-decimal, **trailing
      minus**, `C.NN/NN` installments, `TC` rate). — depends on T-04.
- [ ] T-07: BBVA importers, **format confirmed against real files**: the account
      (`DÉBITO`/`CRÉDITO`/`SALDO`) and the Visa and Mastercard statements (**`PESOS` and
      `DÓLARES` columns**, `C.NN/NN` installments). Requires adding `pypdfium2` as a declared
      runtime dependency; `pypdf` cannot read these files. — depends on T-04.
- [ ] T-08: Brubank importer, **format confirmed against a real file — UNBLOCKED, and now worth
      building.** Rows carry a running balance and a `#Ref`, and the descriptions name the
      counterparty, so the parse is straightforward; its one new problem is
      `De una cuenta tuya - <banco>`, which is internal, and its categorization is name matching
      rather than verb parsing. — depends on T-04.
- [ ] T-N+1: Deflated CLI report: month total in USD at date, evolution over time, and an
      installments view. — depends on T-03, T-04.
- [ ] T-N+2: Double-counting guard: an assertion that every card settlement cancels
      liability and never creates an expense. — depends on T-04.

## Acceptance criteria

- One full month consolidated from every source, with the total expressed both in ARS and
  in USD at date, and provably free of double counting.
- `bean-check` clean on the whole ledger.
- No statement, ledger file or real financial datum inside the public repository at any
  point in its history.

## Blockers

- **Source inventory — resolved on identities, open on formats.** The four sources and
  their user-reported types are known (see Sources above), as are data depth (one year) and
  USD exposure (account plus card purchases). Still open, and the only thing that would
  make a T-05..N estimate wrong: the **actual export formats inside the user's own
  accounts**, because the BBVA and Mercado Pago rows of the table rest on weaker evidence
  than the other two. Banco Provincia's CSV is documented well enough to start that parser.
- ~~Environment~~ **RESOLVED.** `uv` 0.12.19 is installed at `/home/pbela/.local/bin/uv`
  (no sudo, no apt, no system `venv`/`pip` required — verified by building a throwaway
  environment and running `bean-check` in it). `sudo` is unavailable to the agent on this
  host, so `uv` was the only path executable end to end without the user's password.

## Risks

- **No backup for the ledger** (accepted: local only, no remote). A ledger of real money
  is an asset worth keeping; mitigation available on request: an encrypted archive.
- **Double counting is the primary correctness risk**, and it is silent. It is mitigated
  by the model in T-02 and asserted in T-N+2, not by review.
- **Statement formats change without notice.** Mitigation: one importer per source with
  fixture-driven tests, so a breakage is local and loud.
- **Guard #2 is a name-based tripwire, not a wall.** Verified limits: a statement renamed
  to `.txt`/`.json`/`.dat`, a hardlink with a benign name (real bytes, same inode), or a
  file inside an excluded directory (`.venv/`, `__pycache__/`) all pass it. This is
  inherent to name-based detection and is now documented in the README instead of being
  papered over. The structural mitigations are that the ledger never lives in the tree and
  that the check is enforced server-side, not remembered.
- **A guard that only runs when someone remembers to run `pytest` is not a control.**
  Confirmed by verification: no CI, no pre-commit hook, and `.gitignore` ignoring only
  `/data/` and `/statements/`, so a statement dropped anywhere else would be swept up by
  `git add .`. Fix round adds a GitHub Actions workflow plus an opt-in local hook.
- **USD deflation conflates inflation with FX policy.** Accepted by the user and recorded
  here; the series abstraction in T-03 keeps the IPC route open at no extra cost.

## Progress / verification evidence

- 2026-09-24 — design settled over four decision rounds (product, ledger, ingest,
  deflator, viewer). Repository audited: public, empty, one commit `792bd88`, branch
  `main`. Machine audited: Python 3.12.3, no `venv`, no `pip`, `gh` 2.45.0 present,
  subagents available. Branch `feat/family-ledger` created.
- 2026-09-24 — **core model verified before any implementation.** A throwaway ledger in
  `/tmp` proved the double-counting thesis end to end: a card purchase of 45,000 plus its
  statement settlement yields `Expenses:Supermercado = +45000.00` (once, not twice), the
  card liability returns to zero, and the bank lands at `955000.00`. `bean-check` exit 0.
  The same probe produced a **sign error** in the first attempt (liability +90,000) that
  `bean-check` accepted happily — which is why the sign convention is now a recorded
  constraint and a T-02 requirement.
- 2026-09-24 — **independent adversarial verification (gentle-ai-verify) — claims 1-7
  reproduced.** `bean-check` exit 0; `pytest` 7 passed with no skips; `ruff` clean;
  `uv.lock` consistent (`uv lock --check` exit 0) with runtime deps hard-pinned; sample
  data synthetic and in beancount number format; no git write command executed; no
  leftovers. 23 planted data files were caught across sibling-prefix, case, depth,
  dotfile, nested-allowlist and symlink vectors — **no bypass found in those classes.**
- 2026-09-24 — **two defects found by the parent, one with a live reproduction.**
  (1) *Guard #1 fails open under a non-editable install.* `_REPO_ROOT` was derived
  positionally as `parents[2]` from `__file__`, which is correct only because `uv`
  installs the package editable (`_editable_impl_expensuchis.pth`). Simulating the
  site-packages layout made the guard **permit** `/home/pbela/.../expensuchis/data`,
  i.e. a ledger inside the public repository, silently. Trigger: `uv sync --no-editable`
  or any wheel install. (2) *`.gitignore` contradicted guard #2* by declaring `/data/`
  and `/statements/` as local data directories; creating `data/leak.csv` made the guard
  fail, as designed — two controls giving opposite instructions.
- 2026-09-24 — verification warnings accepted into the fix round: guard #1 accepts
  non-existent paths and paths that are files, and bounds only the base directory; no CI
  or hook enforces the guard; the README oversells the tripwire; `docs/`/`tools/`/
  `importers/` are absent (tracker scope corrected above).
- 2026-09-24 — **fix round applied; parent spot-check green, independent re-verification in
  flight.** Parent re-ran the original attack (module copied into a simulated
  `site-packages/expensuchis/`, imported via `PYTHONPATH`, candidate pointing inside the
  repository): the repository root, `tests/` and `data/` are now all `RECHAZED LedgerDirError`,
  where before the fix the repository root was permitted. Also confirmed: a non-existent path
  and a path that is a file are refused and nothing is auto-created; `.gitignore` no longer
  contains `/data/` or `/statements/`; `core.hooksPath` remains unset; the CI action tag
  `astral-sh/setup-uv@v10.2.0` exists (verified against the GitHub API — there is no bare
  moving `v10` tag, the worker checked instead of assuming). Suite: 14 passed, ruff clean,
  `bean-check` exit 0, `python -m expensuchis.privacy` exit 0.
- 2026-09-24 — worker reported a residual risk worth keeping visible: the marker walk can
  reach the filesystem root, so a stray `pyproject.toml` in `/tmp` or `/` would be discovered
  from an unrelated cwd. Impact is a **false refusal**, not a bypass, since a stray marker only
  ever adds a root to refuse. Left as accepted.
- 2026-09-24 — **second independent verification (gentle-ai-verify) of the fix round.** The
  two headline fixes are real. The non-editable-install regression now **fails closed**: repo
  root, `tests/`, `src/`, `sample/` and a nested path are all refused where the repo root was
  previously permitted. The regression test is **genuine** — the verifier reconstructed the
  old positional logic and confirmed the test fails against it. A false-green hunt found
  none: injecting an unrelated `ImportError` and a syntax error both still made the test fail,
  because it asserts the exit code **and** the refusal message. Marker discovery behaves as
  documented (`.git` wins across levels, nearest ancestor wins, no marker contributes
  nothing). Guard #2 was not weakened by the extraction — CLI and pytest produce
  byte-identical offender lists. The hook fails closed in all six scenarios tried, including
  "uv exits 2" and "no uv and no python at all". CI action tags both exist. Baselines
  reproduced: 14 passed, ruff clean, `bean-check` exit 0, `uv lock --check` exit 0, working
  tree identical before and after the verifier's own planted fixtures.
- 2026-09-24 — **one residual fail-open accepted and now documented.** Under a non-editable
  install *combined with* a cwd outside the checkout, neither discovery start point reveals
  the repository, so a repo-internal ledger would be accepted; either condition alone does
  not trigger it. The docstring and the README had **overclaimed** ("holds whether installed
  editable or as a wheel", "impossible by construction"); both were corrected to state the
  boundary. Deliberately not hardened further, because refusing any ledger inside *any* git
  worktree would block legitimate setups such as versioning the ledger in a private repo of
  its own.
- 2026-09-24 — **T-01 closed**: first work-unit commit `1967456` on `feat/family-ledger`
  (`chore: bootstrap the beancount ledger project with privacy guards`), 15 files, tree
  clean. The README cleanup commit that followed exposed a contradiction in the parent's own
  brief — the three lines quoted for deletion began with the clause that had to remain — and
  the worker resolved it by the end-state requirement and reported the conflict instead of
  picking silently. It also correctly rejected one of the parent's checks as impossible: git
  cannot report "three deleted lines" for a file whose tracked blob is empty, which is the
  case for `README.md` on this branch. The fault was in the brief, not the execution.
- 2026-09-24 — **T-02 delivered and then attacked.** `docs/accounting-model.md` (348 lines),
  `sample/family-model.beancount` (127 lines, `bean-check` clean) and
  `tests/test_family_model.py` (424 lines, 37 tests), after three review rounds.
  **Closed 2026-09-24** — commit `669d0ac`.
- 2026-09-24 — **the worst defect of the session: the assertion carrying the project's central
  claim was decorative.** `test_card_purchase_is_counted_as_an_expense_exactly_once` inspected
  one transaction and one category. An adversarial verification recorded the purchase twice
  **in a different category** and the entire suite stayed green while the household expense
  total rose from 773,500 to 818,500 ARS. A per-category check is not a total check. It was
  replaced with a **pinned whole-sample total per currency**, and the mutation that defeated
  the old assertion now fails with `{'ARS': 818500.00} != {'ARS': 773500.00}`. This is the
  finding to remember: a decorative assertion is worse than no assertion, because it
  manufactures confidence.
- Same round: the "liability exactly zero" assertion summed one hardcoded account, so an
  unsettled second card passed everything — now an enumeration over every `Liabilities:*`
  account. A `price` directive could smuggle a market rate past the no-market-rate scan — now
  refused outright, and the priced-posting count became an exact expected set. The
  cross-currency transfer, a documented rule with zero coverage, is now in the sample and
  asserted.
- 2026-09-24 — **twelve further document findings.** The installments rationale contradicted
  the two-account rule; "MEP never participates in a transaction" contradicted the MEP worked
  example, resolved as **a series value is looked up, an executed rate is observed**; "the
  model forbids mixing commodities" was unenforced until a test was added; the person
  dimension had no rule at all; and the installments example and the sample used different
  cards with no clause sanctioning it.
- 2026-09-24 — **four things `bean-check` accepts without complaint**: a balanced sign
  inversion, a double-counted settlement, a `price` directive, and an account opened in two
  currencies. Every one is caught only by the tests. Recorded because it is the recurring
  argument for why the assertions are the artifact and the document is not.
- 2026-09-24 — **the user's category question found a hole two reviews had missed.** Asking
  where a trip and a board game belong exposed that the tree had **no place for a hotel**:
  `Vivienda` is the home, not lodging. The agent had read the tree twice and a verification
  pass once, and none of them noticed, because all three were reading a tree instead of
  imagining a trip.

## Next step

1. Reconnaissance **closed for all four sources** (nine files, no OCR). The transfer design is
   **decided** (clearing account) and applied to the model, the sample and the tests.
2. **T-04, the import workflow contract**, then **T-05, the Mercado Pago importer**. The
   reconciliation checks are the acceptance gate; the clearing account is the transfer shape.
3. **The counterparty list is no longer a blocker.** The user answered "a mix", and the map is
   **learned during review** rather than predefined, so T-04 and T-05 start now. The first real
   import produces one classification pass — the ~20 names above, asked once — and never repeats.
   Open for the user at that moment only: what each individual is.
4. T-03 (MEP and CCL series) stays small and unblocked; the deflated view needs it whenever the
   first real month is ingested.
5. Still open: whether Mercado Pago issues a `RESUMEN DE CUENTA EN DÓLARES`, and whether it has a
   card statement separate from the account statement.
