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
  for transfers, and the actual service provider for the rest (`Aguas Ejemplo`, `Electrica Ejemplo`,
  `Gas Ejemplo ban bs as`, `Municipio de ejemplo`, `Instituto Ejemplo`), plus `Intereses pagados` and the
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
account alone, plus a set of first names and nicknames in Mercado Pago and three more in
Brubank. The user's answer to what they are:
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
`5b92142`.

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
- [x] T-01b: **The leak guard — incident work, not in the original plan.** Opened after real
      identifiers taken from the statements were found in this repository's own history (see
      *The leak guard*). Deliverable: `src/expensuchis/leakguard.py`, its wiring into
      `.githooks/pre-commit`, and the reviewed baseline that lives with the data in the ledger
      directory. — checks: the sweep fails on a planted real token; an **absent** ledger
      directory prints the loud "cannot run" line and allows the commit; a ledger directory
      that is *configured but unusable* exits 2 and refuses the commit. **Closed 2026-09-24** —
      commits `7dbf703` (the guard, alongside the import-primitive warnings) and `fec9a99`
      (the README and docstrings corrected to claim only the behaviour that was measured).
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
- [x] T-03: Historical USD series (MEP + CCL) fetcher with a local cache, source
      attribution, gap handling, and offline tests against fixtures. Independent of the
      user's banking sources.
      **Source decided 2026-09-26 (owner):** ArgentinaDatos, `GET
      https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}` — `bolsa` (MEP, from
      2018-10-29) and `contadoconliqui` (CCL, from 2013-01-02); one call returns the whole
      series as `[{casa, compra, venta, fecha}]`. Public, no auth, MIT, **unofficial** (fed by
      DolarApi); the BCRA publishes neither series. Mitigation: every cached quote carries its
      source, and the series sits behind a source interface so a switch is not a migration.
      Route: delegated direct (writer trigger: module + cache + tests + fixtures). TDD strict,
      runner `uv run pytest`.
      **Closed 2026-09-26** — commit `13ba9f6` (`fx.py`, `LedgerPaths.fx_dir`/`fx_series`, 33
      offline tests, two public fixtures). RED observed (`ImportError` on `expensuchis.fx`), then
      `uv run pytest -q` 588 passed / 2 skipped, `ruff check .` clean. Assessed medium
      (`slice_budget_reached`, 817 lines); owner granted review; native review
      `review-55a8b979a3aa5d30` (reliability lens) **approved**, acknowledged and burned. No CLI
      subcommand yet. Leak guard could not run at commit (`EXPENSUCHIS_LEDGER_DIR` unset; content is
      public FX data). Non-blocking follow-ups from the review, open:
      R3-001 (WARNING) an empty payload passes validation and `refresh` overwrites a good cache with
      zero quotes; R3-002 (WARNING) a cached `NaN` escapes as `decimal.InvalidOperation` instead of
      `CacheError`, `Infinity` loads, and cached rates are not required to be strings; R3-003 most
      `_read_cache` rejection branches are untested; R3-004 `http.client.HTTPException` (e.g.
      `IncompleteRead`) escapes `fetch` unwrapped instead of `FetchError`.
      **R3-001 and R3-002 fixed 2026-09-26** in `493043d` (RED: 7 failed; GREEN: 599 passed / 2
      skipped, ruff clean). Assessed medium, `review_due=false` (`under_budget`, 120 lines): pending
      in the slice from boundary `7342258` until a later commit reaches the budget.
      **R3-003 and R3-004 fixed 2026-09-26** in `849dc62`: `http.client.HTTPException` is now
      wrapped as `FetchError` (RED: 2 failed; GREEN: 624 passed / 2 skipped, ruff clean); 23 cache
      structure rejection cases added, all passing on first run as characterization (no behaviour
      change). Still untested: the `read_text` `OSError` branch of `_read_cache`. Slice from
      `7342258` assessed medium, `under_budget` (246 lines): review still pending in the slice.
- [x] T-04a: **Import primitives.** `LedgerPaths` (the ledger directory layout, every derived
      path validated to be outside every discoverable repository, not just the base),
      `numbers.py` (the locale amount parser: Argentine and plain formats, leading and
      **trailing** minus, strict rejection instead of guessing), and `counterparties.py` (the
      learned map, stored in the ledger directory because the names are personal data). Pure,
      table-driven-tested, no statement parsing. **Closed 2026-09-25** — commits `579e466`
      (the primitives), with `7dbf703` closing the warnings that review left open. The checkbox
      stayed unticked through the leak-guard incident; corrected in the 2026-09-25
      reconciliation rather than left implicit.
- [x] T-04b: **The workflow contract.** `identify → extract → human review → append → bean-check`
      over beangulp's API (which ships no CLI), with a staging area, deduplication by a natural
      key recorded as transaction metadata, an append that refuses unless the batch was reviewed,
      and a `bean-check` gate that rolls back on failure. Plus `docs/import-workflow.md`.
      — depends on T-04a. **Closed 2026-09-25** — native review `review-5b534756e599835c` approved
      after one bounded correction (four CRITICAL findings, all candidate-caused), commits
      `ab41013`, `0823343`, `e08f0d3`, `cde0e9a`. See *Native review of T-04b* below.
- [ ] T-01c: **Format the tree once under `ruff format` and add it to CI.** Nine files predate
      the formatter and drift; CI's gate is `ruff check .` only, so nothing is broken today, but
      the drift is invisible until someone runs `ruff format --check .` and finds nine red files.
      Deliberately not done as part of T-04b: it is a large cosmetic diff across guarded files and
      belongs in its own reviewable unit. Discovered by T-04b's brief, which asked for a check the
      project never had.
- [x] T-01d: **Redact the leak guard's failure output.** A finding prints the offending token *and*
      the statement path it came from — correct for a human at a terminal and a leak when an agent
      runs the guard, because the agent's model is a remote API. Running it once during T-05a put
      statement tokens and statement paths into the parent's context. The guard needs a redaction
      mode (token shape and file, never the token; a content hash, never the path), or the harness
      must filter its output. Until then the parent pipes it through a masking filter, which is a
      workaround rather than a control. **Extended 2026-09-25 while wiring T-05b:** the same class of
      leak lives in the pipeline's own refusals. `importer-raised` embeds `statement.name` — the
      file's basename, which for at least one source names its holder — and the CLI prints it after
      `refused:`. T-05b removed the reader's own path from that message (the importer wraps a reader
      failure with the exception class only), but the basename comes from the pipeline (T-04b) and
      stays until this task redacts it.
      **Decided 2026-09-26 (owner): redacted by default, `--reveal` to opt in.** A finding prints
      the token's shape, a content hash and the repository file it was found in; never the token,
      never the statement path. The raw token and statement path print only under an explicit
      `--reveal`, meant for the owner at a terminal. The pipeline's refusals replace the statement
      basename with a short content hash. Rejected: full-by-default with an opt-in `--redact`,
      because safety would depend on the caller remembering a flag, which already failed in T-05a.
      Route: delegated direct (writer trigger: guard, pipeline, CLI, tests, docs). TDD strict,
      runner `uv run pytest`.
      **Closed 2026-09-26** — commit `db788f0` (`redact.py` shared `content_hash`, guard
      `format_finding`/`format_result` and `--reveal`, `importer-raised` names the statement by
      hash). RED: collection errors on the missing `expensuchis.redact`/`format_finding`; GREEN: 632
      passed / 2 skipped, ruff clean. Hooks pass no `--reveal`; CI never runs the guard. The slice
      from `7342258` (T-03 fixes `493043d`, `849dc62` plus this commit, 555 lines) was assessed
      **high** (`process_boundary` in `leakguard.py`); owner granted; four-lens native review
      `review-f5a56760c4aedbdd` **approved** with no correction, acknowledged and burned.
      Non-blocking follow-ups, open, in the order they matter:
      - **The hash is reversible** (R1-001/R3-003): an unsalted 12-hex sha256 plus the exact length
        and class lets a reader brute-force numeric tokens (a CUIT in minutes) and dictionary-attack
        names; `redact.py`'s docstring over-promises. Needs a keyed hash (local secret in the ledger
        dir) or dropping the hash and length from the default output.
      - **The refusal still quotes the importer's exception text** (R1-002/R3-002): an `OSError` or
        parser error carrying the statement path puts the basename back into `refused:`.
      - **`read_bytes()` inside the except handler** (R4-001/R3-001/R2-004): an unreadable statement
        turns the typed refusal into an unhandled `OSError`; hash before the `try` or guard it.
      - Readability/test gaps: `format_finding` has no `reveal` default despite its docstring
        (R2-001); two `test_fx.py` comments overstate coverage (R2-002, R2-003); the two R3-004 tests
        could be parametrized (R2-005); no test for the line-0 `(line unknown)` rendering (R3-004).
      - Left out by the writer: no pipeline `--reveal`; `STATEMENT_MISSING` still prints the path
        the caller typed; `mercadopago_importer._unclassified_message` prints raw counterparty names
        by documented design.
      **The three WARNINGs fixed 2026-09-26** in `ba638f1`: the hash is an HMAC under a 32-byte key
      at `$EXPENSUCHIS_LEDGER_DIR/redaction.key` (created on first use, 0600, a corrupt key raises,
      no unkeyed fallback); the refusal carries only the exception class except each importer's
      `CounterpartyClassificationError` and `StatementReadError`, whose messages are built to be
      shown; an unreadable statement or key yields `unreadable`. R2-001 (`reveal` default) fixed
      along the way. RED: import/attribute errors on the new API; GREEN: 648 passed / 2 skipped,
      ruff clean. Assessed **high**; owner granted; four-lens review `review-eb4967afc32ba7c2`
      **approved** with no correction, acknowledged and burned. Open follow-ups from it:
      - **Key creation races** (R4-001/R3-001/R1-001, WARNING): a fixed `.tmp` name opened without
        `O_EXCL`, and `os.replace` over an existing key, so two first uses at once can each mint a
        key and one overwrites the other, making earlier hashes unmatchable. Create with
        `os.link`/`O_EXCL` and re-read after writing.
      - **The `unreadable` marker also covers a bad key** (R2-002, WARNING), blaming the statement
        for a key failure; and `import-workflow.md` names only `CounterpartyClassificationError` as
        preserved, not `StatementReadError` (R2-001, WARNING).
      - Suggestions: the `key=None` reason is hardcoded as "no ledger directory" (R2-003); the guard
        builds the key path by hand instead of via `LedgerPaths` (R2-004); comments cite review IDs
        (R2-005); no tests for the bad-key fallback (R3-002) or a preserved `StatementReadError`
        (R3-003); a read-only ledger dir makes a real leak exit 3 instead of 1 (R3-004).
      - Still by design: the unclassified-counterparties refusal shows raw names, and reaches an
        agent's model if an agent runs an import.
