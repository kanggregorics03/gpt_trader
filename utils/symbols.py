import re
from typing import Iterable, List


def normalize_symbol(symbol: str | None) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "/" in raw:
        pair = raw.split(":", 1)[0]
        if "/" in pair:
            base, quote = pair.split("/", 1)
            return re.sub(r"[^A-Z0-9]", "", f"{base}{quote}")
    return re.sub(r"[^A-Z0-9]", "", raw)


def normalize_symbol_list(symbols: Iterable[str] | None) -> List[str]:
    normalized: List[str] = []
    for symbol in symbols or []:
        value = normalize_symbol(symbol)
        if value:
            normalized.append(value)
    return normalized
