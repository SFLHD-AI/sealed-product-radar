"""Turn the board's saved settings (settings/board, as the ArtifactData tool
returns it) into a watchlist YAML for `edge dashboard`.

usage: python scripts/board_watchlist.py settings_board.json watchlist.board.yaml
Prints the refresh cadence in hours (0 = paused) on stdout.
"""
import json
import sys

import yaml

src, dst = sys.argv[1], sys.argv[2]
raw = json.load(open(src))
doc = raw.get("data", raw)  # accept either the bare document or the tool's {id, data, version} wrapper
products = []
for p in doc.get("products") or []:
    name = str(p.get("name") or "").strip()
    if not name:
        continue
    item = {"name": name}
    if p.get("tpx_id"):
        item["tpx_id"] = p["tpx_id"]
    if p.get("shipping") is not None:
        item["shipping"] = p["shipping"]
    elif any(w in name.lower() for w in ("pack", "tin", "blister")):
        item["shipping"] = 4
    products.append(item)
with open(dst, "w") as f:
    f.write("# Generated from the board's My products panel; do not edit by hand.\n")
    yaml.safe_dump({"products": products}, f, sort_keys=False, allow_unicode=True)
print(int(doc.get("hours", 1) or 0))
