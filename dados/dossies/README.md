# Dossiês de crédito do Radar do Secundário

Bases mantidas pela tarefa agendada "Dossiês de crédito — Radar" (cópia do projeto "Originação" no Claude).

- `dossies.json`: um dossiê por emissor/devedor (chave normalizada) ou `RAIZ:XXXX` para debênture ilíquida.
- `iliquidas.json`: nome do emissor por raiz de código e sinal de estresse (`est`).
- `descartados.json`: nomes e grupos econômicos fora das triagens.

Regra da rotina: antes de pesquisar, ler estes arquivos e mesclar com o projeto (fica o dossiê com
`pesquisado_em` mais recente de cada chave); depois de pesquisar, gravar aqui de novo.
Triagem com fontes públicas, não é análise nem recomendação de crédito.