- [x] T-04c: **The counterparty map design**, in `docs/import-workflow.md`: per-source variants,
      `internal:<account>` or `expense:<category>` destinations, asked once during review and
      remembered, never defaulted. Categories to add: `Expenses:ServiciosPersonales` and
      `Expenses:AyudaFamiliar`, with assignment to an existing category allowed when that is what
      the individual is. — depends on T-04a. **Also owns one defect T-04b left in that document:
      the quick path starts with `expensuchis bootstrap` and never says the base directory must
      already exist.** `LedgerPaths` deliberately refuses a base that does not exist (a typo must
      not silently split the ledger across two directories), so on a fresh machine the first
      documented command fails with `refused: ledger-dir: ... does not exist` until the user runs
      `mkdir -p` himself. The tool behaviour is right and its message names the fix; the document
      is what is incomplete. Found by running the quick path instead of reading it. **Closed
      2026-09-25** — native review `review-83a9010a1ddd5288` approved with **no correction** (medium
      tier, one lens, 239 changed lines), commits `aea5ce0`, `3624fd9`. See *T-04c delivered* below.
- [ ] T-05a: **The Mercado Pago parser core — the format knowledge lives here.** Pure over
      *extracted text* (no PDF library in the parser): movements with their running balance, and
      the five reconciliation checks (header arithmetic, sum of positives against *Entradas*,
      sum of negatives against *Salidas*, the running-balance chain, and the closing balance) as a
      **hard gate that raises instead of returning partial rows**, plus a sixth self-check that the
      natural keys are unique inside one statement — a key that repeats is not a key. Ship with a
      **local probe that prints aggregates only** (file content hash, page and movement counts, one
      line per check) so the real statements can be verified without any statement content entering
      an agent's context. — split out of T-05 because the parser and the wiring are separately
      reviewable, and because the parser is the only place the format is encoded. **Closed
      2026-09-25** — commit `43defe0`, reviewed twice: `review-87443c8fe0b24ecb` (approved, then
      superseded by the fixture rewrite below) and `review-39398fe5ffe5a963` (approved with no
      correction). See *T-05a delivered* below.
- [ ] T-05b: **The Mercado Pago importer wiring.** The thin `pypdfium2` extractor declared per
      source, the natural key (date + operation id + amount) recorded as `key:` metadata,
      resolution through `CounterpartyMap` with the refusal message shape T-04c pinned, and
      registration in `get_importers()`. — depends on T-05a.
      *Why the split, recorded because it changes the plan mid-flight:* T-05 as written was the
      first importer end to end, which is roughly twice the review budget and mixes two different
      kinds of knowledge — the statement's geometry and the pipeline's contract. The parser can be
      proved by the reconciliation checks alone; the wiring can be proved by the pipeline's own
      gate matrix. **Closed 2026-09-25** — commits `b218459` (importer), `8805d26` (probe),
      `81cdc36` (docs); native review `review-9972c11f5e343edd` approved with **no correction**.
      See *T-05b delivered* below.
- [x] T-06a: **Banco Provincia account `Extracto de cuenta`** (quarterly, dot-decimal,
      `Saldo` on every row → row-by-row self-verification). Split out of T-06 on
      2026-09-25 because the two formats share almost nothing: the account is dot-decimal
      with a running balance, the card is comma-decimal with no balance chain. Recon
      (masked shape dump, 2026-09-25) settled the geometry: header `Fecha Concepto
      Importe Fecha Valor Saldo` repeats every page; the first data row is `SALDO
      ANTERIOR`; a movement is either a compact reference row (`Nº.XXXXX dd/dd-X.XXXXXX
      X:XXXXXXXXXXX <amount> dd-mm <saldo>`, no description) or a description row
      (`<VERB> DE ...` / `<VERB> TARJETA ...`), with wrapped descriptions spanning two
      lines and the amount/date/saldo on the continuation. Vocabulary (counts from the
      probe): `compra` 35, `pago` 12, `sueldo` 11, `intereses` 5, `crédito` 5, `depósito`
      5, `haberes` 3, `comisión` 3, `cargos` 2, `devolución` 1, `débito` 1, `recarga` 1.
      The reference rows carry no description, so their classification is a design
      decision to document (not silently default). — depends on T-04.
      **Delivered 2026-09-26** — commits `3daa513` (importer), `536a1a7` (probe),
      `e96e6d5` (the counterparty-identity contract). See *T-06a delivered* below.
- [x] T-06b: **Banco Provincia `Liquidación Visa`** (monthly, Argentine comma-decimal,
      **trailing minus**, `C.NN/NN` installments, USD rows, merchant `*` prefixes, **no**
      running balance, sign inverted: a purchase grows the card liability). Split out of
      T-06 with T-06a. — depends on T-04. **Reconnaissance closed 2026-09-26** (masked,
      one real file) and **four owner decisions recorded**: the `SU PAGO EN PESOS` row is the card
      payment and emits no entry (the extracto owns the cash movement), USD purchases are model
      case (b) against `…:VisaUSD` with no `@` price, and charge rows are recognized by shape
      (`Impuestos:Sellos` / `Impuestos:Percepciones`), any other shape refusing the import. The
      geometry and the two-sum reconciliation gate: see *T-06b reconnaissance* below.
- [ ] T-07: BBVA importers, **format confirmed against real files**: the account
      (`DÉBITO`/`CRÉDITO`/`SALDO`) and the Visa and Mastercard statements (**`PESOS` and
      `DÓLARES` columns**, `C.NN/NN` installments). Requires adding `pypdfium2` as a declared
      runtime dependency; `pypdf` cannot read these files. — depends on T-04.
