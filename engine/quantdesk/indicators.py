"""Indicators. Pure functions over price arrays; no IO, no globals.

`rsi_wilder` implements strategy_spec.md section 4 exactly, including its
one-based bar indexing. The caller is responsible for handing in only bars on
or after `rsi_start`; this module never sees the cache.
"""

from __future__ import annotations

import numpy as np

from quantdesk import config


def rsi_wilder(close, period: int = config.RSI_PERIOD) -> np.ndarray:
    """Wilder's RSI over bar indices 1..N (strategy_spec.md section 4).

    ``close`` is the ticker's closes from bar 1 (the first cached bar on or
    after `rsi_start`) forward, in session order. Returns an array of the same
    length; entries before bar `period + 1` are NaN because the recursion is
    not yet seeded.

    The spec writes the recursion in one-based bar indices:

        change[t] = close[t] - close[t-1]                       t >= 2
        gain[t]   = max(change[t], 0);  loss[t] = max(-change[t], 0)
        avg_gain[15] = mean(gain[2..15]);  avg_loss[15] = mean(loss[2..15])
        avg_gain[t]  = (avg_gain[t-1] * 13 + gain[t]) / 14      t > 15
        RSI[t] = 100                     if avg_loss[t] == 0
        RSI[t] = 100 - 100/(1 + avg_gain[t]/avg_loss[t])  otherwise

    with `period = 14`, so bar 15 is the first bar with a value.
    """
    values = np.asarray(close, dtype=np.float64)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return out

    change = np.diff(values)                 # change[i] is change[t=i+2]
    gain = np.maximum(change, 0.0)
    loss = np.maximum(-change, 0.0)

    # Seed: avg over gain[2..period+1], which is change positions 0..period-1.
    avg_gain = float(gain[:period].mean())
    avg_loss = float(loss[:period].mean())

    seed_pos = period                        # zero-based position of bar period+1
    out[seed_pos] = _rsi_from_averages(avg_gain, avg_loss)

    k = float(period - 1)
    p = float(period)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * k + gain[i]) / p
        avg_loss = (avg_loss * k + loss[i]) / p
        out[i + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def trend_mean(close, window: int = config.TREND_WINDOW) -> np.ndarray:
    """Rolling mean of the `window` most recent bars ending at each bar.

    Entries before bar `window` are NaN. Used for strategy_spec.md section 5
    condition 4: `close[T] >= mean(close[b-199..b])`.
    """
    values = np.asarray(close, dtype=np.float64)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    csum = np.concatenate(([0.0], np.cumsum(values)))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def rolling_median_dollar_volume(
    close, volume, window: int = config.DOLLAR_VOLUME_WINDOW
) -> np.ndarray:
    """Median of `close * volume` over the `window` most recent bars.

    Section 5 condition 6. Entries before bar `window` are NaN.
    """
    c = np.asarray(close, dtype=np.float64)
    v = np.asarray(volume, dtype=np.float64)
    dollar = c * v
    n = dollar.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    # Sliding window view keeps this O(n * window) but vectorized in numpy,
    # which is fast enough for 92 tickers over 11 years.
    view = np.lib.stride_tricks.sliding_window_view(dollar, window)
    out[window - 1:] = np.median(view, axis=1)
    return out


def realized_vol(close, window: int = config.PLACEBO_VOL_WINDOW) -> np.ndarray:
    """Standard deviation of daily log returns over bar indices `b-13..b`.

    strategy_spec.md section 16.4. `window` is the number of log returns in the
    measure. Entries with fewer than `window` returns available are NaN.
    """
    values = np.asarray(close, dtype=np.float64)
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window + 1:
        return out
    logret = np.diff(np.log(values))
    view = np.lib.stride_tricks.sliding_window_view(logret, window)
    out[window:] = view.std(axis=1, ddof=1)
    return out
