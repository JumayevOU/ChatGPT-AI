def clean_response(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("###"):
        lines.pop(0)
    return "\n".join(lines).strip()
