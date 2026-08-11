import re

# Limpeza OCR
_NM_PATTERNS = [
    re.compile(r'(\d)\s*\n\s*\.\s*(\d)'),
    re.compile(r'(\d)\s+\.\s+(\d)'),
    re.compile(r'(\d)\.\s+(\d)'),
    re.compile(r'(\d)\s+\.(\d)'),
]

_OCR_PATTERNS = [
    (re.compile(r'(\d)\s*\.\s*(\d{3})'), r'\1.\2'),
    (re.compile(r'(\d)\s*,\s*(\d{2}€?)'), r'\1,\2'),
    (re.compile(r'\*\*==> picture.*?<==\*\*', re.DOTALL), ''),
    (re.compile(r'<!--\s*(?:image|figure|picture|PageBreak|PageHeader|PageFooter|FigureCaption)[^>]*-->', re.IGNORECASE), ''),
    (re.compile(r'!\[[^\]]*\]\([^)]*\)'), ''),
    (re.compile(r'\$\$[^$]*\$\$', re.DOTALL), ''),
    (re.compile(r'\b\d{1,3}/\d{1,3}\s*\n'), ''),
    (re.compile(r'Os Fundos Europeus[^\n]*\n'), ''),
    (re.compile(r'^Página \d+ de \d+\s*$', re.MULTILINE), ''),
    (re.compile(r'^\d+/\d+[\.,]\d+\s+de\s+\d{2}/\d{2}/\d{4}\s*$', re.MULTILINE), ''),
    (re.compile(r'<!--\s*formula-not-decoded\s*-->', re.IGNORECASE), ''),
]

_TABLE_REPAIR = re.compile(r'(\|[^\n]+)\n{2,}(\|)', re.MULTILINE)


def _clean_ocr(text: str) -> str:
    text = _normalize_monetary(text)
    for pattern, repl in _OCR_PATTERNS:
        text = pattern.sub(repl, text)
    text = re.sub(r'(?<!\n) {2,}(?!\n)', ' ', text)
    prev = None
    while prev != text:
        prev = text
        text = _TABLE_REPAIR.sub(r'\1\n\2', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _normalize_monetary(text: str) -> str:
    text = _NM_PATTERNS[0].sub(r'\1.\2', text)
    for _ in range(3):
        text = _NM_PATTERNS[1].sub(r'\1.\2', text)
        text = _NM_PATTERNS[2].sub(r'\1.\2', text)
        text = _NM_PATTERNS[3].sub(r'\1.\2', text)
    return text