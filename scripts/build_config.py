#!/usr/bin/env python3
"""Build deployment configuration JSON from workflow inputs.

Reads INPUT_* environment variables and writes /tmp/config.json.
"""
import json
import os

accessories_raw = os.environ.get("INPUT_ACCESSORIES") or "[]"
try:
    accessories = json.loads(accessories_raw)
except json.JSONDecodeError:
    accessories = []

config = {
    "zone": os.environ.get("INPUT_ZONE") or "ZP01",
    "web_plan": os.environ.get("INPUT_WEB_PLAN") or "small",
    "web_disk_size_gb": int(os.environ.get("INPUT_WEB_DISK_SIZE_GB") or "20"),
    "workers_replicas": int(os.environ.get("INPUT_WORKERS_REPLICAS") or "0"),
    "workers_plan": os.environ.get("INPUT_WORKERS_PLAN") or "small",
    "accessories": accessories,
    "recover": os.environ.get("INPUT_RECOVER") == "true",
}

with open("/tmp/config.json", "w") as f:
    json.dump(config, f, indent=2)

print("Configuration:")
for k, v in config.items():
    print(f"  {k}: {v}")
