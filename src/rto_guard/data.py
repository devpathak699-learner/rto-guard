"""Load orders_sample.csv and fix the data-quality problems found in it.

Problems found in the raw file (see scripts/analyze.py):
  * 30 exact duplicate rows
  * final_status written 6 ways: DELIVERED, delivered, RTO, "RTO ", Rto, IN_TRANSIT
  * phones as 10 digits, 9 digits (truncated) and "+91 98xxx xxxxx"
  * 12 missing pincodes, 4 missing cities, 3 orders with value ₹0
"""

from __future__ import annotations

import re

import pandas as pd

from . import DATA_PATH

STATUS_MAP = {"DELIVERED": "DELIVERED", "RTO": "RTO"}


def normalise_phone(raw) -> str:
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def phone_is_valid(phone: str) -> bool:
    return bool(re.fullmatch(r"[6-9]\d{9}", phone or ""))


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop_duplicates().copy()
    df["final_status"] = df["final_status"].astype(str).str.strip().str.upper()
    df["phone"] = df["phone"].map(normalise_phone)
    df["pincode"] = (
        df["pincode"].astype(str).str.replace(r"\.0$", "", regex=True).replace({"nan": None})
    )
    df["city"] = df["city"].fillna("")
    df["order_datetime"] = pd.to_datetime(df["order_datetime"])
    return df


def load_orders(path=DATA_PATH, labelled_only: bool = True) -> pd.DataFrame:
    df = clean(pd.read_csv(path, dtype={"phone": str, "pincode": str}))
    if labelled_only:
        # IN_TRANSIT has no outcome yet, so it can't teach us anything.
        df = df[df["final_status"].isin(STATUS_MAP)].copy()
        df["is_rto"] = (df["final_status"] == "RTO").astype(int)
    return df.reset_index(drop=True)
