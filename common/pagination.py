"""Paginação de listagens — fonte única (evita reimplementar em cada view).

Lê `?page` e `?page_size` da query string (com defaults e limite), usa o Paginator do
Django sobre um queryset/lista já ordenado, e devolve o envelope uniforme
`{total, page, page_size, num_pages, items_key: [...]}` usado por todas as listagens
paginadas (avisos, anúncios, utilizadores).
"""

from django.core.paginator import Paginator

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def parse_pagination(request, default_size: int = DEFAULT_PAGE_SIZE, max_size: int = MAX_PAGE_SIZE):
    """(page, page_size) a partir de `?page`/`?page_size`, sempre válidos (>=1, <=max_size)."""
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.GET.get("page_size", default_size))
    except (TypeError, ValueError):
        page_size = default_size
    page_size = max(1, min(page_size, max_size))
    return page, page_size


def paginate_queryset(queryset, page: int, page_size: int, serialize, items_key: str) -> dict:
    """Pagina `queryset`/lista (já ordenado/filtrado) e serializa a página com `serialize`.

    Devolve {total, page, page_size, num_pages, <items_key>: [serialize(item), ...]}.
    Sem dependência do request — usável tanto nas views como na camada de serviço
    (ex: users/service.py, que recebe page/page_size já parseados pela view).
    """
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(page)
    return {
        "total": paginator.count,
        "page": page_obj.number,
        "page_size": page_size,
        "num_pages": paginator.num_pages,
        items_key: [serialize(item) for item in page_obj],
    }


def paginate(request, queryset, serialize, items_key: str, **kwargs) -> dict:
    """Conveniência para views simples: lê a paginação do `request` e pagina de uma vez."""
    page, page_size = parse_pagination(request, **kwargs)
    return paginate_queryset(queryset, page, page_size, serialize, items_key)
