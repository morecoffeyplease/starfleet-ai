#!/usr/bin/env python3
"""Compatibility CLI for the renamed Pattern Buffer app."""
import importlib.util
from pathlib import Path
import sys


source = Path(__file__).resolve().parents[1] / "pattern-buffer" / "pattern_buffer.py"
spec = importlib.util.spec_from_file_location("pattern_buffer_legacy_cli_core", source)
if spec is None or spec.loader is None:
    raise SystemExit(f"Pattern Buffer source is missing: {source}")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
globals().update({name: value for name, value in vars(module).items()
                  if name not in {"__name__", "__file__", "__loader__", "__package__", "__spec__"}})

if __name__ == "__main__":
    raise SystemExit(module.main())