- [ ] T-08: Brubank importer, **format confirmed against a real file — UNBLOCKED, and now worth
      building.** Rows carry a running balance and a `#Ref`, and the descriptions name the
      counterparty, so the parse is straightforward; its one new problem is
      `De una cuenta tuya - <banco>`, which is internal, and its categorization is name matching
      rather than verb parsing. — depends on T-04. **Reconnaissance closed 2026-09-26** (masked, one real
      file): see *T-08 reconnaissance* below. **Owner decisions 2026-09-26:** (1) `Intereses
      pagados` posts to `Income:<person>:Intereses`, as in Provincia; (2) `Imp. Trans. Financieras`
      is a header-only total and emits no entry, and a row carrying it refuses the import; (3)
      `#Ref` is the identity part of the natural dedup key (periods are user-chosen and overlap),
      its global uniqueness to be re-checked when a second, overlapping statement arrives. The
      two-digit year reads as 20aa. `De una cuenta tuya - <bank>` follows the 2026-09-24 clearing
      account decision. Units, as in T-06b: **T-08a** the pure-text parser with its reconciliation
      gate and fixtures; **T-08b** the importer wiring; **T-08c** the acceptance probe against the
      real file. TDD strict, runner `uv run pytest`. T-08a route: delegated direct (writer
      trigger: parser, fixtures, tests).
      **T-08a delivered 2026-09-26** — commit `a3377c1` (`importers/brubank.py`, 50 parser tests,
      two synthetic fixtures). One pre-commit correction by the parent: the first draft skipped
      every non-date line silently; the table is now an explicit per-page region that refuses any
      unrecognized line (RED: 2 failed; GREEN: 698 passed / 2 skipped, ruff clean). Assessed medium,
      `slice_budget_reached`; owner granted; native review `review-8c4658400d3ec133` (reliability)
      **approved**, acknowledged and burned. Open follow-up, WARNING: the region closes on
      `_HEADER_FIELD_RE` (any label-then-amount line, not only the five header labels) and on an
      unanchored period search, so a stray amount-bearing or period-shaped line inside the table
      closes the region and later rows on that page are skipped (reconciliation then fails loudly
      if they carry money, but the refusal points at the wrong cause). Assumptions T-08c must check
      against the real file: the header/recap and period line shapes, and whether a generation
      stamp falls inside the table region.
      **Region warning fixed 2026-09-26** in `24ce845`: the region closes only on one of the five
      header labels (shared `_header_field` helper) or an anchored footer line, and a movement row
      outside any region refuses. RED: 3 failed (two refused for the wrong cause via
      `ReconciliationError`, one silently accepted); GREEN: 701 passed / 2 skipped, ruff clean.
      Assessed medium, `under_budget` (171 lines): review pending in the slice from `3130fb3`.
      **T-08b delivered 2026-09-27** — `importers/brubank_importer.py` (wiring), the registry
      entry in `importers/__init__.py`, and `tests/test_brubank_importer.py` (28 tests, reusing
      the parser's own `minimal.txt`/`multipage.txt` fixtures). `pipeline.py`'s
      `_PRESERVED_MESSAGE_TYPES` allowlist also gained Brubank's `CounterpartyClassificationError`
      and `StatementReadError`, without which the CLI would have reduced Brubank's actionable
      refusal to its class name only, breaking the "never defaulted" contract every other source
      already gets. RED: 1 error (missing module, collection failure) then 4 failed on first pass
      (two test bugs: an incomplete stub map and a mutation that broke the header block instead of
      the running-balance chain; one real gap, now fixed by the allowlist change). GREEN: 729
      passed / 2 skipped, `ruff` clean. Route: delegated direct (writer trigger: importer module +
      registry + test file, 2+ non-trivial files). One design point resolved without a new
      decision: unlike Provincia/MercadoPago, every `ORDINARY` row — credit or debit alike — goes
      through the counterparty map, because Brubank's vocabulary has no verb to separate spending
      from income; the map's existing `internal:`/`expense:` contract is direction-agnostic (only
      the literal account after the marker is used), so no new prefix was needed. Two follow-ups
      for **T-08c**: confirm the header/recap/footer line shapes and the generation-stamp placement
      against the real file (carried over from T-08a), and confirm `Intereses pagados` never
      appears as a debit in the real file (this unit's fixed destination assumes it never does,
      mirroring only Provincia's positive-interest branch).
      **Slice review 2026-09-27** (`3130fb3..11b7059`, covering `24ce845` and `11b7059`, 1253
      lines): assessed high (`process_boundary` in the importer tests); owner granted; four-lens
      native review `review-cd6bec3fc5da0e6c` **approved**, acknowledged and burned. Non-blocking
      findings, recorded for T-08c: two WARNINGs (R3/R4, same root) — any date-prefixed line
      outside a table region now refuses, so a dated generation stamp or dated legal prose in the
      real file would reject a valid statement; and the footer match is both brittle (fullmatch:
      a trailing page counter after the period keeps the region open) and loose (a digit-free
      note followed by a period-shaped run closes it). Suggestions: no test pins the
      `CounterpartyClassificationError` preservation in `pipeline.py`, and four readability nits
      in `brubank_importer.py` and the parser tests.
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
  **Closed 2026-09-24** — commit `e24ac71`.
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

- 2026-09-24 — **the git history was rewritten to remove real identifiers.** Real names and one
  real aggregate amount taken from the user's statements had leaked into this repository through
  this tracker and through one module docstring that a verification pass missed. The working tree
  was scrubbed first, then `git filter-repo --replace-text` was run over all commits, and the
  commit references in this document were remapped from `filter-repo`'s commit map. The rewrite is
  verified by a **token sweep of every added line of every commit** against the statement
  vocabulary: 876 tokens, 76 intersections, and every one of them is generic banking vocabulary
  (`cuenta`, `saldo`, `crédito`, `pesos`) or a placeholder introduced by the scrub itself
  (`apellido`, `nombre`, `ejemplo`). No personal identifier and no real amount remains. `origin`
  was re-added after the rewrite, and the push is a separate, separately-authorised step.

  **The lesson, recorded because it cost real exposure:** the first sweep reported the tree clean
  and was wrong three ways — its patterns were longer than the leaked strings, single-word names
  were never extracted, and amounts without decimals were skipped. **A leak is found by sweeping
  tokens, not phrases**, because what leaked is a transformation of the source: truncated,
  reformatted, recased. The inverted sweep — take the tokens from the statements, look for them in
  everything ever committed — is also what found the leak site the verification pass missed.

  **Correction, and it matters: the leaked commits were never published.** GitHub held only
  `792bd88` — the initial commit with a one-line README — and this branch had no upstream and no
  push in its reflog. The exposure window was **zero**, no force-push was needed, and the question
  of overwriting published history was moot. The parent asserted exposure without ever checking
  which refs the remote actually carried, and the verification pass made the same assumption.

  The catch was still worth its cost, and for a better reason than cleaning up: **it stopped the
  leak before the first push.** Had the branch been pushed first, an ordinary `git push` would have
  published the identifiers permanently — precisely what every guard in this project exists to
  prevent, done by hand, by the agent. The rewrite is kept because it makes the first push clean by
  construction rather than by remembering.

  A second helper to the sweep was removed at the same time: its **stoplist had been masking real
  tokens.** Three household service providers named in this tracker were on that stoplist; it
  hid two of the three while the third survived. The final verification was re-run with
  **no stoplist at all** — 876 statement tokens against every added line of every commit, 76
  intersections, all reviewed and all either generic banking vocabulary or scrub placeholders.

### The leak guard (2026-09-24)

`src/expensuchis/leakguard.py`, wired into `.githooks/pre-commit` after the privacy guard.

- **Sweeps by token, not phrase**, which is the incident's lesson: a phrase extracted from a
  statement is longer than the transformed text that leaked, so it matches nothing.
- **No stoplist.** Instead a **reviewed baseline** lives with the data in the ledger directory
  (`leakguard-baseline.txt`), derived from a sweep whose entries were justified one by one. A
  baseline token is ignored; **any other intersection fails**, naming the token, the repository
  file and the source statement. The distinction is deliberate: a stoplist grows silently and
  hides things, whereas every baseline addition is a deliberate act against a reviewed list.
- **Three-way status, not two.** An *absent* ledger directory (unset or nonexistent — a fresh
  clone, or CI) prints a loud "cannot run; allowing the commit" and exits 0, because the
  repository must stay usable without the private data. A ledger that is *configured but unusable*
  (missing statements, an unreadable statement, the ledger inside the repo) is `UNREADABLE` and
  exits 2 — fail closed. An intersection is `LEAK` and exits 1.
- The index and baseline live **only** in the ledger directory. The token source is a seam, so the
  suite injects synthetic text and never needs a PDF or a ledger; `pypdfium2` is imported lazily,
  and adding it also serves T-07.

**Documented limits, so the guard is not read as a wall:**

- **The alphabetic floor is five characters.** A four-character provider name cannot be caught —
  and the incident itself named one. Lowering the floor is a one-line change that enlarges the
  baseline, which is why it is a decision and not a default.
- **Numeric normalisation is lossy by design.** `1,000.00`, `1000.00` and `100000` collapse to one
  token, so the numeric baseline is large: the synthetic sample shares round numbers with the real
  statements. That weakens amount detection to a **delta** check — a new amount in a new place
  still fails, which is the intent, but it is weaker than it first reads.
- **The finding output prints the source statement path**, and the Brubank filename contains the
  account holder's name. Fine locally; that output must not be pasted into a public log.

### T-04b decision record — the review gate is a digest, not a prompt (2026-09-25)

`T-04b` requires "an append that refuses unless the batch was reviewed". Review is mechanised as
**bytes bound by digest**, and this is the load-bearing design decision of the task:

- `extract` writes `staging/<batch-id>/proposed.beancount` — the exact text to append, produced
  by beancount's own printer from the entries the importer returned — plus `batch.json` (source,
  statement sha256, timestamp) and `report.txt` (the human-readable view of the batch).
- The human reads the report and the proposed bytes, then runs `approve`, which writes
  `approval.json` holding the **sha256 of `proposed.beancount`**.
- `append` recomputes that digest and refuses on any mismatch, so an edit made *after* review
  invalidates the approval instead of riding in on it. **The reviewed bytes are the appended
  bytes:** nothing is re-derived between review and write.

Why not interactive prompts: the operator is a developer, the whole batch has to be visible at
once and remain hand-editable, and a prompt cannot be re-read tomorrow. Why not a boolean
"reviewed" flag: it survives an edit, and the digest does not.

**Honest boundary, stated because this project states them:** the CLI cannot verify that a human
actually read the report. What it enforces is the separation — staging and appending are
different commands, and the approval is bound to exact bytes. `approve` proves the digest, not
the reading.

### T-04b scope note — the tool never invents an account name (2026-09-25)

`bootstrap` writes only the structural skeleton: `main.beancount` (options plus the two
`include` lines) and a starter `accounts.beancount` holding the equity accounts and
`Assets:TransferenciaEnTransito`. It **refuses to overwrite** an existing file, and it does not
generate the person or entity accounts: the real tree names the household's own accounts, which
are personal data and cannot be derived by the tool. The sample's `P1`/`P2`/`P3` labels are
placeholders; the real ledger's names are the user's decision, made by hand.

### T-04a closed (2026-09-25)

