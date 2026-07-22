"""Resolução SEGURA de caminhos de ficheiros locais (PDFs descarregados) para servir por HTTP.

Os avisos guardam o PDF em `pdf_Avisos/…` (Grant.pdf_path) e os anúncios o caderno de encargos
em `pdf_Anuncios/…` (Notice.specifications_path). Para os expor num link, é preciso resolver o
caminho relativo em absoluto — mas garantindo que fica DENTRO da pasta permitida (bloqueia
path traversal com `../`), senão um valor manipulado podia servir qualquer ficheiro do disco.
"""

import os

from django.conf import settings


def safe_media_path(rel_path, allowed_subdir: str) -> str | None:
    """Caminho absoluto de `rel_path` (relativo ao BASE_DIR) SE o ficheiro existir E estiver
    dentro de BASE_DIR/`allowed_subdir`; caso contrário None. Bloqueia path traversal."""
    if not rel_path:
        return None
    base = os.path.realpath(os.path.join(settings.BASE_DIR, allowed_subdir))
    full = os.path.realpath(os.path.join(settings.BASE_DIR, str(rel_path)))
    if full != base and not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None
