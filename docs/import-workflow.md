# Import workflow

`expensuchis` imports a downloaded statement into the ledger through a review gate:
nothing an importer parses reaches the ledger until you have approved the exact bytes
that will be written. This document is the contract for that workflow: the commands,
the invariants, the refusal codes, and what the tool deliberately does not do.

## Quick path

```bash
expensuchis bootstrap                       # once, per ledger
expensuchis identify statement.pdf          # which importer claims it?
expensuchis extract --source MercadoPago statement.pdf
expensuchis report  MercadoPago-20260925T143012Z-1a2b3c4d
expensuchis approve MercadoPago-20260925T143012Z-1a2b3c4d
expensuchis append  MercadoPago-20260925T143012Z-1a2b3c4d
bean-check "$EXPENSUCHIS_LEDGER_DIR/main.beancount"
```

Exit codes are stable and greppable:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | a refusal or validation failure, printed as `refused: <reason-code>: <sentence>` |
| `2` | a usage error (argparse) |
| `141` | the consumer closed the output pipe (SIGPIPE); the command itself succeeded |

Output is plain text, never colour. `extract` prints the batch id and the report path so
the next command can be copied from the terminal.

## Ledger layout

The ledger lives **outside this repository**, at `$EXPENSUCHIS_LEDGER_DIR`:

```
main.beancount                     the file bean-check runs on
accounts.beancount                 open directives and options
transactions/<YYYY-MM>.beancount   appended transactions, one file per month
statements/<Source>/...            the raw files you download
staging/<batch-id>/                proposed batches awaiting review
counterparties.tsv                 the learned counterparty map (T-04c)
```

`bootstrap` writes `main.beancount`, `accounts.beancount` and the layout directories.

## The review gate is a digest, not a prompt

| Step | What happens |
| --- | --- |
| `extract` | Writes `staging/<batch-id>/proposed.beancount` — the exact bytes `append` will write, produced by beancount's own printer — plus `batch.json` and `report.txt`. |
| you | Read `report.txt` (or edit `proposed.beancount` by hand). |
| `approve` | Writes `approval.json` holding the sha256 of `proposed.beancount`. |
| `append` | Recomputes that digest and refuses on any mismatch. The reviewed bytes are the appended bytes. |

**Honest boundary.** `approve` proves the digest, not the reading. The CLI cannot verify
that a human read the report. What it enforces is that staging and appending are two
separate commands and that the approval is bound to exact bytes, so an edit made after
review invalidates the approval instead of riding in on it.

## The `key` contract

Every entry an importer returns must carry a non-empty `key` metadata field, and keys
must be unique within a batch. `key` is the importer-supplied natural identity of the
movement — T-05 builds it from date + operation id + amount. Deduplication, not the
human, is what the `key` is for.

```beancount
2026-09-03 * "Comercio Ejemplo" "Compra"
  key: "2026-09-03:op-000123:-15000.00"
  Expenses:Supermercado                 15000.00 ARS
  Liabilities:Provincia:P2:Visa        -15000.00 ARS
```

## How deduplication is derived

The index is **derived, never stored**. It is built by loading `main.beancount` — the file
`bean-check` runs — with beancount's own loader and collecting every entry's `key` metadata.
Loading the ledger root, rather than globbing `transactions/*.beancount`, is what makes a key
hand-written into `main.beancount` (or any other included file) visible: a missed key is a
silent duplicate, the failure this whole workflow exists to prevent. There is no regex over
the ledger text: a key that only appears inside a comment or a string is not a key.

- At `extract` time, entries whose key is already in the ledger are dropped and counted
  in `batch.json` and `report.txt`.
- At `append` time, an already-present key is a hard refusal. That can only happen if the
  ledger moved between staging and appending.

## What the report shows

