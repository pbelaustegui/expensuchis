# Accounting model

The rules this ledger follows. Read this before writing an importer, because a wrong
account tree or a wrong sign produces a `bean-check`-clean ledger with plausible numbers —
confirmed, not feared: the first draft of this very model balanced at +90,000 on a card
that should have been zero.

Everything below is a requirement that `sample/family-model.beancount` and
`tests/test_family_model.py` enforce against beancount 3.2.3: the sample is `bean-check`-clean
and the tests assert the properties this document describes. The sample is the authority for
every worked example — if this document and the sample ever disagree, the sample wins and this
document is corrected. Concretely: a worked example's **account shapes, currencies and
amounts** must match the sample; merchant names and narrations may differ, because the sample
uses invented placeholders.

## The two problems this model exists to solve

1. **Double counting.** The same money appears as a purchase in the card statement, as a
   debit in the bank account that pays the card, and sometimes as a transfer in a fintech.
   That is one purchase plus one settlement. A single-entry ledger counts it two or three
   times.
2. **Loss of meaning over time.** Nominal ARS stops being comparable within months. The
   measure chosen for this household is **USD at the date of the transaction**.

## Account tree

```
Assets:<Entity>:<Person>:<Product>
Liabilities:<Entity>:<Person>:<Card>
Expenses:<Area>[:<Category>]
Income:<Person>:<Source>
Equity:Opening-Balances
```

- `<Entity>` — `BBVA`, `Provincia`, `Brubank`, `MercadoPago`. Prose says "Banco Provincia";
  account names use `Provincia`. Same entity, two spellings, by convention.
- `<Person>` — the account holder. The document uses `P1` (self), `P2` (partner), `P3`
  (son, spends only). Real names or initials go in the live ledger; the shape does not change.
- `<Product>` — `Caja` (savings account), `CajaUSD`, or whatever the statement calls it.
- A `USD` suffix marks a USD-denominated account. **The suffix is a human convention only:
  nothing in beancount enforces agreement between the account name and the currency declared
  in its `open` directive.** `Assets:BBVA:P1:CajaUSD` opened as `ARS` is legal to beancount
  and wrong in every other sense; `bean-check` will not complain. What catches the mismatch is
  the sample's structural test, which asserts that every account whose name ends in `USD` is
  declared with currency `USD`.
- A card that carries balances in both currencies is **two liability accounts**, one per
  currency — `Visa` for ARS and `VisaUSD` for USD — and each is settled against its own
  account. One account mixing commodities makes settlement and reconciliation unreadable, so
  the model uses **two accounts and never mixes commodities in one**. This is a convention, not
  something beancount enforces: `bean-check` accepts `open Liabilities:X:VisaMix ARS,USD`
  without complaint. What enforces it is the sample's tests, which assert that no `open`
  directive declares more than one currency.

```
2026-01-01 open Assets:BBVA:P1:Caja          ARS
2026-01-01 open Assets:BBVA:P1:CajaUSD       USD
2026-01-01 open Liabilities:BBVA:P1:Visa     ARS
2026-01-01 open Liabilities:BBVA:P1:VisaUSD  USD
```

An importer must never emit an account that the tree does not define. The path in this
document and in the sample is `Expenses:ServiciosDigitales:Suscripciones`; an
`Expenses:Servicios:Suscripciones` that is not in the tree would be undeclared, and
`bean-check` would reject it.

Which accounts exist per person, per the user (2026-09-24):

| Person | Entities | Notes |
| --- | --- | --- |
| P1 | BBVA, Brubank, Mercado Pago | USD savings account at BBVA and Brubank |
| P2 | Banco Provincia, Mercado Pago | USD savings account at Provincia |
| P3 | Mercado Pago | **spends only**, no income; funded by transfers |

## Sign convention

Verified live, and the most likely way to corrupt this ledger silently:

| Event | Posting |
| --- | --- |
| Card purchase | `Expenses:*` **positive**, card liability **negative** (the debt grows) |
| Statement settlement | card liability **positive** (back toward zero), bank asset **negative** |
| Income | bank asset positive, `Income:*` **negative** |
| Internal transfer | both postings inside `Assets:*`/`Liabilities:*`; nothing in `Expenses:*` |

The trap has two failure modes, and only one of them is dangerous:

- **Invert a single leg.** The transaction no longer balances, `bean-check` refuses it loudly,
  and nothing gets recorded. Noisy, but harmless.
- **Invert a balanced pair — flip both legs.** The transaction still balances, `bean-check`
  accepts it, and every total stays plausible. This is the silent case: it reveals itself only
  when a balance that should be zero is not.

`tests/test_family_model.py` asserts the card liability returns to exactly zero after
settlement, which is the check that catches the balanced inversion.

