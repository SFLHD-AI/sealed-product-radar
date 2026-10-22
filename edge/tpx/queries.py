"""Every GraphQL document the tool sends, in one place.

STATUS: verified against the live API on 2026-10-21 (tpx.tradepost.co/v2 docs
plus live probes). The live API has introspection disabled and masks validation
errors as INVALID_REQUEST, so `edge doctor` runs each query below against a real
product instead of reading the schema. Fixes land here and nowhere else.

Only queries are defined. No mutation (createOrderSubmission etc.) is, or may
be, added here - the tool is read-only.
"""
from __future__ import annotations

ERROR_FRAGMENT = "error { message code }"

INTROSPECTION = """
query EdgeIntrospection {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields { name args { name type { ...TypeRef } } type { ...TypeRef } }
      inputFields { name type { ...TypeRef } }
      enumValues { name }
    }
  }
}
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
}
"""

# --- Catalog -------------------------------------------------------------

# Asset types (open query, no auth needed).
LIST_ASSET_TYPES = """
query EdgeListAssetTypes($input: ListAssetTypesInput!) {
  assetTypes(input: $input) {
    %s
    assetTypes { id type slug displayName tpxEnabled }
  }
}
""" % ERROR_FRAGMENT

LIST_KEY_CONFIGURATIONS = """
query EdgeListKeyConfigurations($input: ListKeyConfigurationsInput!) {
  listKeyConfigurations(input: $input) {
    %s
    keyConfigurations {
      id
      assetTypeId
      keyData { keys { keyId keyType value displayName ancestors } }
    }
  }
}
""" % ERROR_FRAGMENT

FILTER_BUILDER_SEARCH = """
query EdgeSearch($input: FilterBuilderInput!) {
  getFilterBuilderData(input: $input) {
    %s
    totalProducts
    preview {
      id
      name
      assetId
      assetName
      slug
      fairValueCents
      data
    }
  }
}
""" % ERROR_FRAGMENT

# --- Markets & fees ------------------------------------------------------

GET_PRODUCT_MARKETS = """
query EdgeProductMarkets($input: GetProductMarketsInput!) {
  getProductMarkets(input: $input) {
    %s
    markets {
      market { id displayName isVaulted visibility }
      canBuy
      canSell
      canBid
      canAsk
      associations { tradingState }
    }
  }
}
""" % ERROR_FRAGMENT

GET_MARKET = """
query EdgeMarket($input: GetMarketInput!) {
  getMarket(input: $input) {
    %s
    market {
      id
      displayName
      isVaulted
      startAt
      endAt
      configuration {
        data {
          feeModel
          buyerFeeBps
          sellerFeeBps
          makerFeeBps
          takerFeeBps
          tickSizeCents
          lotSize
          minOrderQuantity
          maxOrderQuantity
          minUnitPriceCents
          maxUnitPriceCents
        }
      }
    }
  }
}
""" % ERROR_FRAGMENT

# --- Order book & tape ---------------------------------------------------

GET_ORDER_BOOK = """
query EdgeOrderBook($input: GetOrderBookInput!) {
  getOrderBook(input: $input) {
    %s
    sequence
    serverNow
    bids { priceCents quantity numMakers restingSince lastChangedAt }
    asks { priceCents quantity numMakers restingSince lastChangedAt }
    summary {
      topBidCents topAskCents midCents
      bidUnits askUnits bidDepthCents askDepthCents imbalancePercent
    }
  }
}
""" % ERROR_FRAGMENT

GET_TICKER = """
query EdgeTicker($input: GetTickerInput!) {
  getTicker(input: $input) {
    %s
    keyTicker {
      lastPriceCents lastTradeAt volume24h high24hCents low24hCents tradeCount24h
    }
  }
}
""" % ERROR_FRAGMENT

GET_RECENT_TRADES = """
query EdgeRecentTrades($input: GetRecentTradesInput!) {
  getRecentTrades(input: $input) {
    %s
    trades { executedAt priceCents quantity takerSide }
  }
}
""" % ERROR_FRAGMENT

