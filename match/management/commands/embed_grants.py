"""
Pré-calcula (em lote) os embeddings especializados dos avisos — um por tipo (GENERAL,
SECTOR, …) — para a procura semântica do match.

O match só LÊ embeddings; quem os escreve é o save do aviso (db_service) e este comando.
Corre-o depois de acrescentar/alterar um tipo de embedding ou para preencher avisos antigos.

Idempotente: só (re)calcula os tipos cujo texto mudou (ou que ainda não existem); `--all`
força tudo. Os textos pendentes de VÁRIOS avisos vão agrupados em poucas chamadas à API.

Uso:
    python manage.py embed_grants
    python manage.py embed_grants --all
"""

from django.core.management.base import BaseCommand

from avisos.models import Grant
from match import embeddings, grant_embeddings


class Command(BaseCommand):
    help = "Calcula/atualiza os embeddings (GENERAL, SECTOR…) dos avisos processados."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="Recalcula todos, ignorando os hashes existentes.")
        parser.add_argument("--batch", type=int, default=100,
                            help="Nº de textos por chamada de embeddings (default: 100).")

    def handle(self, *args, **options):
        grants = Grant.objects.filter(ai_processed=True).prefetch_related("embeddings")

        # (grant, tipo, texto, hash) de tudo o que falta — em todos os avisos e tipos.
        pending = [
            (grant, etype, text, h)
            for grant in grants
            for etype, text, h in grant_embeddings.pending_embeddings(grant, force=options["all"])
        ]

        self.stdout.write(
            f"{len(grants)} avisos processados; {len(pending)} embedding(s) a (re)calcular..."
        )
        if not pending:
            self.stdout.write(self.style.SUCCESS("Tudo atualizado — nada a fazer."))
            return

        batch, done = options["batch"], 0
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            vectors = embeddings.embed_many([text for _, _, text, _ in chunk])
            for (grant, etype, _, h), vec in zip(chunk, vectors):
                if vec is None:
                    continue
                grant_embeddings.store_embedding(grant, etype, vec, h)
                done += 1
            self.stdout.write(f"  {done}/{len(pending)}...", ending="\r")

        if done == 0:
            self.stdout.write(self.style.WARNING(
                "\n0 gravados — OPENAI_API_KEY em falta? (o match cai para taxa+dotação sem semântica)"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nFeito — {done} embedding(s) atualizados."))