## Accrual versus cash

The distinction that makes double counting structurally impossible:

- **Accrual** — the economic event. A purchase is an expense on the day it was made. It
  creates a liability at the same time. Sourced from the **card statement**.
- **Cash** — the settlement. Paying the statement reduces the liability and takes cash out of
  the bank account. **It has no `Expenses:` leg, so it cannot add spending.** Sourced from the
  **bank statement**.

A purchase and its settlement are therefore two different transactions that both exist in the
real world, and exactly one of them is an expense.

## Opening balances

The ledger starts mid-life, on a cutover date, with money already sitting in the accounts.
Every account that holds a balance on that date gets an **opening balance**: one posting to
the account and a matching posting to `Equity:Opening-Balances`. A single opening transaction
cannot balance across two commodities, so an account holding both ARS and USD gets **one
opening balance per currency**:

```
2026-01-02 * "Opening balance" "P1 USD savings"
  Assets:BBVA:P1:CajaUSD            1,000.00 USD
  Equity:Opening-Balances          -1,000.00 USD
```

An opening balance is **not a conversion**. It introduces the account's initial quantity in
the currency the account is already denominated in, so no rate is involved and none may be
recorded. This is also how the USD savings accounts come to exist without any market rate ever
entering the ledger: the dollars were already there on the cutover date, so they are an
opening balance, not an FX operation. Any real importer hits this on day one, before it
records a single purchase.

The opening balance is sourced from the statement's **own opening-balance line** on the cutover
date — it is read, not recomputed. An account whose opening balance is zero gets **no** opening
posting; declaring the account is still correct, which is why the sample opens accounts (and
expense categories) that no transaction ever posts to.

## Installments

A 12-installment purchase of 720,000 is recorded **once**, at the purchase date: the full
amount as the expense, the full amount as the liability, and the plan captured as metadata so
nothing is lost:

```
2026-03-12 * "ElectroEjemplo" "Lavarropas en 12 cuotas"
  installments: 12
  first_due: "2026-04"
  installment_amount: 60000.00 ARS
  Expenses:Compras:Electrodomesticos     720000.00 ARS
  Liabilities:Provincia:P2:Visa         -720000.00 ARS
```

The amount carries its currency: `60000.00 ARS`, not a bare `60000.00`. beancount metadata
accepts an `Amount`, and an unqualified number is unattributable: the key may appear on any
transaction, so the amount must say which currency it is in.

Two consequences, both deliberate:

- **The monthly expense total is lumpy.** A month with a big financed purchase shows a spike,
  because that is when the money was committed. This is the *accrual* view and it answers
  "what did I spend", which is what the user asked for.
- **The installment view does not need a second expense.** It is a query over the card
  liability's movements, which the monthly settlements already produce. Recording each
  installment as its own expense would be the alternative, and it would answer a different
  question ("what did I pay this month") while erasing the moment the money was committed.
  The choice is the accrual one, and the installment plan in metadata keeps the other view
  derivable.

The `C.03/12` marker on an Argentine statement is the plan; the importer reads it from there.
The three keys are a **contract**, not decoration:

- `installments` — integer; the `/12` in a `C.03/12` marker, the total number of installments.
- `first_due` — string `"YYYY-MM"`; the month of the first installment, from the statement's
  own due-date line.
- `installment_amount` — an `Amount`; the per-installment value the statement shows on that
  line, in the plan's currency.

## USD

A USD purchase reaches the ledger in one of three shapes. Which shape applies is a property of
the real product, not a modelling choice, and the importer emits different postings for each.
Conflating them is the second silent-corruption risk. The discriminator the importer implements
is the **statement's own currency of charge**, and which one a given entity produces is
confirmed per source in T-05:

- **(a)** the statement charges **ARS** for a USD purchase (an ARS card, or an ARS debit).
- **(b)** the statement carries the charge **in USD on a card that holds a USD balance**,
  settled later from a USD account.
- **(c)** the debit is **directly against the USD savings account**, with no liability.

**(a) A USD purchase on an ARS card.** The statement gives **both** amounts: the original USD
and the ARS actually charged. That ARS comes from an administrative conversion with tax and
perception components, *not* from MEP or CCL. Express it as a price on the USD posting, so the
effective rate is **derived from the two amounts the statement provides** and never looked up:

```
2026-03-10 * "StreamingEjemplo" "Suscripcion en USD"
  Expenses:ServiciosDigitales:Suscripciones     15.00 USD @ 1580.00 ARS
  Liabilities:BBVA:P1:Visa                 -23700.00 ARS
```

The `@` price is the administrative rate. **Never replace it with a market rate**: converting
the 15 USD at CCL produces a believable, wrong number.

