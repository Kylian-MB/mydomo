"""Trim the raw models.dev list into the catalog Domo ships.

Usage: trim_models_dev.py <api.json> <out.json> --usd-per-eur <rate> [--fetched YYYY-MM-DD]

The rate is the day's dollars for one euro (the ``USD`` line of the ECB daily rates).
"""

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from domo_core.llm.catalog import trim_models_dev


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--usd-per-eur", type=Decimal, required=True)
    parser.add_argument("--fetched", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    raw = json.loads(args.source.read_text(encoding="utf-8"))
    trimmed = trim_models_dev(raw, fetched=args.fetched, usd_per_eur=args.usd_per_eur)
    text = json.dumps(trimmed, indent=1, ensure_ascii=False) + "\n"
    args.target.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
