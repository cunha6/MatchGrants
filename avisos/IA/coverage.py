"""
Harness de cobertura: mede quantas secções de um markdown de aviso chegam a um prompt
de extração (vs descartadas) e mostra a distribuição por categoria/prompt.

Uso:
    python -m avisos.IA.coverage output/markdown/portugal/<ficheiro>.md
    python -m avisos.IA.coverage            # corre sobre todos os .md de output/markdown
"""

import sys
from collections import Counter
from pathlib import Path

from .chunker import chunk_by_markdown, _parse_markdown_sections, map_category, CATEGORIA_PARA_PROMPTS


def coverage_report(markdown: str, source: str = "doc") -> dict:
    """Relatório de cobertura de um markdown: secções totais, descartadas e por categoria."""
    sections = [(k, t) for k, t in _parse_markdown_sections(markdown) if len(t) >= 30]
    chunks = chunk_by_markdown(markdown, source, source)

    dropped: list[str] = []
    for key, text in sections:
        if map_category(" > ".join(key), text) == "ignorar":
            dropped.append(key[-1][:70])

    total = len(sections)
    covered = total - len(dropped)
    by_cat = Counter(c["category"] for c in chunks)
    prompts_hit = sorted({
        p for c in chunks
        for p in CATEGORIA_PARA_PROMPTS.get(c["category"], "").split(",") if p
    })

    return {
        "source": source,
        "total_sections": total,
        "covered_sections": covered,
        "dropped_sections": dropped,
        "coverage_pct": round(covered / total * 100, 1) if total else 0.0,
        "chunks": len(chunks),
        "by_category": dict(sorted(by_cat.items())),
        "prompts_hit": prompts_hit,
    }


def _print(report: dict) -> None:
    print(f"\n=== {report['source']} ===")
    print(f"  Secções: {report['total_sections']} | cobertas: {report['covered_sections']} "
          f"| cobertura: {report['coverage_pct']}%")
    print(f"  Chunks: {report['chunks']} | prompts atingidos: {', '.join(report['prompts_hit'])}")
    print(f"  Por categoria: {report['by_category']}")
    if report["dropped_sections"]:
        print(f"  Descartadas ({len(report['dropped_sections'])}): {report['dropped_sections']}")


def _main(argv: list[str]) -> None:
    if argv:
        paths = [Path(a) for a in argv]
    else:
        paths = sorted(Path("output/markdown").rglob("*.md"))

    if not paths:
        print("Nenhum markdown encontrado.")
        return

    pcts = []
    for p in paths:
        md = p.read_text(encoding="utf-8")
        report = coverage_report(md, p.stem)
        _print(report)
        pcts.append(report["coverage_pct"])

    if len(pcts) > 1:
        print(f"\nCobertura média: {round(sum(pcts)/len(pcts), 1)}% em {len(pcts)} documentos")


if __name__ == "__main__":
    _main(sys.argv[1:])