**(b) A USD purchase charged to a card that carries a USD balance.** The liability is in USD,
so no rate appears. It is settled later by a **separate** transaction that moves USD from the
savings account to the liability, which also needs no rate:

```
2026-03-10 * "Steam" "Compra en USD"
  Expenses:ServiciosDigitales:Suscripciones     15.00 USD
  Liabilities:BBVA:P1:VisaUSD                  -15.00 USD

2026-03-22 * "BBVA" "USD card settlement"
  Liabilities:BBVA:P1:VisaUSD                   15.00 USD
  Assets:BBVA:P1:CajaUSD                       -15.00 USD
```

**(c) A USD purchase debited directly from the USD savings account** (debit card or instant
debit). One transaction, no liability, no rate:

```
2026-03-10 * "PixelForge" "Compra en USD"
  Expenses:ServiciosDigitales:Suscripciones     15.00 USD
  Assets:BBVA:P1:CajaUSD                       -15.00 USD
```

A wrong guess between (b) and (c) is the same class of error as getting a sign backwards: the
totals stay plausible and only the balances that should be zero (or the liability that should
not exist) reveal it.

**A series value is looked up; an executed rate is observed.** MEP and CCL (T-03) are FX
*series*: they exist to answer "what is this worth in hard currency", a reporting concern, and
a series value is never looked up to record a transaction. An FX operation that actually
happened is different: its rate *was executed*, so it is an observed fact of that operation
and is recorded from the operation itself — the `@ 1240.00` of the cross-currency transfer in
Internal transfers, below. The same
distinction is what makes the card's administrative `@` price valid: the bank's conversion is
an observed fact of that statement, not a series lookup. This separation is what keeps a
future switch between MEP, CCL and IPC from becoming a data migration.

## Internal transfers

Any transaction whose postings all fall inside `Assets:*` and `Liabilities:*` is a transfer,
never an expense. Two rules follow:

- **Every account that participates must be in the ledger. P3's Mercado Pago account
  included, even though he has no income.** If his account were missing, the money sent to him
  would have nowhere to post except `Expenses:*`, and every funding transfer would become a
  phantom expense inflating the household total.
- **A transfer between currencies is not an expense either**, but it does carry a rate: the
  rate actually executed in that operation. It is observed from the operation itself, like the
  card's administrative conversion and unlike a series lookup:

```
2026-03-20 * "Compra de dolares MEP"
  Assets:BBVA:P1:CajaUSD          500.00 USD @ 1240.00 ARS
  Assets:BBVA:P1:Caja          -620000.00 ARS
```

The same real transfer appears as a line in **two different statements**: a debit in one
account's statement and a credit in the other's. Producing **one balanced transaction per real
transfer**, not one per statement line, is **T-04's responsibility**. The model assumes it: two
per-line transactions would record the transfer twice.

## Income

Out of scope for **reporting** — "the expenses are what matter" — but not omittable in a
double-entry ledger, because every peso that arrives must post from somewhere. Income is
therefore modelled **coarsely**: one account per person and origin, never reported, present so
that balances reconcile against the bank statements.

```
2026-01-05 * "Sueldo"
  Assets:BBVA:P1:Caja            1800000.00 ARS
  Income:P1:Sueldo
```

## Expense categories (approved 2026-09-24)

Approved by the user, with two amendments that came out of asking where a trip and a board game
belong — the original draft had nowhere for a hotel to go. Categories are how the user thinks
about their spending, not how a programmer does:

```
Expenses:Vivienda:{Alquiler,Expensas,Servicios,Internet}
Expenses:Supermercado
Expenses:ComidaFuera:{Restaurantes,Delivery}
Expenses:Transporte:{Nafta,Peaje,Estacionamiento,TaxiApps,SUBE}
Expenses:Salud:{ObraSocial,Farmacia,Consultas}
Expenses:Educacion:{Colegio,Cursos,Utiles}
Expenses:Compras:{Ropa,Hogar,Electrodomesticos,Electronica,Juegos}
Expenses:ServiciosDigitales:{Suscripciones,Telefonia}
Expenses:Impuestos:{Percepciones,Sellos,ABL}
Expenses:Seguros
Expenses:Entretenimiento:{Salidas,Deportes}
Expenses:Viajes:{Transporte,Alojamiento,Comida,Actividades,Otros}
Expenses:CargosBancarios
Expenses:Mascotas
Expenses:Otros
```

Two notes on the Argentine specifics:

- `Expenses:Impuestos:Percepciones` exists on purpose: perceptions (IIBB, RG 5617) appear on
  card statements as separate lines and are real cost, not noise.
- `Expenses:Familia` is deliberately **absent**. Transfers to family members are internal.
  That account would only exist for money that leaves the household for good.

### The axis each category answers

Two categories can look like rivals when they are answering different questions, and the axis
settles it:

