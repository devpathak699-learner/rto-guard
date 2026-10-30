"""RTO Guard — decide what to do with every COD order before it ships."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT / "data" / "orders_sample.csv"
CONFIG_PATH = ROOT / "config" / "rules.yaml"
ARTIFACTS = ROOT / "artifacts"
MODEL_PATH = ARTIFACTS / "risk_model.json"
