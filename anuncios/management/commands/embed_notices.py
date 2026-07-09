"""
Pré-calcula (em lote) os embeddings dos anúncios, para a pesquisa semântica.

Idempotente: só (re)calcula os que faltam ou cujo texto mudou; `--all` força tudo.

Uso:
    python manage.py embed_notices
    python manage.py embed_notices --all
"""

from django.core.management.base import BaseCommand

from anuncios.models import Notice
from anuncios.embeddings import notice_embedding_text
from match import embeddings as emb


class Command(BaseCommand):
    help = "Calcula/atualiza os embeddings dos anúncios (cache pgvector)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="Recalcula todos, ignorando a cache existente.")
        parser.add_argument("--batch", type=int, default=100)

    def handle(self, *args, **options):
        notices = list(Notice.objects.all())
        pending = []
        for n in notices:
            text = notice_embedding_text(n)
            h = emb.text_hash(text)
            if options["all"] or n.activity_embedding is None or n.activity_embedding_hash != h:
                pending.append((n, text, h))

        self.stdout.write(f"{len(notices)} anúncios; {len(pending)} a (re)calcular...")
        if not pending:
            self.stdout.write(self.style.SUCCESS("Cache já atualizada — nada a fazer."))
            return

        batch, done = options["batch"], 0
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            vecs = emb.embed_many([t for _, t, _ in chunk])
            updated = []
            for (n, _, h), vec in zip(chunk, vecs):
                if vec is not None:
                    n.activity_embedding = vec
                    n.activity_embedding_hash = h
                    updated.append(n)
            if updated:
                Notice.objects.bulk_update(updated, ["activity_embedding", "activity_embedding_hash"])
                done += len(updated)
            self.stdout.write(f"  {done}/{len(pending)}...", ending="\r")

        if done == 0:
            self.stdout.write(self.style.WARNING(
                "\n0 atualizados — OPENAI_API_KEY em falta?"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nFeito — {done} embeddings atualizados."))
