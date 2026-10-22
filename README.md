# TPX ↔ Dealernet Edge Finder

Local, **read-only** Python tool. For any sealed product (sports "sealed wax" or
sealed TCG box/case) it pulls live market data from Tradepost Partner Exchange
(TPX) and compares it against the Dealernet (dealernetx.com) bid/ask you
supply, then tells you where the edge is:

* **Taker arb** both directions after all fees and shipping, with the TPX book
  walked at sizes 1 / 3 / 5 / 10.
* **Maker levels**: the best bid or ask you could rest on either venue and
  still clear the target margin against the other venue, with a best-in-book
  flag.
* **Fair-value gaps**: Dealernet bid/ask and TPX mid vs TPX's fair-value model
  ("no FV" when unpriced, never 0).
* **Spread, depth, imbalance, staleness, 30-day velocity, momentum** so you can
  judge how real the edge is, and a risk-adjusted ranking.

No order placement. No mutation is implemented anywhere (see
[Read-only guarantee](#read-only-guarantee)).

---

## Status (2026-10-21)

| Step | State |
|---|---|
| (a) TPX client + `edge search` | **Built; live run pending.** The build environment could not reach `tpx.tradepost.co` / `platform-api.tradepost.co` (network policy) and had no `TPX_API_KEY`, so query/field names come from the brief, not from the live schema. Run `edge doctor` first (below). |
| (b) `edge compare` with `--dn-bid/--dn-ask` | Built, verified against the offline demo fixtures (`--demo`). |
| (c) CSV source + `edge watchlist` | Built. |
| (d) Playwright scraper | **Not built** — gated on you confirming `DEALERNET_ACCESS=scrape` and Dealernet's terms. `edge/dealernet/scrape.py` is a stub. |
| (e) `edge watch` | Built with a polling transport. The socket transport needs the URL + message framing from *Integration → Realtime Socket*; the sequence/RESYNC state machine (`edge/realtime.py`) is done and tested. |

### Confirmed settings (2026-10-21)

| Setting | Value | Change in |
|---|---|---|
| `VERTICAL` | `both` | `.env` or `--vertical` |
| `DEALERNET_ACCESS` | `manual` (CSV also works; scrape not built) | `.env` |
| `DEALERNET_PLAN` | `vendor` (0% at a listed price, 3% name-your-price/counter). The same fee schedule is applied to `basic`. | `.env`, `DEALERNET_*_FEE_BPS` |
| `SHIPPING_USD` | $15 per box, $45 per case, $0 handling, **per physical move** | `.env` or `--shipping` |
| `TARGET_MARGIN` | 8% on capital deployed | `.env` or `--margin` |

---

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # put TPX_API_KEY=tp_live_... in it (never committed)
```

## First run: validate the schema, then search

```bash
edge doctor                      # introspects the live TPX schema and checks every
                                 # query/field in edge/tpx/queries.py; prints asset types
edge search "2024 topps chrome baseball hobby"
```

If `doctor` reports a field or input type that does not exist, fix it in
**`edge/tpx/queries.py`** only — nothing else hard-codes GraphQL. Where the
docs and the live API disagree, the live API wins; note the change in the
[Live-API notes](#live-api-notes) section below.

If `listAssetTypes` is not a real query, pin the IDs from the docs
(*Verticals → Sealed Wax / Sealed TCG*):

```
TPX_ASSET_TYPE_SEALED_WAX=...
TPX_ASSET_TYPE_SEALED_TCG=...
```

### Try it offline

`--demo` serves TPX from hand-built fixtures in `tests/fixtures/demo` (no key):

```bash
edge --demo search "2024 topps chrome baseball hobby"
edge --demo compare "2024 topps chrome baseball hobby" --dn-bid 252 --dn-ask 258 --dn-qty 3
edge --demo watchlist watchlist.example.yaml --csv edges.csv
edge --demo compare "2024 topps chrome baseball hobby" --dn-csv dealernet.example.csv
```

## Commands

```
edge search  "<text>" [--limit N] [--quick]
edge compare "<product>" [--dn-bid X --dn-ask Y --dn-qty N | --dn-csv file.csv [--dn-name "<name>"]]
                         [--margin 0.08] [--shipping 15] [--pick i | --id <assetConfigurationId>]
                         [--json out.json] [--csv out.csv] [--md out.md] [--no-prompt]
edge watchlist watchlist.yaml [--dn-csv file.csv] [--margin 0.08] [--csv out.csv] [--json out.json]
edge map                      # list mappings.yaml
edge map add <tpx_id> "<dealernet name>" [--tpx-name ...] [--url ...]
edge map remove <tpx_id>
edge map suggest dealernet.csv
edge watch "<product>" [--dn-bid/--dn-ask | --dn-csv] [--interval 10] [--dn-refresh 300] [--alert 10]
edge doctor
edge --record ...             # record every live TPX response to tests/fixtures/
```

`compare` with no Dealernet flags prompts you for bid/ask/qty (`--no-prompt`
for a TPX-only view). With `--dn-csv` the Dealernet product is matched by
`mappings.yaml` first, then fuzzy name match (`edge/matching.py`, threshold 70),
or exactly with `--dn-name`.

### Dealernet CSV layouts

Ladder (one row per offer — paste a product page's left/right columns):

```
product,side,price,qty,member,observed_at
2024 Topps Chrome Baseball Hobby Box,bid,252,3,m1829,2026-10-20
2024 Topps Chrome Baseball Hobby Box,ask,258,3,m0412,2026-10-20T18:00:00Z
```

Wide (one row per product): `product,bid,bid_qty,ask,ask_qty[,observed_at]`.
Prices in dollars; `-` as the path reads stdin. See `dealernet.example.csv`.

### TCGplayer reference (sealed TCG)

Set `TCGPLAYER_PUBLIC_KEY` and `TCGPLAYER_PRIVATE_KEY` and the dashboard adds a
**vs TCGplayer mkt** column: TCGplayer market price, then the TPX and Dealernet
high bid / low ask as a percent of it (100% = at market). The TCGplayer product
is found by id from TPX's own catalog feed (`externalIds.TCG_PLAYER_ID`), so no
name matching; override per product with `tcgplayer_id:` in the watchlist.
Sports wax has no TCGplayer listing and shows "—". Read-only (token + pricing
GETs only); the hosts `api.tcgplayer.com` must be reachable.

## How the numbers are built

Everything is computed in integer/Decimal **cents**; output is $ and % of
capital deployed. **Book prices are always CLEAN (pre-fee)**, exactly as TPX
serves them; columns say "all-in" when fees are applied. Every figure carries
its source and timestamp.

### Cost models

| Venue | Source | Fees | Shipping |
|---|---|---|---|
| TPX market | `getMarket → configuration.data.feeModel` | `BUYER_SELLER` (buyer/seller bps) or `MAKER_TAKER` (maker/taker bps), tick size, lot size | $15 box / $45 case per move; **0 on vaulted markets** (settles in the vault) |
| Dealernet | `DEALERNET_PLAN` | taker (hit a listed price) 0 bps, maker (name your price / counter) 300 bps — modelled as a MAKER_TAKER schedule | $15 box / $45 case per move |

```
all-in buy   = clean × (1 + fee_bps/10000) + fixed
all-in sell  = clean × (1 − fee_bps/10000) − fixed
cost         = all-in buy  + shipping_in
proceeds     = all-in sell − shipping_out
edge         = proceeds − cost          pct = edge / cost
```

### Worked example (hand-checked; `tests/test_edge_math.py`)

2024 Topps Chrome Baseball Hobby Box. TPX **Vault** market: `BUYER_SELLER`,
buyer 0 bps, seller 500 bps, $1 tick, vaulted (no shipping). Dealernet: Vendor
plan, $15 shipping per move. Margin 8 %.

**Taker arb A — buy Dealernet ask $258, sell TPX Vault bid $258**

```
cost     = 258.00 × 1.00 + 15.00 ship in   = 273.00
proceeds = 258.00 × 0.95 − 0               = 245.10
edge     = −27.90 / unit  (−10.2 % of capital)      → no edge
```

**Taker arb B — buy TPX Vault ask $240, sell Dealernet bid $265 (qty 2 @ 265, 4 @ 260)**

```
size 1: cost 240.00, proceeds 265.00 − 15.00 = 250.00      edge +10.00 (+4.2 %)
size 3: buy 2@240 + 1@245 = 725.00 ; sell 2@265 + 1@260 = 790.00 − 45.00 = 745.00  edge +20.00
```

**Maker: highest bid to rest on TPX Vault, flipping into Dealernet bid $265**

```
proceeds  = 265.00 − 15.00                 = 250.00
max cost  = 250.00 / 1.08                  = 231.48
bid ≤ (231.48 − 0 ship) / (1 + 0 bps)      = 231.48 → round down to tick → $231
current TPX bid $225 → rests 6 ticks inside: best-in-book ✔
```

**Maker: highest bid to rest on Dealernet (3 %), flipping into TPX Vault bid $258**

```
proceeds  = 258.00 × 0.95                  = 245.10
max cost  = 245.10 / 1.08                  = 226.94
bid ≤ (226.94 − 15.00) / 1.03              = 205.77 → $205
```

**Maker: lowest ask to rest on TPX Ship (`MAKER_TAKER` 100/400, $15 ship), covering from Dealernet ask $258**

```
cost      = 258.00 + 15.00                 = 273.00 ;  need ≥ 273.00 × 1.08 = 294.84
ask ≥ (294.84 + 15.00 ship out) / (1 − 0.01) = 312.97 → round up → $313
current TPX ask $279 → 34 ticks behind: not best-in-book
```

**Fair value**: FV $266.50 → Dealernet ask $258 is −3.2 % vs FV → flag
"Dealernet ask BELOW fair value". With `fairValues: []` the row says **no FV**.

### Liquidity factor and ranking

```
depth_part = min(1, min(bid_units, ask_units) / size_cap)          size_cap = 10
vel_part   = min(1, units_30d / size_cap)      (0.5 when unknown, e.g. Dealernet)
factor     = 0.4 × depth_part + 0.6 × vel_part   (halved if a top-of-book level is stale > STALE_DAYS)
score      = best_taker_edge_per_unit × min(max_size, size_cap) × factor_TPX × factor_DN   (in $)
```

Negative edges are not scaled by the factor (so an illiquid book never looks
"less bad"). Flags: `stale bid/ask (Nd)`, `thin bids/asks`, `no bids/asks`,
`wide spread`, `no trade in Nd`.

Every ranked row names which leg is taker vs maker and which TPX market
(vaulted or not) it assumes. Vaulted sell legs are annotated: you need vaulted
units; vault deposit/withdrawal shipping is **not** modelled.

## Read-only guarantee

* `edge/tpx/queries.py` contains only queries. `TPXClient.execute` refuses any
  document containing `mutation` or `createOrderSubmission` (tested).
* `getOrderQuote` is used purely to read TPX's own all-in number for a size.
* `TODO(place)`: a future `edge place` must live in its own module, be gated
  behind an explicit `--i-really-want-to-trade` flag **and** an interactive
  confirmation, and is deliberately not built.

## Secrets & files

* `.env` holds `TPX_API_KEY` (a `SecretStr`, never logged, never in fixtures).
* `.gitignore` covers `.env`, `storage_state.json`, `.cache/`, `out/`.
* `.cache/tpx_ids.json` caches asset type IDs and the per-asset-type MINT key.
* `mappings.yaml` persists TPX ↔ Dealernet name overrides.

## Tests

```bash
pytest -q
```

Hand-checked edge math, client error handling (top-level `errors` before
`data`, per-payload `error`, retries, cache, mutation refusal), CSV parsing,
fuzzy matching, BookSync sequence rules, and CLI end-to-end against the demo
fixtures. Recorded live responses (`edge --record ...`) land in
`tests/fixtures/` in the same shape the mock serves.

## Live-API notes

Record here anything where the live API differs from the docs/brief. The
tool trusts the live API.

* _(none yet — first `edge doctor` run pending a network-enabled environment
  with the key)_

## Layout

```
edge/
  config.py              pydantic-settings from .env (key, plan, shipping, margin)
  models.py              VenueQuote, CostModel, BookLevel, Product (cents, clean vs all-in)
  tpx/queries.py         every GraphQL document (queries only)
  tpx/client.py          X-API-Key, error handling, retry/backoff, limiter, TTL cache, recorder
  tpx/schema_check.py    introspection validator used by `edge doctor`
  tpx/catalog.py         asset types, MINT key per asset type (cached), product search
  tpx/market.py          markets+fees, book, ticker, stats, trades, candles, fair value
  tpx/mock.py            offline mock transport (demo + tests)
  dealernet/base.py      DealernetSource.get_quote(product) -> DealernetQuote
  dealernet/manual.py    flags / prompt
  dealernet/csv_source.py
  dealernet/scrape.py    stub (gated)
  matching.py            normalisation, fuzzy match, mappings.yaml
  edge.py                pure edge math
  realtime.py            BookSync (seq / RESYNC) + polling feed
  report.py              rich tables, CSV / JSON / markdown export
  service.py             orchestration
  cli.py                 typer: search, compare, watchlist, map, watch, doctor
tests/                   pytest + fixtures
```


## Dashboard (view only)

```
edge dashboard watchlist.yaml --dn-csv dealernet.csv -o dashboard.html --open
edge dashboard watchlist.yaml --dn-csv dealernet.csv --refresh 300   # re-run every 5 min; the page reloads itself
edge --demo dashboard watchlist.example.yaml --dn-csv dealernet.example.csv   # offline sample
```

Writes one self-contained HTML file: KPI tiles, the ranked edge table (filter, "edges only", sortable
columns), a 30-day TPX price chart per product with Dealernet bid/ask and fair value as reference lines,
and an edge-over-time sparkline built from `.cache/history.jsonl` (one snapshot per run; demo and live
snapshots are kept apart). Demo output carries a "Sample data" banner.

The page has no buttons, forms or network calls. `ACTIONS_ENABLED` (default `false`) is the master
switch for any future feature that would act on a marketplace; the dashboard shows its state, and no
such feature exists yet.

## Hosted web app (Railway)

`edge/webapp.py` serves the board as a web app: it keeps your picks and refresh cadence in
`$DATA_DIR/board.json`, re-runs `edge dashboard` on that cadence (the hottest list at most hourly,
your board alone in between, down to every 15 min), and runs a board-only refresh when you press
**Refresh**. Run it locally with `EDGE_DEMO=1 python -m edge.webapp` (http://localhost:8080).

Railway service (deploys on every push to the branch it watches):

| Setting | Value |
|---|---|
| Source | this repo, branch `main` (`railway.json` at the root: Dockerfile build, `/healthz` health check) |
| Volume | mount path `/data` (board, history, catalogue cache, Dealernet session) |
| Variables | `TPX_API_KEY`, `DEALERNET_ACCESS=scrape`, `DEALERNET_USERNAME`, `DEALERNET_PASSWORD`, `APP_PASSWORD` (login for the page; any user name) |
| Optional | `HOT_COUNT` (hottest products priced per vertical, default 25), `FULL_EVERY_S` (default 3600) |

Secrets live only in Railway's Variables; nothing secret is in the image or the repo.