`paths.py` (`LedgerPaths`, every derived path re-validated individually), `ledger.py`
(`ledger_dir` + `assert_outside_repository`), `numbers.py` and `counterparties.py` are in and
green. Baseline re-run by the parent on 2026-09-25, working tree clean at `fec9a99`:
**192 passed, 1 skipped** (the skip is the opt-in `pdfium` marker) and `ruff` clean on the whole
source tree. The `T-04a` checkbox had been stale since before the leak-guard incident; the
reconciliation above corrects it, and the commit identity is recorded there as evidence rather
than left to the reader.

**Engram mirror refreshed on 2026-09-25.** The mirror at `odd/family-ledger/tasks` still held
the *first* version of this tracker — the version with T-01 and T-02 open — while the file had
moved five commits past it. A mirror that lags by that much is worse than no mirror, because it
would have been trusted on resume. Recorded because the failure mode is silent: nothing in the
workflow notices a stale mirror.

### T-04b delivered (2026-09-25) — the contract, before the review

Five modules and 256 tests. `src/expensuchis/pipeline.py` (identify → extract → report → approve
→ append, the dedup index, the append gate with byte-for-byte rollback),
`src/expensuchis/bootstrap.py` (the structural skeleton and the placeholder finding),
`src/expensuchis/cli.py` (the front end, beangulp 0.2.0 ships no console scripts),
`src/expensuchis/importers.py` (`get_importers()` — the seam T-05 fills), plus
`docs/import-workflow.md`.

**The parent did not trust the worker's green suite.** Fifteen mutation probes ran against the
load-bearing behaviours; the dishonest ones are the interesting half. Deleting
`extra_validations=HARDCORE_VALIDATIONS` from `ledger_errors` left the whole suite green — the test
named *"agrees with bean-check"* used an undeclared account, which both checkers catch with or
without the option, so the parity claim was **decorative**. The worker then reported a deviation
from the parent's own brief and was right: the list tripwire cannot kill that mutation, because it
never observes `ledger_errors`; the forwarding spy is what kills it. Both are kept, with distinct
jobs. Reading the code also found that `build_key_index` globbed `transactions/*.beancount`, so a
key hand-written into `main.beancount` was invisible to dedup — a silent double-count, the
project's primary risk. Index now derives from the loaded ledger root. All fifteen probes are
killed by the suite as committed.

### Native review of T-04b — CLOSED (approved after one bounded correction, authority burned)

Lineage `review-5b534756e599835c`, tier **high**, four lenses (risk, resilience, readability,
reliability), 9 changed paths, 2021 original changed lines, `correction_budget` 200.

The four reviewers returned **four CRITICAL findings, all candidate-caused and all
`deterministic`** — so no refuter was needed, and the parent corroborated each one by reading the
code before planning anything:

| id | lens | what it caught |
| --- | --- | --- |
| `R2-001-_proposed_chunking_contract` | readability | staging/append serialization rested on an undocumented `\n\n` block-boundary invariant; a printer change could mis-split a block. |
| `R4-bootstrap-partial-state` | resilience | three unguarded writes: a mid-write `OSError` left a half-bootstrapped ledger that `BOOTSTRAP_EXISTS` then refused to repair, because `main.beancount` was already on disk. |
| `R4-broken-pipe-unhandled` | resilience | an unguarded `print()` meant `head -n 1` produced a traceback, contradicting the docstring's promise that the output can be piped and grepped. |
| `R4-extract-silent-batch-overwrite` | resilience | `extract` overwrote an existing batch, so a re-run in the same second could replace the very bytes an approval had pinned. |

One bounded correction followed: append-side per-block verification (every staged block must parse
to exactly one entry, else `staging-corrupt`), bootstrap rollback with a `bootstrap-failed` reason
code, exit `141` on a closed pipe, and a `batch-exists` refusal in `extract`. A **targeted
validator** then approved the corrected candidate on the first admissible event. Acknowledgement
`gentle-ai.review-acknowledged/v1`, `authority: "burned"`. **Committed as four work units on the
same bytes the validator approved:** `ab41013` (the contract), `0823343` (bootstrap), `e08f0d3`
(the CLI), `cde0e9a` (the workflow document).

**Three advisory findings, all non-blocking, none reopening this review** — separate later work,
never a reason to re-run review on this candidate: `R3-ledger-gate-parity-pinned`
(`tests/test_pipeline.py:631-649`), `R4-double-ledger-load` (`src/expensuchis/pipeline.py:228-244`),
`R4-rollback-masks-post-write-dirty` (`src/expensuchis/pipeline.py:341-365`).

#### Five process facts worth keeping — every one of them cost a round

1. **A provider binding is opaque, and trimming it is a defect.** The first validator submission
   was rejected with *"collectBinding is unknown, expired, or belongs to a different session
   route"* because the parent had shortened the nested `validationRequest` object (dropping
   `fixFindings`, `fixClassifications`, `policyContent`) to keep the tool call small. Resending
   the binding **verbatim** produced the forecast immediately. Never re-render, drop or summarize
   a provider-issued binding, not even the parts that look redundant.
2. **The correction plan comes before the correction, not after.** The first plan submission was
   refused while the corrected tree was already in place; the identical binding was accepted the
   moment the working tree was restored to the frozen candidate. Restoring cost nothing only
   because both states had been snapshotted to `/tmp` *before* anything was touched — **take that
   snapshot before the first write, not after the first failure.**
3. **The provider's budget unit is not GNU diff's.** The parent measured the correction at 186
   changed lines (`diff -u`, counting `^+`/`^-` lines); the provider counted **217** against a
   budget of 200 and refused admission. Its number is reproduced by counting every diff line that
   starts with `+` or `-` except the `+++`/`---` headers, which also counts added source lines
   that themselves begin with `+` or `-`. Use that count whenever a correction budget is in play.
4. **Shrinking cost a guard, and that was the right trade.** To fit the budget the staging-side
   rebuild check and its test were dropped; the **append-side per-block verification stayed**,
   because that is the guard that actually prevents a mis-split — a block that does not parse to
   exactly one entry refuses `staging-corrupt`. Documentation prose was reduced to the three
   additive table rows that the new refusals and the new exit code require. Provider-scale total:
   217 → 159.
5. **An unassigned review role is a configuration failure, not a defect in the candidate.** The
   targeted validator could not run at all because `review-validator` had no model in
   `~/.pi/gentle-ai/models.json`; `review-refuter` was missing too and would have surfaced on the
   first inferential blocker. Both now route to `opencode/claude-haiku-4-5` — in the active
   routing *and* in both profiles, so a profile re-apply does not silently drop them.

#### One documentation defect the review could not have caught, and why it is not fixed here

Running the documented quick path end to end (not reading it) found that it starts with
`expensuchis bootstrap` on a machine where the ledger directory does not exist yet, and
`bootstrap` **refuses**: `LedgerPaths` requires the base directory to exist on purpose, because a
typo would otherwise be accepted and silently split the ledger across two directories. The
refusal message names the fix (`mkdir -p ...`), so the tool is right and the document is
incomplete. It is **recorded here for T-04c rather than patched now**, because the approved bytes
are the delivered bytes: `docs/import-workflow.md` was part of the reviewed candidate, the review's
authority is burned on that exact content, and a silent post-approval edit is precisely the class
of thing the digest gate exists to prevent. T-04c already owns that document and will be reviewed
as its own candidate.

#### One observed interaction with the privacy guard, now verified as transient

While the lineage held candidate views, `.git/gentle-ai/candidate-views/<uuid>/sample/*.beancount`
tripped guard #2: `pytest` was red and the local pre-commit hook would have refused a commit. The
tooling **removes those views when the lineage closes** — after acknowledgement,
`python -m expensuchis.privacy` exits 0 and the suite is green (256 passed, 1 skipped). No guard
change was made: the condition is transient, self-clearing, and reachable only while a review is
open, which is exactly the window in which this project does not commit. Recorded rather than
hardened, because hardening it would mean either teaching the guard about a tool's directory
layout or deleting another tool's state by hand.

### T-04c delivered (2026-09-25) — the boundary is the ledger, not the family

`docs/import-workflow.md` gains the counterparty-map contract in place of the gap it used to
declare: the key is `(source, raw_name)` exactly as the statement spells it, case-sensitive,
because the same person reads `APELLIDO NOMBRE J` at Provincia, `Nombre Apellido` at BBVA and a
first name at Mercado Pago; the destination is `internal:<account>` for a counterparty **this
ledger holds** and `expense:<category>` for money leaving for good; the map is append-only and the
last row for a key wins, so a correction is one appended row. Nothing is ever defaulted — there is
no holding account and no fallback to `Expenses:Otros`, an unclassified name stops the import — and
the document states **in bold that the refusal path and its message shape are a contract T-05
implements, not behaviour that exists today**, because `CounterpartyMap` stores and resolves rows
and nothing calls it from `extract` yet. That disclaimer is the difference between a design and a
false claim of working code.

`docs/accounting-model.md` gains `Expenses:ServiciosPersonales` and `Expenses:AyudaFamiliar`, and the
sample exercises both with one transaction each. Commits `aea5ce0` (model, sample, tests) and
`3624fd9` (the workflow contract and the quick-path fix).

#### The contradiction that had to be resolved, not tolerated

