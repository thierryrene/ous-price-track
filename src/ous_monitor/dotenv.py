"""Loader minimalista de .env (sem dependência externa).

Aceita `KEY=valor`, `KEY="valor"`, `KEY='valor'`. Ignora comentários (`#`)
e linhas em branco. Não faz expansão de variáveis (não precisamos).
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> int:
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        # variáveis já no ambiente têm precedência (útil pra cron)
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
