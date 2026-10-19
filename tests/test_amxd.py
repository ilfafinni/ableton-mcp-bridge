#!/usr/bin/env python3
"""Valida que device_helper.amxd esté bien cableado (sin cables rotos).

Uso:  python tests/test_amxd.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AMXD = os.path.join(HERE, "..", "bridge", "device_helper.amxd")


def main():
    data = open(AMXD, encoding="utf-8", errors="replace").read()
    start = data.find("{")
    if start < 0:
        print("FAIL: no hay JSON en el .amxd")
        return 1
    j, _ = json.JSONDecoder().raw_decode(data[start:])
    patcher = j["patcher"]
    boxes = {}
    for box in patcher["boxes"]:
        b = box["box"]
        boxes[b["id"]] = b
    errors = []
    for ln in patcher["lines"]:
        p = ln["patchline"]
        src, so = p["source"]
        dst, di = p["destination"]
        if src not in boxes:
            errors.append(f"source '{src}' no existe")
            continue
        if dst not in boxes:
            errors.append(f"dest '{dst}' no existe")
            continue
        if so >= boxes[src].get("numoutlets", 0):
            errors.append(f"{src}.{so}->{dst}.{di}: outlet fuera de rango")
        if di >= boxes[dst].get("numinlets", 0):
            errors.append(f"{src}.{so}->{dst}.{di}: inlet fuera de rango")
    if errors:
        print(f"FAIL ({len(boxes)} boxes, {len(patcher['lines'])} lines):")
        for e in errors:
            print("  ", e)
        return 1
    print(f"OK: {len(boxes)} boxes, {len(patcher['lines'])} lines, sin cables rotos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