The model document said `Expenses:Familia` was **deliberately absent** because transfers to family
members are internal. T-04c asks for `Expenses:AyudaFamiliar`. Read carelessly those are opposites,
and the failure mode of leaving both standing is a reader who cannot tell which rule governs. The
resolution is now written down: **the boundary is the ledger, not the family relationship.** A
transfer into a family member's account that is *in the ledger* (P3's) stays `internal:` and is
never spending; support to a relative with **no account here** leaves the household for good and is
spending. The same surname can land on either side, and only the ledger decides which. The category
stays coarse on purpose: per-person categories would put real names into a public document.

#### What the parent's verification changed before the review

- **The pinned total's arithmetic did not add up as written.** The comment listed the previous
total *and* its components as if they were addends: 773500 + 45000 + 720000 + 8500 + 25000 + 40000
is not 838500. The value was right and the justification was wrong, which is the worse half — a
reader who checks it and finds it broken stops trusting the pinned number that catches double
counting. Rewritten as the components' sum, with the delta to the old total named separately.
- **A sentence about tags had become a trap.** The model document said the sample deliberately does
not exercise tags; the sample has carried structural markers (`#card-purchase`, `#installments`,
`#transfer-to-p3`) since T-02, and T-04c added two more. The claim now distinguishes the sample's
**scenario markers** from an **occasion tag** (`#brasil-2026`), which is the thing an importer
never emits.
- **The delegated handoff was truncated** — the worker's last turn was consumed arguing about the
review lifecycle and its report arrived without the RED evidence, the arithmetic, or the
contradiction quotes it was asked for. Everything above was re-derived by the parent:
**five mutation probes, all killed** — remove either new transaction, remove the
`Expenses:ServiciosPersonales` open directive, mis-categorise the service payment, or route it
through the clearing account, and the suite fails in each case. The pinned total was recomputed
independently (838500.00 ARS / 45.00 USD) and the clearing account still nets to exactly zero.

#### Native review of T-04c — approved on the first admitted event

Lineage `review-83a9010a1ddd5288`, tier **medium**, one lens (`review-reliability`), 5 changed
paths, 239 original changed lines, `correction_budget` 120 — **unused**: no BLOCKER or CRITICAL was
found, so no refuter, no correction and no targeted validator were needed. Acknowledgement
`gentle-ai.review-acknowledged/v1`, `authority: "burned"`.

**Two advisory findings, both non-blocking and neither reopening this review** — separate later
work: `R3-001` (WARNING, `tests/test_family_model.py:54`) and `R3-002` (SUGGESTION,
`docs/import-workflow.md:264`).

### The statement's geometry, obtained without reading a single word of it (2026-09-25)

The two real Mercado Pago statements were still where the first reconnaissance left them — and that
made the next question urgent, because **the agents' models are remote APIs**: putting a real
statement in a worker's context, or letting a worker open the PDF itself, would send the household's
financial data to a third party. The repository rule ("real financial data never enters this
repository") is necessary and not sufficient; the stronger rule this session adopted is **no agent
reads statement content, and no statement-derived string appears in a report, a commit or a tool
call.**

So the parser was designed against a **shape dump**: a local script extracts the text, replaces every
digit with `x` (length preserved, so column widths survive) and every alphabetic token **not on a
whitelist of generic statement vocabulary** with `x`s of the same length. What reached the parent,
and then the brief, was the template and its geometry with zero content — no name, no amount, no
operation id. It paid for itself immediately, because the geometry was **not** what the tracker
implied:

- Rows are **not one line each**. A movement is a *block*: a compact single line
  (`dd-mm-yyyy Rendimientos <id> $ <value> $ <balance>`), or a wrapped two-or-three-line block where
  the date is alone on its line, the description follows across one or two lines, and the final line
  carries the id and the two amounts. A parser that assumed one line per movement would have failed
  on the real files while passing any synthetic fixture its author invented.
- `Rendimientos` are the **volume**, not the exception: the small statement carries many of them
  between two real movements, so a wrong sign or a skipped row is invisible in the totals and
  caught only by the running-balance chain.
- Page furniture repeats the table header on every page (`Fecha … ID de …` / `… Valor Saldo`), so the
  parser must distinguish furniture from payload by **structure** — a payload line ends with two
  amounts — and let the reconciliation checks prove that nothing real was skipped.

**The rule for the agent, stated once so it is not re-learned:** the real bytes are only ever read by
ordinary local processes whose output is an aggregate — a content hash, counts, and one line per
check. Debugging information is masked at the source, which makes masking a requirement **on the
code**: a refusal message must not echo a raw statement line.

### T-05a delivered (2026-09-25) — the parser, and the day the leak guard earned its keep

`src/expensuchis/importers/` is now a package: `__init__.py` keeps the registry unchanged and
`mercadopago.py` (471 lines) holds everything known about the statement format — the block
structure, the frozen verb vocabulary, seven checks, and the masking rule for diagnostics.
`tests/test_mercadopago_parser.py` (376) with three synthetic fixtures, and
`tools/probe_mercadopago.py` (156): the only component that reads a real statement, and therefore
the only place that decides what a human may see of one. Commit `43defe0`.

**The masked shape dump paid for itself immediately.** The design input was the real text with every
digit replaced and every non-template word reduced to `x`s of the same length, so the geometry
survived and the content did not. It showed that a movement is a **block of one to three lines** (a
compact line, or a date line, a description, and a payload line ending in two amounts), that the
table header repeats on every page, and that `Rendimientos` rows are the volume rather than the
exception. A parser written against an invented fixture would have passed its own tests and failed on
the first real file. Against the two real statements it reconciles all seven checks on the **first
run**, with **13 and 48 movements** and **39 transfers in the larger one** — the exact counts this
tracker had recorded independently, before a parser existed.

**The one thing reconciliation cannot see, checked separately.** The operation id is taken as the
last token before the amount pair, and no reconciliation check validates that choice: a wrong id
still balances. A masked structural probe confirms every payload line carries one (13/13, 48/48),
that all of them are digits 12 to 13 characters long, and that they are unique per statement.

**Four of the seven checks are cross-validation, not independent alarms.** The chain plus the two sum
checks imply the header arithmetic and the closing balance, so no mutation can break exactly one of
the four. A test pins that rather than letting the matrix look stronger than it is; the other three
checks *can* fail alone, and the mutation matrix shows all seven individually reachable.

#### The leak guard refused the commit, and it was right twice

The first commit attempt was refused: **42 statement tokens** in the new content. Classified, they
were four *template* words (`Periodo`, `DETALLE DE MOVIMIENTOS`, `Valor`, `Movimiento` — every
document of the source carries them, and a fixture that omits them proves nothing), seven words the
fixtures had *invented* into plausibility (a Spanish merchant name, a city name, a banking noun),
and six round amounts. Only the template words are unavoidable. The fixtures were regenerated with
**non-word names** (`Zxqv`, `Qwerty`, `Plugh`) and **non-round amounts**, and the four template words
were added to the reviewed baseline with a written reason each (72 → 76). The fixture module's
docstring now states both properties, so the next person to edit a fixture does not "fix" the names
back into Spanish and trip the guard again.

**Two facts about the control, learned the hard way.**

1. **The guard scans the staged content, not the working tree.** Regenerating the fixtures changed
the files but not the index, so the guard kept reporting the *old* bytes until they were re-staged —
25 phantom findings that looked like a broken fixture. Recorded because the failure mode is a
misleading signal, and the next agent will read it the same way.
2. **Its failure output is written for a human at a terminal, and an agent is not one.** A finding
names the token *and* the statement it came from, so one run put a list of statement tokens and
statement paths into the parent's (remote) model context. The classification was still done —
through a masking filter written on the spot, and the tokens turned out to be generic words and
round numbers rather than names — but the control itself became a leak path. Opened as **T-01d**.

#### Native reviews of T-05a

The candidate was reviewed twice, and the second one is the one that counts:

- `review-87443c8fe0b24ecb` — medium tier, one lens, 1131 changed lines: **approved with no
correction**. Advisory, none blocking: `R3-001` (`mercadopago.py:194-228`, WARNING), `R3-002`
(`test_mercadopago_parser.py:315-328`, SUGGESTION), `R3-003` (`mercadopago.py:383-387`,
SUGGESTION).
- The leak-guard refusal then forced the fixture rewrite, so the approved bytes stopped being the
delivered bytes: **the amended candidate was reviewed again** instead of shipped on the first
approval. `review-39398fe5ffe5a963` — medium tier, one lens, 1137 changed lines: **approved with no
correction**, advisory `R3-BOUNDARY_DETECTION`, `R3-MASKING_COMPLETENESS` and
`R3-RECONCILIATION_DETERMINISM` (all SUGGESTION, none blocking). Authority burned on both.

Re-reviewing instead of disclosing the delta was the deliberate choice: a review that approves bytes
nobody ships is a receipt for the wrong artefact.

#### Two loose ends T-05b inherits

- **The probe has no test.** It is the component that touches real data and the one nothing pins:
the masking test covers the parser's diagnostics, not the probe's output surface.
- **The probe's generic exception path prints `{exc}`**, and an engine error can carry a real file
path. The parse paths are masked; that one is not.

Both were closed by T-05b (2026-09-25), with one correction to the record: the second claim was
**stale** by the time it was implemented. The generic extraction path already printed
`type(exc).__name__`, not `{exc}`; the one print with an unmasked shape was
`read error: {exc.strerror}`, which names the OS message rather than the path but was uniformised
to the exception class anyway. The audit that found this is in the T-05b section below.

### T-05b decisions (2026-09-25, before implementation)

Three questions the tracker had left open were settled with the user, because the parser
knows the statement's shape but not the household's chart of accounts:

- **How a statement declares whose account it is — per-person folder.** The cash account is
  derived from the path: `statements/MercadoPago/<person>/<file>.pdf` posts to
  `Assets:MercadoPago:<person>:Caja`. The `<person>` token is the same one the real ledger uses
  in its account names, so the derivation needs no lookup and no new declaration file. Chosen
  over a `--account` flag (three statements a month, three chances to mistype an account) and
  over a source table in the ledger (a new format to design, validate and review). A statement
  outside that layout is refused with the expected shape, never guessed.
- **`Dinero retirado` is spending.** Four rows in P3's statement (the probe's counts, recorded
  below) withdraw cash. The user's decision: since there is no record of how the cash is spent,
  the withdrawal is *assumed spent at withdrawal time* and posts to `Expenses:Efectivo`. It is
  not modelled as a cash asset waiting to be reconciled — that would create a balance nobody
  will ever detail. The account is a convention the importer states and the user opens by hand;
  the name is revisable in one line if the user prefers another.
