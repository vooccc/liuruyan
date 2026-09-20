#!/usr/bin/env python3
"""Deprecated temporary helper kept for compatibility.

Use generate_image.py instead:
  python generate_image.py --model gpt-image-2 --prompt "..."
"""
import sys

print("_call_codex.py is deprecated; use generate_image.py --model gpt-image-2 instead.", file=sys.stderr)
sys.exit(1)
