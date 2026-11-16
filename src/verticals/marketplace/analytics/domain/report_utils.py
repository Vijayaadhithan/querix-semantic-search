"""Shared calculations used by marketplace report groups."""

import pandas as pd


def _percentage(count, total):
    return round(count / total * 100, 1) if total else 0


def _rounded_stat(value, digits=1):
    return 0 if pd.isna(value) else round(float(value), digits)


def _nonnegative_int(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