`report.txt` lists the batch header, every proposed entry (date, payee, narration, postings,
and every metadata field except beancount's own `filename`/`lineno`), and the entries skipped
as already present.

Its **debit-side totals per currency** are the sum of the **positive** postings only. This is
a review aid, **not the batch net**: every balanced batch nets to exactly zero, so a signed
total would carry no information. The figure answers "how much moved into accounts in this
batch", not "what did this batch cost".

## Append order and rollback

`append` refuses at the first failure, in this order:

1. the batch exists;
2. it has not already been appended (`append.json` absent);
3. `approval.json` exists;
4. the proposed digest matches the approval;
5. the batch is not empty — a batch whose every entry was already in the ledger refuses
   with `batch-empty` rather than writing an `append.json` that records a write that never
   happened;
6. `main.beancount` exists;
7. the ledger is **already clean** — a dirty ledger must not be "fixed" by an append;
8. every staged `key` is absent from the ledger.

It then writes each entry to `transactions/<YYYY-MM>.beancount` by month, in date order,
blank-line separated, creating a month file when absent. Before writing, it snapshots the
previous bytes of every touched file (and whether it existed). If the post-write
`bean-check` gate fails, every touched file is restored **byte-for-byte**, files that did
not exist are deleted, and the batch and its approval are left untouched. On success it
writes `append.json` (status, digest, timestamp, touched files) so a second `append`
refuses clearly.

**Empirical note (empty-glob).** `include "transactions/*.beancount"` fails `bean-check`
when no file matches:

```
<load>:0: File glob "transactions/*.beancount" does not match any files
```

A freshly bootstrapped ledger would therefore be "dirty" before the first append, and the
append gate would deadlock (the first append is the only thing that could create the first
transaction file). `bootstrap` seeds `transactions/placeholder.beancount` with a comment so
the ledger is clean from the first command.

**Do not delete `transactions/placeholder.beancount`.** It carries no entries; it exists
only so the include glob matches. Deleting it breaks `bean-check` for any month that has no
entries yet, and `append` never deletes it — doing so would widen the rollback surface for a
file the batch never touched. Leave it beside the real month files.

## The bean-check gate

The gate is **bean-check equivalence**: the in-process check calls
`beancount.loader.load_file(main, extra_validations=validation.HARDCORE_VALIDATIONS)` and
treats a non-empty error list as failure.

**What that option actually does, verified rather than assumed.** `HARDCORE_VALIDATIONS` is
exactly `[validate_data_types]`, and `validate_data_types` runs `data.sanity_check_types`
over the entries **in memory**. It therefore cannot fire for a ledger parsed from text; the
option makes no difference on any text fixture. It is passed anyway so the in-process gate
and the `bean-check` CLI stay equal if beancount ever adds a hardcore validation that *can*
fire on text. Two tests pin this: one asserts the list is exactly
`[validate_data_types]` (a tripwire — if it changes, re-derive the parity claim instead of
assuming it), and one asserts `ledger_errors` forwards the option. A further test runs the
real `uv run bean-check` binary on a broken ledger and asserts both checkers report it.

## Refusal reason codes

| Code | Condition |
| --- | --- |
| `batch-not-found` | the batch directory (or its `proposed.beancount`) does not exist |
| `batch-already-appended` | `append.json` already exists |
| `batch-empty` | every entry was already in the ledger; there is nothing to append |
| `batch-exists` | `extract` would replace an existing staging batch (a re-extract in the same second, or an approved/hand-edited batch) |
| `approval-missing` | `approval.json` does not exist |
| `digest-mismatch` | `proposed.beancount` no longer hashes to the approved digest |
| `main-missing` | `main.beancount` does not exist |
| `ledger-dirty` | the ledger already fails the bean-check gate |
| `key-already-present` | a staged key is already in the ledger |
| `key-missing` | an entry has no non-empty `key` |
| `key-duplicate` | a key appears more than once in the batch |
| `importer-raised` | the importer raised instead of returning entries |
| `no-importer` | no registered importer claims the file |
| `ambiguous-importer` | more than one importer claims the file |
| `post-write-dirty` | the write broke the gate; every touched file was rolled back |
| `staging-corrupt` | the staged bytes do not match the parsed entries |
| `invalid-source` | the `--source` name is a path, not a single name |
| `statement-missing` | the statement file does not exist |
| `bootstrap-exists` | `bootstrap` would overwrite an existing file |
| `bootstrap-failed` | `bootstrap` hit an `OSError`; every file it created was removed |

## Bootstrap: who names the accounts

`bootstrap` writes only the structural skeleton. `main.beancount` holds the options and
the two `include` lines. `accounts.beancount` opens the equity account and
`Assets:TransferenciaEnTransito`, and nothing else.

**It does not generate person, entity or card accounts.** Those name the household's real
accounts, which are personal data the tool cannot derive; they are named by hand in the
live ledger. The generated file states that rule in a comment. `bootstrap` also refuses to
overwrite an existing file, so re-running it never clobbers a live ledger.

## The importer contract

Importers live in `src/expensuchis/importers.py` and subclass beangulp's `Importer`
(`identify`, `account`, `extract`). Two rules the pipeline depends on:

- **A failing importer raises; it never returns partial entries.** A reconciliation check
  that does not hold means the document was not understood, and emitting the rows that
  happened to parse writes a plausible, wrong ledger. T-05's five Mercado Pago checks gate
  on this rule.
- **Heavyweight dependencies are imported lazily, inside `extract`.** A PDF engine such as
  `pypdfium2` must not be imported at module import time, so `identify` and the CLI stay
  fast and a machine without the engine can still run everything else.

`get_importers()` returns one fresh importer instance per supported source. It is empty
until T-05 registers the first one.

## Gap: the counterparty map

The learned counterparty map — per-source name variants, `internal:<account>` /
`expense:<category>` destinations, asked once during review and remembered, never
defaulted — is **not specified here**. `counterparties.tsv` exists and `CounterpartyMap`
implements its storage, but how review resolves an unknown name into a destination is
**T-04c**, which owns this section. Do not infer the map's design from this document.

## Checklist

- [ ] `bootstrap` ran once and the ledger passes `bean-check`.
- [ ] `identify` names exactly one importer for the statement.
- [ ] `extract` printed a batch id and a report path.
- [ ] the report was read (and edited, if needed) before `approve`.
- [ ] `append` succeeded and `bean-check` is still clean.
- [ ] no statement, ledger file or real financial datum is inside the repository.

## Next step

Writing the first importer is **T-05** (Mercado Pago). The counterparty-map design is
**T-04c**.
