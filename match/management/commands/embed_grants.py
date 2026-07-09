"""
Pré-calcula (em lote) os embeddings de atividade dos avisos, para a procura semântica.

Sem isto, o primeiro match calcula os embeddings em falta um a um (mais lento). Aqui
fazem-se em lote (poucas chamadas). Idempotente: só (re)calcula os que faltam ou cujo
texto mudou; `--all` força tudo.

Uso:
    python manage.py embed_grants
    python manage.py embed_grants --all
"""

from django.core.management.base import BaseCommand

from avisos.models import Grant
from match import embeddings


class Command(BaseCommand):
    help = "Calcula/atualiza os embeddings de atividade dos avisos processados (cache)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="Recalcula todos, ignorando a cache existente.")
        parser.add_argument("--batch", type=int, default=100,
                            help="Nº de avisos por chamada de embeddings (default: 100).")

    def handle(self, *args, **options):
        grants = list(Grant.objects.filter(ai_processed=True))
        pending = []
        for g in grants:
            text = embeddings.grant_embedding_text(g)
            h = embeddings.text_hash(text)
            if options["all"] or g.activity_embedding is None or g.activity_embedding_hash != h:
                pending.append((g, text, h))

        self.stdout.write(f"{len(grants)} avisos processados; {len(pending)} a (re)calcular...")
        if not pending:
            self.stdout.write(self.style.SUCCESS("Cache já atualizada — nada a fazer."))
            return

        batch, done = options["batch"], 0
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            vecs = embeddings.embed_many([t for _, t, _ in chunk])
            updated = []
            for (g, _, h), vec in zip(chunk, vecs):
                if vec is not None:
                    g.activity_embedding = vec
                    g.activity_embedding_hash = h
                    updated.append(g)
            if updated:
                Grant.objects.bulk_update(updated, ["activity_embedding", "activity_embedding_hash"])
                done += len(updated)
            self.stdout.write(f"  {done}/{len(pending)}...", ending="\r")

        if done == 0:
            self.stdout.write(self.style.WARNING(
                "\n0 atualizados — OPENAI_API_KEY em falta? (o match cai para taxa+dotação sem semântica)"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nFeito — {done} embeddings atualizados."))
