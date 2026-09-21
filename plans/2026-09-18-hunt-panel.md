# hunt-panel — `hx ui --serve` (read-only local panel)

Aprovado pelo dj em 2026-09-18: abordagem A (stdlib + meta refresh), todas as seções.

## Objetivo

Painel local, só leitura, do estado de um engagement: fila de hipóteses,
cobertura endpoint×classe, findings verificados, runs/sessões recentes.
Uso: olhar o hunt de lado durante runner/slices. Sem mutations pelo painel.

## Comando

```
hx ui --serve [--port 8137] [--dir ENG] [--interval 15] [--show-findings]
```

- Foreground; Ctrl-C encerra; imprime a URL (`http://127.0.0.1:PORT/`).
- Resolução do engagement igual aos demais comandos (`--dir` ou cwd;
  exige `hunt/scope.json`; mesmos exit codes de guard).
- Servidor: `http.server` stdlib dentro de `bin/hx` (`cmd_ui`).
  Apenas `GET /` ( resto → 404). Bind fixo em `127.0.0.1`, sem flag
  para `0.0.0.0`. Nenhuma rota de mutação, nenhum query param com efeito.
- `--interval` com piso: `interval = max(interval, 3)` (evita `--interval 0`
  virando loop de refresh).
- Findings **só** com `--show-findings` (default off).
- Modelo de ameaça: o bind loopback protege da rede, **não** de outro
  processo/usuário no mesmo host — qualquer coisa local pode
  `curl 127.0.0.1:PORT` e ler o que estiver sendo servido. Por isso o dado
  mais sensível (findings, que sobrevive ao engagement e vira report) é
  opt-in explícito, e a página nunca renderiza corpos de evidência nem
  blocos de proof marker. Auth segue fora de escopo por decisão consciente.

## Dados → seções (render server-side a cada refresh)

Leitura direta do disco; função pura `render_ui(repo) -> str`.

- **Topo:** engagement (`scope.json → engagement`), contadores
  open/claimed/running/result(+dropped), timestamp da leitura.
- **Fila** (`hunt/HYPOTHESES.json`, agrupada por status):
  open → claim, endpoint, classe, confirm/refute;
  claimed/running → owner + idade do claim (destaque se passou do TTL);
  result → verdict. Dropped fora da lista, só na contagem.
- **Cobertura** (`hunt/COVERAGE.md`, matriz endpoint×classe): tabela com
  chips confirmed/refuted/blocked/untested. Parser tolerante: linha
  ilegível não quebra a página (seção mostra "cobertura ilegível").
- **Findings** (`hunt/FINDINGS/<id>.json`, só com `--show-findings`):
  só verificados (cenário PASS ou attestation assinada); renderiza id,
  verdict, note e caminho da evidência como texto — nunca corpos de
  evidência nem blocos de proof marker.
- **Runs/sessões** (`hunt/runs.jsonl` + `hunt/sessions/`): últimas 10 runs
  (hora, hipótese, verdict, tokens/custo) + nomes dos últimos sessions
  (só nomes, sem corpo).
- Arquivo ausente ou vazio → seção mostra "sem dados", nunca 500.

## Página e estilo

- HTML único; `<meta http-equiv="refresh" content="{interval}">`.
- Tokens Telemetry inline (`:root` dark: background/foreground/card/
  primary/muted/border/ring, destructive/warning/info, `--radius`,
  base `Space Mono`) + CSS próprio curto sobre os tokens (cabeçalho,
  chips, tabela, seções). Sem Tailwind, sem JS.
- Todo dado do engagement passa por `html.escape`. Nenhum corpo bruto
  de scan é renderizado (só resumos já redigidos na captura).

## Testes (`tests/test_hx.py`)

Fixture de engagement em tmp (`init` + `hypothesis add` + `result`):

- `render_ui` contém contadores e seções esperados; claim com
  `<script>` sai escapado; cobertura ilegível não quebra.
- Smoke do handler: `GET /` → 200 numa porta efêmera; `GET /x` → 404.

## Arquivos tocados

- `bin/hx` (`cmd_ui`, `render_ui`, handler) e `tests/test_hx.py`. Mais nada.

## Fora de escopo

Poll via JS, ações pelo painel (claim/release), export estático,
autenticação, bind externo, servir arquivos do engagement.
