"""Data providers.

The primary is Tiingo's end-of-day API; the cross-check is Alpha Vantage
`TIME_SERIES_DAILY` (strategy_spec.md section 3). The split-adjusted-only bars
of section 3 are built here, in `base.split_adjust`, from the primary
provider's raw fields; no provider's own adjusted column is ever used.
"""

from quantdesk.providers.base import (
    BARS_COLUMNS,
    RAW_COLUMNS,
    Provider,
    ProviderError,
    split_adjust,
)

__all__ = [
    "Provider",
    "ProviderError",
    "BARS_COLUMNS",
    "RAW_COLUMNS",
    "split_adjust",
]
