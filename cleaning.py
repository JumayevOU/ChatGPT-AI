import re
from typing import Dict

def clean_response(text: str) -> str:
    """
    Markdown va ortiqcha belgilarni olib tashlaydi, lekin code-fence (```...```)
    ichidagi kodni saqlaydi.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    code_blocks: Dict[str, str] = {}
    def _code_repl(m):
        key = f"__CODEBLOCK_{len(code_blocks)}__"
        code_blocks[key] = m.group(0)
        return key

    text_no_code = re.sub(r"```.*?```", _code_repl, text, flags=re.S)
    text_no_code = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text_no_code)
    text_no_code = re.sub(r"(?m)^\s*>\s?", "", text_no_code)
    text_no_code = re.sub(r"(?m)^\s*([-*+])\s+", "", text_no_code)
    text_no_code = re.sub(r"(?m)^\s*\d+[\.\)]\s+", "", text_no_code)
    text_no_code = re.sub(r"(?m)^[\s]*([-*_]){3,}[\s]*$", "", text_no_code)
    text_no_code = re.sub(r"(?mi)^\s*(assistant|user|system)\s*[:\-]\s*", "", text_no_code)
    text_no_code = re.sub(r"`([^`]+)`", r"\1", text_no_code)
    text_no_code = re.sub(r"(?s)\*\*(.+?)\*\*", r"\1", text_no_code)
    text_no_code = re.sub(r"(?s)__(.+?)__", r"\1", text_no_code)
    text_no_code = re.sub(r"(?s)\*(.+?)\*", r"\1", text_no_code)
    text_no_code = re.sub(r"(?s)_(.+?)_", r"\1", text_no_code)
    text_no_code = text_no_code.replace("\\*", "*").replace("\\_", "_").replace("\\`", "`")
    text_no_code = re.sub(r"\n{3,}", "\n\n", text_no_code)

    lines = [ln.rstrip() for ln in text_no_code.splitlines()]
    cleaned = "\n".join(lines).strip()

    for k, v in code_blocks.items():
        cleaned = cleaned.replace(k, v)

    return cleaned
