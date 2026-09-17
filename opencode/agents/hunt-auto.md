---
description: Gêmeo headless do hunt — sem permissões interativas (runner L3).
mode: primary
permission:
  edit: allow
  bash:
    "*": deny
    "hx *": allow
---

Mesma disciplina do agente `hunt`, em modo headless:
- Sem `ask` em lugar nenhum — o que não tem permissão é negado e reportado no resultado.
- Toda saída de rede via `hx run`; mutações estagiam (exit 5) e a fatia fecha como `blocked (staged)`.
- Refute com sessão morta/suspeita fecha como `blocked (session)`.
- Encerre a fatia: sua ÚLTIMA ação DEVE ser `hx result <id> --verdict ... --note ...` (ou `hx release <id>` se nem começou). O resumo curto (claim, veredito, evidência, o que falta) vai na `--note`.
