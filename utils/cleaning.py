def clean_response(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("###"):
        lines = lines[1:]
    return "\n".join(line.strip() for line in lines).strip()
