"""
Gera HTML estático a partir dos templates React Email (.tsx) em emails/ — o welcome.tsx (e
afins) é a fonte de verdade; este comando é o único sítio onde esse .tsx é lido. O Django em
runtime nunca precisa de Node.js: só lê o HTML já gerado (match/notifications.py).

Passos, por template:
  1. `node emails/render.mjs <tsx>` — JSX -> HTML (via Node; PRECISA de `npm install` feito
     dentro de emails/ antes da primeira vez).
  2. As imagens locais (<img src="/static/X.png" height="H">) são embutidas em base64 —
     redimensionadas a 2x a altura declarada (retina), via Pillow (já é dependência do
     projeto, sem precisar de mais nada do lado do Node).
  3. Escreve o resultado em emails/html/<nome>_email.html.

Corre-se manualmente sempre que um .tsx em emails/ mudar — nunca automaticamente, nunca em
produção.

Uso:
    cd emails && npm install   # só da 1ª vez, ou quando as dependências mudarem
    python manage.py render_emails                 # todos os .tsx em emails/
    python manage.py render_emails welcome          # só emails/welcome.tsx
"""

import base64
import io
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image

_EMAILS_DIR = Path(settings.BASE_DIR) / "emails"
_STATIC_DIR = _EMAILS_DIR / "static"
_OUT_DIR = _EMAILS_DIR / "html"

# Fator de escala aplicado à altura declarada no JSX (retina — o dobro fica nítido nos
# ecrãs de alta densidade sem pesar demasiado o HTML final).
_RETINA_SCALE = 2

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(r'src="(/static/[^"]+\.png)"')
_HEIGHT_RE = re.compile(r'height="(\d+)"')


class Command(BaseCommand):
    help = "Renderiza os templates React Email (.tsx) de emails/ para HTML estático Django."

    def add_arguments(self, parser):
        parser.add_argument(
            "names", nargs="*",
            help="Nomes dos templates a renderizar (sem .tsx), ex: welcome. Omitido = todos.",
        )

    def handle(self, *args, **options):
        node = shutil.which("node")
        if not node:
            raise CommandError(
                "Node.js não encontrado no PATH — este comando só corre num ambiente de "
                "desenvolvimento com Node instalado (nunca em produção)."
            )
        if not (_EMAILS_DIR / "node_modules").exists():
            raise CommandError(
                f"Faltam as dependências Node — corre `npm install` dentro de {_EMAILS_DIR} "
                "antes de usar este comando."
            )

        names = options["names"] or [p.stem for p in _EMAILS_DIR.glob("*.tsx")]
        if not names:
            self.stdout.write(self.style.WARNING("Nenhum .tsx encontrado em emails/."))
            return

        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        image_cache: dict[tuple[str, int], str] = {}

        for name in names:
            tsx_path = _EMAILS_DIR / f"{name}.tsx"
            if not tsx_path.exists():
                raise CommandError(f"Não existe {tsx_path}.")
            html = self._render_tsx(node, tsx_path)
            html = self._inline_local_images(html, image_cache)
            out_path = _OUT_DIR / f"{name}_email.html"
            out_path.write_text(html, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(
                f"{tsx_path.relative_to(settings.BASE_DIR)} -> "
                f"{out_path.relative_to(settings.BASE_DIR)} ({len(html)} chars)"
            ))

    def _render_tsx(self, node: str, tsx_path: Path) -> str:
        result = subprocess.run(
            [node, "render.mjs", str(tsx_path)],
            cwd=_EMAILS_DIR, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise CommandError(f"Falha a renderizar {tsx_path.name}:\n{result.stderr}")
        return result.stdout

    def _inline_local_images(self, html: str, cache: dict[tuple[str, int], str]) -> str:
        """Substitui cada <img src="/static/X.png" height="H" .../> local por um data-URI
        base64, redimensionado a _RETINA_SCALE * H (cacheado por (ficheiro, altura) — o
        mesmo ícone repetido no template só é lido/redimensionado uma vez)."""
        def replace_tag(match: re.Match) -> str:
            tag = match.group(0)
            src_match = _SRC_RE.search(tag)
            if not src_match:
                return tag  # não é uma imagem local (ex: o ícone do LinkedIn, já é URL absoluto)
            height_match = _HEIGHT_RE.search(tag)
            height = int(height_match.group(1)) if height_match else 40
            rel_path = src_match.group(1).removeprefix("/static/")
            data_uri = self._image_data_uri(rel_path, height * _RETINA_SCALE, cache)
            return tag.replace(src_match.group(0), f'src="{data_uri}"')

        return _IMG_TAG_RE.sub(replace_tag, html)

    def _image_data_uri(self, rel_path: str, target_height: int,
                        cache: dict[tuple[str, int], str]) -> str:
        key = (rel_path, target_height)
        if key in cache:
            return cache[key]
        src_path = _STATIC_DIR / rel_path
        if not src_path.exists():
            raise CommandError(f"Imagem referida no template não existe: {src_path}")
        with Image.open(src_path) as im:
            im = im.convert("RGBA")
            ratio = target_height / im.height
            resized = im.resize((round(im.width * ratio), target_height), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode()
        data_uri = f"data:image/png;base64,{b64}"
        cache[key] = data_uri
        return data_uri