- `Expenses:Compras:*` is for a **thing you buy and keep** — which is why a board game lives in
  `Compras:Juegos` and not under entertainment.
- `Expenses:Entretenimiento:*` is for an **outing or an activity**.

When a purchase seems to fit both, the axis decides, not taste.

**Never create a category for completeness.** A category earns its place by answering a question
the user is actually going to ask. If "how much did I spend on board games" will never be
asked, `Compras:Hogar` is a fine home, and `Otros` is an escape valve rather than a plan.

### Prefer the specific: merging is trivial, splitting is impossible

If a distinction later turns out to be useless, merging two categories is a find-and-replace
across the ledger. If it turns out to be necessary after everything was filed under one generic
category, splitting it means re-reading every statement by hand. **Being wrong by being
specific costs nothing; being wrong by being generic costs the user's time.**

### Tags: the second dimension

A trip is not a category, because a trip has two orthogonal dimensions: the **object** of each
expense (transport, lodging, food, activities) and the **occasion** (the trip itself). Choosing
one loses the other — filing everything under the trip scatters the object breakdown, and filing
by object means the trip can never be totalled.

One dimension is structure, the other is a tag. beancount supports tags natively:

```
2026-07-14 * "Aerolineas" "Vuelo a Brasil" #brasil-2026
  Expenses:Viajes:Transporte              250000.00 ARS
  Liabilities:BBVA:P1:Visa               -250000.00 ARS
```

This answers both questions: spending by object across the tree, and the total cost of
`#brasil-2026`. The same mechanism serves any occasion worth grouping later — `#mudanza`,
`#auto-nuevo`.

**Tags are applied by the user during import review, not by an importer.** No statement knows the
user is travelling. This is deliberately the one convention here that the sample does not
exercise: the sample is the authority for what an **importer emits**, and an importer never emits
a trip tag.

### Whose expense is it

Expenses carry **no person tag by design**. Per-person attribution is derived from the
counterparty account of the expense posting — the account the money left (`Assets:...:<Person>`
or the card liability it charged), not from a tag on the expense itself. The consequence is
that a per-person view is a **query over transactions' counterparty accounts**, which is a
reporting concern (T-N+1), not a posting convention. If that query proves awkward in practice,
a person tag can be added later without re-recording history: the counterparty is already in
the ledger.

## What the sample must demonstrate

`sample/family-model.beancount` is synthetic and must cover, with `bean-check` clean. It uses
placeholder product and merchant names, which is fine in a synthetic sample:

1. A card purchase, and the statement settlement of it.
2. A 12-installment purchase with its plan in metadata, left outstanding at the end of the
   sample.
3. A cash expense.
4. A USD purchase charged to an ARS card with an administrative `@` price (case a).
5. A USD purchase charged to a USD card balance, settled separately from USD savings with no
   rate (case b).
6. A USD purchase debited directly from a USD savings account with no rate (case c).
7. An internal transfer between two family members' accounts.
8. A cross-currency internal transfer, with the rate the operation executed.

And the assertions that make the model meaningful, in `tests/test_family_model.py`:

- **The whole-sample expense total is pinned exactly, per currency.** Every `Expenses:`
  posting in the sample is summed and compared against a fixed expected value, so a purchase
  counted twice under *any* category fails. This is the whole point of the model and the one
  property that must never regress. A per-category check is not enough on its own: one was
  tried, and a duplicate recorded under a *different* category passed it while the household
  total rose from 773,500 to 818,500 ARS.
- Every liability except the installment plan is **exactly zero**, enumerated over all
  `Liabilities:*` accounts rather than a hardcoded one, so an unsettled card cannot hide.
- The settlement transactions contribute **no** `Expenses:` posting.
- The administrative `@` price on the USD purchase equals the two amounts on the statement, and
  the set of priced postings is exactly the expected set — a count alone would be satisfied by
  the wrong price. No `Price` or `Commodity` directive exists anywhere in `sample/`, so a market
  rate cannot enter as a directive while a token scan stays green.
- Every account whose name ends in `USD` is declared with currency `USD`, and **no** `open`
  directive declares more than one currency.
- The installment plan's metadata survives parsing as an `Amount` with its currency intact.
- The opening balances, the income postings and the cash expense are present and posted against
  the accounts this document names.

## Open decisions

- Which card/product names to use per entity (`Visa`, `Amex`, `Mastercard`) — needs the real
  cards. A card that carries both currencies is settled as two accounts (`Visa`, `VisaUSD`),
  per the account-tree rule above.
- Person naming (`P1`/`P2`/`P3` versus real names) in the live ledger.
- Whether a per-person expense view needs a tag. The derivation rule above is expected to
  suffice, and a tag can be added without re-recording history if it does not.