- **`Dinero reservado` goes to the clearing account.** One row in P3. The tracker's earlier
  proposal (`Assets:MercadoPago:<person>:Reservado`) was declined in favour of
  `Assets:TransferenciaEnTransito`, so a hold is treated as a transfer in flight. Consequence
  recorded honestly: if the hold later releases back into the wallet, the clearing account
  carries the offset until a second statement's side cancels it, and a non-zero clearing
  balance remains the visible signal that something is unresolved.

Also decided while writing the brief: `Rendimientos` posts to `Income:<person>:Rendimientos`
(the sample's `Income:P1:Sueldo` shape, coarse and never reported), and an unclassified
counterparty suggestion carries the literal placeholder `expense:<category>` — the importer
never chooses a category, so the pinned message cannot pretend it did.

The probe's own run over the two real Mercado Pago files (aggregate output only: hashes,
pages, counts per kind, one line per check; no path, no datum) supplied the counts that made
the withdrawal and hold decisions necessary rather than hypothetical: P1 has 13 movements
(9 `Rendimientos`, 2 `Pago con`, 1 `Pago`, 1 transfer received) and P3 has 48 (25 sent and 14
received transfers, 4 `Pago con`, 4 `Dinero retirado`, 1 `Dinero reservado`). The seven other
PDFs in the statements directory are other sources and correctly fail the Mercado Pago parse.

### T-05b delivered (2026-09-25) — the pipeline imports, and the reader leak it closed

`src/expensuchis/importers/mercadopago_importer.py` is the wiring: it derives the cash account
from the per-person folder, posts each kind to its decided account, resolves purchases and bill
payments through `CounterpartyMap`, records the natural key as `key:` metadata, and registers in
`get_importers()`. `src/expensuchis/importers/pdf.py` is the only PDF reader (lazy `pypdfium2`,
pages joined with a form feed) and the probe now shares it. `tests/test_mercadopago_importer.py`
and `tests/test_probe_mercadopago.py` are new; `docs/import-workflow.md` and the sample gained the
contracts this task made concrete. 324 passed / 2 skipped, `ruff` clean, 2 opt-in pdfium tests in.

**A leak the parent found and closed before the review.** `read_pdf` propagates `FileNotFoundError`
whose `str` **is the real path**, and `extract` originally let it through: `pipeline.extract` re-wraps
importer exceptions with their message, and the CLI prints them, so an agent running the CLI against
an unreadable statement would have taken a real path into a remote model's context. `extract` now
raises `StatementReadError("cannot read the statement text: <ClassName>")` `from None`; the pipeline's
own `<statement file>` basename in that message stays open as **T-01d**. Two tests pin it, one of them
through `pipeline.extract`.

**End-to-end evidence (synthetic, in a `/tmp` ledger):** `bootstrap → identify → extract
(8 entries) → report → approve → append → bean-check exit 0`, then a second `extract` reporting
`entries: 0 skipped: 8`. The person, expense and income accounts must be opened by hand first, which
is the documented boundary: `bootstrap` never invents account names. No real statement was read.

**Native review `review-9972c11f5e343edd`** — high tier (the risk signal was `process_boundary`:
the hygiene test spawns a subprocess), 4 lenses, 1538 changed lines, correction budget 200;
**approved with no correction** on the last admitted event, 4/4 reviewers, authority burned.
Fourteen advisory findings, none blocking: `R2-001` (WARNING, readability,
`mercadopago_importer.py:231-261`), `R4-importer-account-raises-after-identify` (WARNING, resilience —
beangulp may call `account()` on a path outside the layout; the pipeline never does),
`R4-no-retry-on-reader-failure`, `R2-002`, `R2-003`, and nine `R3-*` confirmations. They are recorded
here as later work, not as a reason to re-review this candidate.

**Leak guard:** the baseline grew 76 → 78. `efectivo` is unavoidable (it names the withdrawal
account the user chose); `comercio` was already in the docs and the reindexed token set caught it
now. Each entry carries its reason in the ledger's baseline file.

**The one moment a human is required is next.** The statements must live at
`statements/MercadoPago/<person>/<file>.pdf`; the six purchases and bill payments across P1 and P3
need rows in `counterparties.tsv`. The refusal lists each one with date, amount, description, raw
name and the row to append, so the classification pass is mechanical. Re-running `extract` after
each batch of rows produces a fresh digest and therefore a fresh approval; that is the gate working,
not an obstacle.

### T-08 reconnaissance (2026-09-26) — the Brubank account, mapped without reading it

Same method as T-06b: shape-only dumps, every quantity compared in-process, scratch scripts outside
the repository and deleted. The filename (which names the holder) never reached a transcript.

- **File:** sha256-12 `138f157b9718`, 4 pages, **28 movements** (22 on page 1, 6 on page 2).
- **Extractor:** the existing `read_pdf()` (`pypdfium2`) reads clean per-line text; normalize `\r\n`
  first, as `provincia.py` does. No new primitive.
- **Columns (page 1, pt):** Fecha ~31, #Ref ~73, Descripción ~136, Débito ~393–431, Crédito ~452–499,
  Saldo ~520–563. The table header repeats on pages 1–2. The balance header block (Saldo Inicial,
  Saldo Final, Créditos, Débitos, Imp. Trans. Financieras) opens page 1 and **repeats as a recap at
  the end of page 3**. Footer with the period repeats on pages 1–3; page 4 is legal prose.
- **Rows:** date **`dd-mm-aa`** (two-digit year, unlike every other source); `#Ref` 10 digits, all
  distinct; exactly one of Débito/Crédito per row, the other `-`; Argentine amounts; **no minus sign
  anywhere**; no wrapped descriptions in this file; ascending chronological order.
- **Reconciliation, all True:** opening + credits − debits = closing; sum of credits and of debits
  match the header totals; the running-balance chain holds on every row; dates inside the period.
- **Special rows:** `Intereses pagados` 1 (a credit row); `De una cuenta tuya - BBVA` 3 (credits);
  `Imp. Trans. Financieras` appears **only as a header total**, never as a row. 0 anomalous rows.
- **Identify marker:** `resumen` + `movimientos` on the title line, disjoint from Provincia's.
- **Sibling to model on:** `provincia.py` / `provincia_importer.py` (cash account, running balance).

### T-06a delivered (2026-09-26) — the account extracto, and the two decisions it forced

`src/expensuchis/importers/provincia.py` (the pure-text parser for the quarterly
`Extracto de Cuenta`) and `provincia_importer.py` (the wiring), a masked-aggregates
`tools/probe_provincia.py`, four synthetic geometry fixtures, and the
parser/importer/probe suites. Registered in `get_importers()` beside Mercado Pago.
**427 passed / 2 skipped**, `ruff` clean.

The parser reconciles the running-balance chain row by row **and** the last page's
closing summary (final balance + total debits), so a dropped last row, a truncated
document or a 0-movement document is refused rather than accepted. The closing-summary
sign convention is pinned by 31 tests.

**Two decisions the user made, because only the statement's owner could:**

- **`compra TARJETA` is a debit-card purchase** (case (c)), not a credit-card accrual.
  It posts through `CounterpartyMap` like any `compra`; only `pago VISA` settles
  `Liabilities:Provincia:<person>:Visa`. Routing it through the map exposed that the
  raw description is **not stable** (per-row id and date/time), so the map key is now
  a **normalized identity** — the merchant with those stripped — and the contract in
  `counterparties.py` / `docs/import-workflow.md` was restated accordingly.
- **The reference rows are immediate transfers**, and their `c:` field is the
  recipient's CUIT/CUIL (personal data). They post to
  `Assets:TransferenciaEnTransito`; the CUIT is key material only and is redacted from
  the CLI-printed refusal.

