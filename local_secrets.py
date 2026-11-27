"""Read and update the project's ignored .env credential file."""

import json
import os
from pathlib import Path
import re

ENV_FILE = Path(__file__).resolve().with_name(".env")


def secret(name):
    if name in os.environ:
        return os.environ[name]
    if not ENV_FILE.is_file():
        return ""
    pattern = assignment(name)
    for line in reversed(ENV_FILE.read_text(encoding="utf-8-sig").splitlines()):
        match = pattern.match(line)
        if match:
            value = match[1].strip()
            if value.startswith('"'):
                return json.JSONDecoder().raw_decode(value)[0]
            return value.split(" #", 1)[0].strip().strip("'")
    return ""


def assignment(name):
    return re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)$")


def flag(name, default=False):
    raw = secret(name).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag (true or false).")


def store_secret(name, value):
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.is_file() else []
    pattern = assignment(name)
    lines = [line for line in lines if not pattern.match(line)]
    lines.append(f"{name}={json.dumps(value)}")
    temporary = ENV_FILE.with_name(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(ENV_FILE)
