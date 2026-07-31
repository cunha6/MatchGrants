// Renderiza UM template React Email (.tsx) para HTML estático, impresso em stdout.
// Uso: node render.mjs <caminho-do-tsx> [caminho-de-props.json]
//
// Chamado pelo management command `python manage.py render_emails` (match/management/
// commands/render_emails.py) — esse comando trata da parte que este script NÃO faz:
// embutir as imagens locais (/static/*.png) em base64 e escrever o HTML final no template
// Django. Este script só sabe transformar JSX em HTML, nada de Django/Python.
//
// O 3º argumento (opcional) é um ficheiro JSON com as props a passar ao componente — usado
// para gerar um preview/envio com dados REAIS (ex: notices da BD) em vez do exemplo por
// omissão (`PreviewProps`), mantendo o resultado pixel-a-pixel igual ao .tsx.
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "url";
import path from "path";
import fs from "fs";
import { render } from "@react-email/render";
import React from "react";

const srcPath = process.argv[2];
const propsPath = process.argv[3];
if (!srcPath) {
  console.error("Uso: node render.mjs <caminho-do-tsx> [caminho-de-props.json]");
  process.exit(1);
}
const props = propsPath ? JSON.parse(fs.readFileSync(propsPath, "utf-8")) : {};

// O ficheiro temporário do build tem de viver DENTRO de emails/ (não em os.tmpdir()) — a
// resolução de módulos ESM do Node sobe a árvore de diretórios a partir de onde o ficheiro
// está para encontrar node_modules, e emails/node_modules só está acima de emails/*.
const emailsDir = path.dirname(fileURLToPath(import.meta.url));
const outJs = path.join(emailsDir, `.render-tmp-${Date.now()}.mjs`);

await esbuild.build({
  entryPoints: [srcPath],
  bundle: true,
  format: "esm",
  jsx: "automatic",
  platform: "node",
  outfile: outJs,
  external: ["react", "react-dom", "@react-email/components"],
});

try {
  const mod = await import(pathToFileURL(outJs).href);
  const Component = mod.default;
  const html = await render(React.createElement(Component, props), { pretty: true });
  process.stdout.write(html);
} finally {
  fs.unlinkSync(outJs);
}