GET_CANDLES = """
query EdgeCandles($input: GetCandlesInput!) {
  getCandles(input: $input) {
    %s
    candles { bucket openCents highCents lowCents closeCents volume vwapCents trades }
  }
}
""" % ERROR_FRAGMENT

GET_PRODUCT_MARKET = """
query EdgeProductMarketStats($input: GetProductMarketInput!) {
  getProductMarket(input: $input) {
    %s
    stats {
      lastPriceCents volumeCents unitVolume tradeCount vwapCents highCents lowCents
      priceChangeCents priceChangePercent lastTradeAt holderCount
    }
    depth { topBidCents topAskCents midCents spreadCents bidUnits askUnits }
    priceSeries { date volumeCents unitsSold tradeCount averageSalePriceCents }
  }
}
""" % ERROR_FRAGMENT

# --- Fair value ----------------------------------------------------------

GET_KEY_FAIR_VALUES = """
query EdgeFairValues($input: GetKeyFairValuesByAssetConfigInput!) {
  getKeyFairValuesByAssetConfig(input: $input) {
    %s
    keys {
      keyId
      keyType
      value
      fairValues { valueCents modelVersion }
    }
  }
}
""" % ERROR_FRAGMENT

# --- Catalog feed (daily bulk catalog; carries TCGplayer product ids) ----

GET_CATALOG_FEEDS = """
query EdgeCatalogFeeds($input: GetCatalogFeedsInput!) {
  getCatalogFeeds(input: $input) {
    %s
    feeds { assetType uri uriExpiresAt recordCount lastUpdated }
  }
}
""" % ERROR_FRAGMENT

# --- Quotes (read-only pricing check, NOT a submission) ------------------

GET_ORDER_QUOTE = """
query EdgeOrderQuote($input: GetOrderQuoteInput!) {
  getOrderQuote(input: $input) {
    %s
    quote {
      side requestedQuantity fillableQuantity subtotalCents feeTotalCents totalCents
      effectiveUnitPriceCents worstPriceCents worstAllInPriceCents
      levels { priceCents quantity allInPriceCents }
      marketId
    }
  }
}
""" % ERROR_FRAGMENT

# Names used by `edge doctor` to validate against the live schema.
# (query field, input type name or None, payload sub-field -> selected fields)
EXPECTED_QUERIES: dict[str, dict] = {
    "listKeyConfigurations": {"input": "ListKeyConfigurationsInput", "fields": ["keyConfigurations"]},
    "getFilterBuilderData": {"input": "FilterBuilderInput", "fields": ["preview", "totalProducts"]},
    "getProductMarkets": {"input": "GetProductMarketsInput", "fields": ["markets"]},
    "getMarket": {"input": "GetMarketInput", "fields": ["market"]},
    "getOrderBook": {"input": "GetOrderBookInput", "fields": ["bids", "asks", "summary", "sequence", "serverNow"]},
    "getTicker": {"input": "GetTickerInput", "fields": ["keyTicker"]},
    "getRecentTrades": {"input": "GetRecentTradesInput", "fields": ["trades"]},
    "getCandles": {"input": "GetCandlesInput", "fields": ["candles"]},
    "getProductMarket": {"input": "GetProductMarketInput", "fields": ["stats", "depth", "priceSeries"]},
    "getKeyFairValuesByAssetConfig": {"input": "GetKeyFairValuesByAssetConfigInput", "fields": ["keys"]},
    "getOrderQuote": {"input": "GetOrderQuoteInput", "fields": ["quote"]},
    "assetTypes": {"input": "ListAssetTypesInput", "fields": ["assetTypes"], "optional": True},
}

# Mutations the tool must never call. `edge doctor` asserts none are referenced.
FORBIDDEN_OPERATIONS = ("createOrderSubmission", "cancelOrderSubmission", "mutation")
