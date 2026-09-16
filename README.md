# huntx

Harness de caça a vulnerabilidades para sessões com LLM: **o estado da caça vive fora do modelo**.

Sessões de LLM são amnésicas, otimistas demais e perigosas com a rede solta. O huntx é a bancada em volta delas: memória durável, work orders, freio de escopo e verificação mecânica de achados.

## Componentes

| peça | o que faz |
|---|---|
| `bin/hx` | CLI de arquivo único (Python + curl_cffi): fila de hipóteses com claim/TTL, coverage por endpoint×classe, `brief` limitado por bytes, rate adaptativo (step-up em 429/WAF, decaimento entre runs), guard de escopo + staging de mutações (exit 5; execução só pelo humano em TTY), `run` com evidência redigida na captura + `raw_sha256`, health de sessões (strikes, janela de morte, reabertura retroativa), `verify` diferencial + assinatura manual (TTY), proxy CONNECT com allowlist e rate compartilhado |
| `opencode/` | camada de integração: agente `hunt` (prompt de work order + permissões), plugin (injeção do brief, env do proxy, tripwire com audit), commands `/brief` `/next` `/debrief`, `install.sh` |
| `templates/` | `scope.json` e `HUNT.md` para novos engagements |
| `specs/`, `plans/` | design (Rev 5) e plano de implementação (fase 1) |

## Conceitos

- **Work order = claim, não pergunta.** "Confirme ou refute isto" — nunca "procure problemas".
- **Evidência redigida na origem**: segredos de alta entropia viram `sha256:<12>`, de baixa entropia são dropados; PII é redigida; o marcador que É a prova fica num bloco isento.
- **Freio estrutural**: mutação fora de `allowed_mutations` estagia (exit 5) e só o humano executa, em TTY, com o host digitado.
- **Refute honesto**: refutação baseada em 401/403/redirect-login exige sessão viva; morte de sessão reabre resultados fechados na janela de morte.
- **Achado só existe verificado**: `FINDINGS/<id>.json` com cenário PASS ou atestação assinada — `result --verdict confirmed` rejeita o resto.

## Quickstart

```bash
# 1. instalar a camada opencode (symlinks em ~/.config/opencode + ~/.local/bin/hx)
bash install.sh
# adicionar ao array "plugin" do ~/.config/opencode/opencode.jsonc:
#   "file:///caminho/para/huntx/opencode/plugin/hunt.ts"
# reiniciar o opencode

# 2. engagement novo
mkdir -p ~/hunts/alvo && cd ~/hunts/alvo
hx init --dir .
# editar hunt/scope.json: hosts in/out, paths banidos, rate, probes de sessão
hx hypothesis add --claim "..." --endpoint "GET /api/x" --class BOLA --confirm "..." --refute "..."
hx brief     # digest da sessão
hx next      # work orders (claima)
```

## Testes

```bash
python3 tests/test_hx.py                   # 64 testes, stdlib
node --test tests/plugin_guard.test.mjs    # 8 testes do tripwire
```

## Postura de escopo

O huntx não é um sandbox. O tripwire denuncia e bloqueia os caminhos convenientes, o guard é a única saída de rede do tráfego scriptado e o proxy cobre clientes que honram `HTTP(S)_PROXY` — nada disso substitui autorização e as regras do programa. Use apenas em escopo autorizado.
