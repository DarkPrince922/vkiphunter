"""Parse OpenRC assignments as text; never execute shell code."""
import re


def parse_openrc(text: str) -> dict[str, str]:
    """Выдрать OS_*-переменные из содержимого openrc-файла."""
    found: dict[str, str] = {}
    pattern = re.compile(
        r"^\s*(?:export\s+)?(OS_[A-Z0-9_]+)\s*=\s*(.*?)\s*$", re.MULTILINE
    )
    for name, raw in pattern.findall(text):
        value = raw.strip().strip('"').strip("'")
        if value.startswith("$") or not value:
            continue
        found[name] = value
    return found