**The independent verifier ran twice:** six defects on the first pass (all fixed and
pinned) and one new HIGH on the second (the unstable map key), also fixed. **The native
review found two CRITICAL findings** — the refusal echoed the raw description, which can
carry the CUIT — corrected with a CUIT redactor; the corrected candidate then
**approved** (4 lenses, authority burned). Nine advisory findings remain as later work,
none blocking; `R1-cuit-redaction-hyphenated-form` (the hyphenated `20-12345678-9` shape
is not matched by the 11-digit redactor) is the one to act on when the real file is next
imported.

**One process cost, recorded because it was avoidable.** Resolving the untracked
selection for the *corrected* candidate with `select-intended-untracked` **opened a
second lineage** instead of continuing the first, leaving the original in
`correction_required` and holding its candidate view. That view trips privacy guard #2
by design (the guard scans `.git/gentle-ai/**`), so the pre-commit hook refused the
commit. The stale lineage was quarantined (`review abandon`, `operator_disposition`) and
the residual view removed by hand, both with the user's explicit authorization. The
lesson: the untracked selection belongs to the *pre-lineage* start, never to a mid-review
correction.

### T-06b reconnaissance (2026-09-26) — the card liquidación, mapped without reading it

The statement never entered an agent's context. `pypdfium2` character boxes grouped by `y`
(2pt tolerance) gave every token an `x` position, and each dump printed **shape only** — digits
as `9`, letters as `A`, width preserved — restoring nothing but a whitelist of generic statement
words. Every number below comes from booleans, buckets and sums compared inside the process; no
amount, merchant, reference or holder reached a transcript. The scratch scripts lived in `/tmp`,
never in the repository.

The real file is a 6-page `Liquidación` (sha256-12 `6ba02c96e474`), and it shares almost nothing
with the account extracto: Argentine comma-decimal, **no running balance**, and the sign is
inverted (a purchase grows the card liability).

**Money block (page 1), in `x` space:**

| x | field |
| --- | --- |
| 18 | year (2 digits) — part of a **merged cell** |
| 32 | month name — same merged cell |
| 73 | day of the purchase (present on every row) |
| 88 | comprobante (6 digits, distinct across all 12 rows) |
| 122 | `*` marker (some rows) |
| 137 | description (merchant, `*` prefixes, optional trailing words) |
| 277 | `C.NN/NN` installment marker (9 of 12 rows) |
| 427 | amount in ARS (11 of 12 rows) |
| 547 | amount in USD (1 of 12 rows) |

Document order: a **10-line letterhead repeated identically on all six pages** (it carries
`LÍMITE: COMPRA $ … DISPONIBLE $ …`, so it is stationery, not a total) → `SALDO ANTERIOR <ARS>
<USD>` → the two `SU PAGO EN PESOS` rows → a rule of underscores → **12 consumption rows** →
the total row → **5 charge rows**. Pages 2-5 are legal prose; page 6 is a financing box plus two
payment coupons.

The coupons are the trap: they carry **6-digit numbers in other columns** (x=248/348/497) and
18-digit barcodes, so "the line contains a 6-digit token" is not a movement rule, and neither is
"the line contains an amount".

**The leading `x=18` token is the year, not a day.** It is constant in every row that carries it
(bucket 20-27), it is greater than that row's day in five of six rows, and smaller only in the
charge row whose day is 31. The per-row day is the token at `x=73`: all 12 fall in 1..31, 11 are
distinct, and they ascend inside each month group (3, 6, 20 / 11, 12, 12, 31 / 9, 10, 17, 23).
The merged cell holds year and month, is drawn **once per group** (4 of 12 rows), and is why one
statement mixes purchase months by design. Observed installment denominators: 03, 04, 06, 12;
the leading `x=18`/`x=32` cell also leads the charge block.

**The reconciliation gate closes exactly, and it is two independent sums:**

- the 11 ARS amounts at `x≈427` sum to the printed `Total Consumos` figure in ARS (`x=422`);
- the USD amount of the single USD row equals the printed USD figure (`x=542`).

Both equalities were verified in-process. The total row is recognized by the folded phrase
`TOTAL CONSUMOS` — present only in this document type — plus its two amounts.

**`identify` markers are disjoint**, probed by presence of generic words: `liquidacion`,
`consumos`, `total consumos`, `cierre`, `vencimiento`, `disponible` and `cuotas` appear **only**
in the liquidación; `extracto` and `trimestral` only in the account extracto. The existing
extracto marker is `Extracto de Cuenta`.

**A plain-text parser stays viable**: the extracted text order matches the `x` order, so no new
primitive is needed and `read_pdf`'s contract does not change. (`pypdf` is not installed; only
`pypdfium2` is. `read_pdf` takes a path, not bytes, and returns `(text, page_count)`.)

#### Four decisions the statement's owner made (2026-09-26)

1. **`SU PAGO EN PESOS` is the card payment, not a consumption.** It produces no expense; the
   expenses come from the consumos detail, exactly as the model says.
2. **The payment's cash movement belongs to the savings account of the same bank.** The
   liquidación **parses and reconciles the payment row and emits no entry for it**: the
   settlement is owned by the account extracto (T-06a), which already posts `pago VISA` against
   `Liabilities:Provincia:<person>:Visa` and `Assets:Provincia:<person>:Caja`. Posting it again
   here would reduce the liability twice. Recorded consequence: if the card carries a USD
   balance, the USD half of a payment can only be settled from a **USD account statement**, and
   none is imported yet (T-03/T-07/T-08 own that) — a follow-up, not a blocker.
3. **USD purchases are model case (b)**, not case (a): the card holds a USD balance. The row
   prints the same amount in the "importe" and "dólares" columns and **no** ARS amount, and both
   the total and `SALDO ANTERIOR` carry a USD column. So a USD purchase posts `Expenses:* <USD>`
   against `Liabilities:Provincia:<person>:VisaUSD` with **no `@` price** — there is no
   administrative conversion on this statement to observe, and deriving one from a market rate
   is the failure the model forbids.
4. **Charge rows are recognized by shape, not by label.** `IMPUESTO DE SELLOS` in either currency
   posts to `Expenses:Impuestos:Sellos`; a charge row carrying a regime number, a `%` rate and a
   **parenthesized base** before its amount is a perception and posts to
   `Expenses:Impuestos:Percepciones` (the fifth row, whose label the owner did not need to read,
   is one of these). Any other charge shape **refuses the import** rather than guessing — a new
   regime must be added deliberately, and a re-labelled row cannot silently become an expense.
5. **Installment rows realize the model's accrual convention through a *plan* key.** A row carrying
   `C.NN/TOT` is not a monthly expense: the importer emits **one** transaction at the purchase date
   for `installment_amount × TOT` with the `installments` / `first_due` / `installment_amount`
   metadata `docs/accounting-model.md` fixes, and its natural `key` is derived from the **plan**
   (person, purchase date, total installments, normalized merchant) rather than from the statement
   row. The same plan seen in a later statement therefore produces the same key, and the pipeline's
   existing *"key already in the ledger"* rule drops it instead of charging it twice — the mechanism
   T-N+2's double-counting guard will assert. `first_due` comes from the statement's own
   `VENCIMIENTO` line minus `NN-1` months, so a plan first seen at `C.03/12` still records the month
   its first installment was due.
6. **The parser's checks are the gate, and its failures are masked.** `parse_liquidacion` refuses
   the document unless: every line inside the money block is classified (an unknown line refuses
   instead of being dropped); the two sums match the printed total; comprobantes are unique; every
   merged-cell year equals the header year; and every day is valid for its month. Its error messages
   carry counts, indexes and line numbers — never a description, a reference or an amount, which is
   the lesson T-06a's refusal taught at the cost of two CRITICAL findings.

#### The candidate is reviewed as a chained sequence, not as one diff (provider verdict, 2026-09-26)

The first `review.start` for the finished T-06b candidate came back as a **provider-owned preflight
failure** — `lens_context_budget_exceeded`: the reviewers' complete evidence does not fit the native
context budget, no review authority was created, and retrying the same candidate cannot succeed
because the immutable evidence is never truncated. The provider's own prescription is to split the
work into a **chained sequence of smaller reviewable commits**, each under the budget, and to review
each reduced scope in turn.

The work therefore lands as four work units, and each one is reviewed over its own committed range
(`committedOnly`, with the previous unit as `baseRef`) before delivery:

1. `docs` — this tracker section: the reconnaissance, the four owner decisions and the plan.
2. `feat(import)` — the parser: `provincia_visa.py`, its synthetic fixtures and its parser suite.
3. `feat(import)` — the wiring: `provincia_visa_importer.py`, the registry entry, the importer suite
   and the registration expectation the new identity moves.
4. `test(probe)` — the masked-aggregates acceptance probe and its suite.

Reconnaissance, decisions and the plan stay the authority for every unit, which is why the docs unit
is the first commit rather than the last.

### T-06b delivered, part 1 (2026-09-26) — the contract and the parser

Commits `9f9f49b` (this tracker: reconnaissance, the four owner decisions and the review split)
and `7db14a1` (the parser, its synthetic fixtures and its suite). The acceptance probe ran against
the **real** document with all eleven checks `ok`: 12 charges, one payment, a balance row present,
two stamp-tax rows and three perception rows.

**The candidate had to be split, and the provider said so.** The first `review.start` over the
finished T-06b candidate came back as a provider-owned preflight failure — `lens_context_budget_exceeded`:
the reviewer evidence does not fit the native context budget, no authority was created, and retrying
the same candidate cannot succeed because the evidence is never truncated. T-06b therefore lands as
**four work units**, each reviewed over its own frozen candidate:

