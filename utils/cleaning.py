def clean_response(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("###"):
        lines = lines[1:]
    return "\n".join(line.strip() for line in lines).strip()

import re
from typing import Dict

def clean_response(text: str) -> str:
    """
    Markdown va ortiqcha belgilardan tozalaydi:
      - CRLF -> LF normalizatsiya
      - Markdown sarlavhalar (##...#), ro'yxat belgilar (-, *, +, 1.), blockquote (>) olib tashlanadi
      - Inline/backtick emphasis (`code`, `*italic*`, **bold**, __underline__) belgilarini olib tashlaydi
      - Horizontal qoidalar (---, ***, ___) olib tashlanadi
      - Code-fence (```...```) ichidagi kodni SAQLAB qoladi (o'zgartirilmaydi)
      - Qator boshidagi "Assistant:", "User:", "System:" kabi prefixlarni olib tashlaydi
      - Ko'p bo'sh qatorlarni 1-2 ta qatorga qisqartiradi
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

