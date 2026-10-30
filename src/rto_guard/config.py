"""Load, validate and save the business rules in config/rules.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import CONFIG_PATH


class ConfigError(ValueError):
    pass


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    """Catch the mistakes a non-technical editor is likely to make."""
    b = cfg["risk_bands"]
    bands = [b["ship_below"], b["nudge_below"], b["confirm_below"]]
    if not all(0 <= x <= 100 for x in bands):
        raise ConfigError("Risk bands must be between 0 and 100.")
    if not bands[0] <= bands[1] <= bands[2]:
        raise ConfigError(
            "Risk bands must go up: ship_below <= nudge_below <= confirm_below "
            f"(got {bands[0]}, {bands[1]}, {bands[2]})."
        )
    for key in ("confirm_no_reply_action", "hold_no_reply_action"):
        if cfg["whatsapp"][key] not in ("ship", "cancel"):
            raise ConfigError(f"whatsapp.{key} must be 'ship' or 'cancel'.")
    if cfg["prepaid_nudge"]["discount_rupees"] < 0:
        raise ConfigError("Discount cannot be negative.")


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    """Update numbers in place so the plain-English comments in the file survive."""
    validate(cfg)
    lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    section = None
    out = []
    for line in lines:
        stripped = line.strip()
        if line and not line[0].isspace() and stripped.endswith(":") and not stripped.startswith("#"):
            section = stripped[:-1]
        elif section and ":" in stripped and not stripped.startswith("#"):
            key = stripped.split(":", 1)[0]
            if key in cfg.get(section, {}):
                indent = line[: len(line) - len(line.lstrip())]
                comment = ""
                rest = stripped.split(":", 1)[1]
                if "#" in rest:
                    value_part, c = rest.split("#", 1)
                    pad = " " * (len(value_part) - len(value_part.rstrip()))
                    comment = f"{pad}#{c}"
                value = cfg[section][key]
                value = str(value).lower() if isinstance(value, bool) else value
                line = f"{indent}{key}: {value}{comment}\n"
        out.append(line)
    Path(path).write_text("".join(out), encoding="utf-8")


def rto_cost(cfg: dict) -> float:
    """₹ lost on one RTO: both shipping legs, handling, and damaged stock."""
    e = cfg["economics"]
    return (
        e["forward_shipping"]
        + e["return_shipping"]
        + e["packaging_handling"]
        + e["damaged_on_return_percent"] / 100 * e["avg_product_cost"]
    )