1. `docs` — this tracker. 2. `feat(import)` — the parser (this part). 3. `feat(import)` — the wiring:
`provincia_visa_importer.py`, the registry entry, the importer suite and the registration expectation
the new identity moves. 4. `test(probe)` — the probe and its suite.

Units 3 and 4 are written, verified twice, and the probe is green against the real document — but
they are **not committed**, because each needs its own review unit and a burned authority covers only
the candidate that earned it.

**The review earned its cost: three CRITICAL findings, all real, all in the post-total surcharge
scanner, and none of them found by either independent verification pass (or by me).**

- `R3-SURCHARGE-SILENT-SKIP` — an unrecognized post-total row **with** an amount but **without** a
  day broke the loop silently: the row and every later one were dropped while `surcharge-complete`
  still reported `ok`. The failing condition had required a day token.
- `R3-1` — a perception-shaped row (rate + parenthesized base) **without** an amount never set the
  "claimed" flag, so it closed the block silently, although the docstring already promised a refusal.
- `R3-001` — an opening parenthesis with no closing one made the base scan evaluate `None + 1` and
  raise an uncaught `TypeError` instead of a controlled refusal. The writer had reported that edge as
  "kept deliberately loud"; it crashed.

The contract that emerged, and that the module now states: **a post-total row that matches any charge
marker (the stamp-tax label, a rate, a parenthesis pair, an amount tail) is claimed and refused when
it cannot be parsed; only a row that matches no marker at all ends the block.** After the third
finding the fix came with a **property test** driving a 15-shape corpus of malformed post-total rows
and asserting that every outcome is either a successful parse or one of the two controlled exceptions —
closing the class instead of one instance per review round.

**Two advisories stand, non-blocking and deliberately not chased:** `R3-001` (WARNING, lines 865-900)
and `R3-002` (SUGGESTION, lines 668-679). The provider records them with an id, a location and a
severity; their text is not exposed to an agent session, so they are tracked by those references.

**Residuals recorded, each with the evidence that would settle it:** the trailing-amount scanner still
requires a grouped integer part, so an *amount* printed ungrouped with four or more digits would refuse
loudly (the masked shapes show real amounts at or below three digits); the period window still admits a
one-year misread of a long plan; the plan key still depends on the merchant text, and only a second
real statement can show whether the bank's rendering drifts between months; and an undated surcharge
row takes the closing month's first day, because `Liquidacion` does not expose the closing day.

#### What the review process itself cost, and what is now standing practice

1. **The leak guard runs at commit time, i.e. *after* a candidate is frozen for review.** A leak-guard
   finding therefore forces a content change that voids the approval and costs a whole review round —
   which is exactly what happened here. **Standing practice now: run `.githooks/pre-commit` with
   `EXPENSUCHIS_LEDGER_DIR` set *before* freezing a candidate**, so the gate is green when the review
   runs.
2. **How a work-unit candidate is isolated for review.** Stage only that unit's files, `git stash` the
   tracked changes belonging to other units, and resolve the untracked inventory with `untrackedScope:
   "exclude"`. New files must be staged or they never enter the projection; a `baseRef` range was
   rejected (`candidate-target-projection-drift`) and the range's `select-intended-untracked` binding
   was refused, so the workspace projection is the route that works.
3. **The targeted-validation transition is not reachable from an agent session.** After a correction the
   provider offers `review.capture-validation`; neither capture tool accepts that binding (`different
   session route`) and `advance` does not know the transition. The native route is the CLI, and composing
   its tokens from a model is forbidden. The substitute used — a fresh review of the corrected candidate
   — covers strictly more than the targeted validation did.
4. **`review abandon` needs `capturedLensResults`, which no status exposes.** Superseded lineages in
   `correction_required` therefore cannot be closed by the agent; their candidate views were removed by
   hand with the user's authorization (each view is a read-only tree under `.git/gentle-ai/candidate-views/`,
   which the privacy guard scans by design — so an open review leaves two privacy tests red and blocks
   the pre-commit until the views are gone). A closed-and-acknowledged lineage removes its own view.
5. **Consent is human and time-boxed.** A `review.start` opens a consent envelope; unanswered after ten
   minutes it returns `consent-binding-stale` with no lineage and no mutation, and the START must be run
   again. One host approval covers later envelopes for the same session and repository.
6. **The acceptance probe is the only oracle for real shapes.** The parser passed 54/54 unit tests while
   the real statement was refused, because my synthetic shapes were wrong: the real perception *base* is
   printed **without** a thousands separator (`2187,43`), and a stricter "money inside the parentheses"
   check rejected it. Fixtures that mimic a real shape must be built from the masked real shape.

### T-06b delivered, part 2 (2026-09-26) — the wiring and the probe

Commits `a3cfad2` (the wiring: `provincia_visa_importer.py`, the registry entry, its suite and the
registration expectation the new identity moves) and `0086184` (the acceptance probe and its suite).
T-06b is complete: every unit reviewed over its own frozen candidate, every approval acknowledged,
authority burned, tree clean at **553 passed / 2 skipped**.

**The wiring review (tier high, four lenses, one refuter pass) found two more CRITICAL findings, both
real, and neither visible to the two independent verification passes:**

- the CUIT redaction was broken for a **mixed-separator CUIT with a space before its final digit**
  (`20.12345678 9`): the identity was split on whitespace *before* CUIT tokens were classified, so
  `20.12345678` survived and the trailing `9` was dropped as a stray digit, printing **ten of the
  eleven digits** in the identity and in the refusal's `append` line. The old guard compared a
  *redacted* form and therefore could not see it. The fix sweeps CUIT runs from the **raw description**
  first and replaced the guard with a property of the string that is actually printed: no run of seven
  or more digits once separators are ignored, else the document is refused. **The lesson generalises:
  a redaction guard must be a property of the printed string, never a comparison against a
  transformation of it.**
- the surcharge key **omitted the person**, so two people's identical charges (same closing month,
  kind, amount, currency) produced one key and the pipeline's ledger-wide dedup **silently dropped the
  second** — understating that person's expenses and liability. The key now carries the person,
  mirroring the plan key. **The lesson generalises: a natural key must carry every dimension that
  distinguishes two legitimately different facts.**

**Ten advisory findings stand on the wiring and two on the probe**, all non-blocking and none of them
reopening the review: `R1-1`, `R1-2`, `R2-001`–`R2-003`, `R3-DEFAULT-READER-UNPROVED`,
`R3-INTERNAL-DESTINATION-UNPROVED`, `R3-LAZY-MAP-UNPROVED`, `R4-1`, `R4-2` (wiring, all in
`provincia_visa_importer.py`), and `R3-multipath-coverage`, `R3-parse-error-message` (probe, in
`tools/probe_provincia_visa.py`). Three of them are explicitly *unproved* paths the reviewers could not
settle — a default reader, an internal destination and the lazy map — each worth a test when the wiring
is next touched.

**One more process lesson, from the transport failure.** The four-lens group capture failed at the
relay (`reviewer completion failed for review-reliability: terminated`, zero slots admitted) and the
provider's instruction was exact: refresh STATUS and submit **only the reoffered one-slot binding**,
never replay the group. Capturing slot by slot afterwards admitted all four.

## Next step

0. **Resume here (2026-09-27 close):** T-03 and T-01d are closed and reviewed; T-08a (the Brubank
   parser, with its region fix `24ce845`) and T-08b (the wiring) are delivered; T-08a's region fix
   and T-08b are both still pending review, in the slice from `3130fb3`. Next is **T-08c** (the
   acceptance probe against the real file), which must confirm the header, period and
   generation-stamp shapes (carried from T-08a) and that `Intereses pagados` never appears as a
   debit (T-08b's fixed-destination assumption). Open follow-ups are recorded in the T-03, T-01d
   and T-08 entries.
1. **T-06b is closed.** The parser, the wiring and the probe are committed and reviewed; the tracker
   unit and both delivery sections are the record. What remains of the feature is the next task in the
   checklist — **T-03** (the MEP and CCL series) — plus the deferred T-01c/T-01d units and the
   follow-ups below.
2. **Follow-ups the review left open, in the order they matter:** the three *unproved* wiring paths
   above; the T-06a advisory (the extracto's redactor does not match the hyphenated CUIT form); the
   probe's multipath coverage and its parse-error message; and the residuals the parser records in its
   own docstring (the trailing-amount scanner's grouped integer part, the period window's one-year
   blind spot for a long plan, the plan key's dependence on merchant text, and the day-1 stand-in for
   an undated surcharge row).
3. T-03 (MEP and CCL series) stays small and unblocked; the deflated view needs it whenever the
   first real month is ingested.
4. **T-01c** (`ruff format` drift across nine files) and **T-01d** (redact the leak guard's failure
   output) remain open and deliberately untouched: both are cosmetic-or-control changes and belong
   in their own reviewed units.
5. Still open: whether Mercado Pago issues a `RESUMEN DE CUENTA EN DÓLARES`, and whether it has a
   card statement separate from the account statement. Still open from T-06a: the hyphenated CUIT
   form (`20-12345678-9`) is not matched by the redactor.
