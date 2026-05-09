"""Helpers para apresentação de tamanhos.

format_sizes_compact(["34","35","36","38","40","41","42"])
    -> "34–36, 38, 40–42"
"""
from __future__ import annotations

from typing import List, Tuple


def _try_numeric(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def format_sizes_compact(sizes: List[str]) -> str:
    if not sizes:
        return ""
    # separa numéricos (que viram faixas) dos textuais (P, M, G, ÚNICO...)
    numeric: List[Tuple[float, str]] = []
    textual: List[str] = []
    for s in sizes:
        s = s.strip()
        if not s:
            continue
        v = _try_numeric(s)
        if v is None:
            textual.append(s)
        else:
            numeric.append((v, s))

    # de-dup preservando ordem
    seen: set = set()
    numeric_uniq: List[Tuple[float, str]] = []
    for v, s in numeric:
        if v in seen:
            continue
        seen.add(v)
        numeric_uniq.append((v, s))
    numeric_uniq.sort(key=lambda t: t[0])

    parts: List[str] = []
    if numeric_uniq:
        i = 0
        while i < len(numeric_uniq):
            j = i
            # Avança enquanto a diferença for "regular" — para inteiros = 1.
            # Usamos passo do primeiro par como referência.
            step = None
            while j + 1 < len(numeric_uniq):
                diff = numeric_uniq[j + 1][0] - numeric_uniq[j][0]
                if step is None:
                    step = diff
                elif abs(diff - step) > 1e-9:
                    break
                # Passo permitido: 1.0 (inteiros consecutivos) ou 0.5 (meios).
                if step not in (1.0, 0.5):
                    break
                j += 1
            if j == i:
                parts.append(numeric_uniq[i][1])
            elif j == i + 1:
                parts.append(f"{numeric_uniq[i][1]}, {numeric_uniq[j][1]}")
            else:
                parts.append(f"{numeric_uniq[i][1]}–{numeric_uniq[j][1]}")
            i = j + 1

    parts.extend(dict.fromkeys(textual))  # de-dup textuais preservando ordem
    return ", ".join(parts)
