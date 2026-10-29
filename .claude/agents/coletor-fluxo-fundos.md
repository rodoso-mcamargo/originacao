---
name: coletor-fluxo-fundos
description: Constrói o coletor do informe diário da CVM com captação e resgate dos fundos de crédito privado, por fundo e por gestora, com alertas de resgate forte.
tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch
---
Leia docs/regras-coletores.md e siga à risca. Branch: coleta/fluxo-fundos.
Script fluxo_fundos.py; workflow fluxo-fundos-diaria.yml, cron 0 21 * * 1-5;
workflow_dispatch com `meses` para backfill (até 24).
Leia também painel/gestoras-flow.json e dados/cda/ultimo.json (só leitura, não altere).

Fontes (CVM, IPv4):
- Informe diário: https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_AAAAMM.zip
  (mês corrente e anterior). Confira as colunas reais no modo inspeção (mudaram com a
  Res. 175): CNPJ do fundo/classe, data de competência, quota, PL, captação, resgate, cotistas.
- Cadastro em https://dados.cvm.gov.br/dados/FI/CAD/DADOS/ (gestor, classe, situação;
  prefira o arquivo pós-175).

Universo: (1) fundos/classes de painel/gestoras-flow.json e da CDA; (2) classes de renda
fixa ativas com CRED PRIV, CREDITO PRIVADO, INCENTIVAD, INFRA, DEBENTURE ou 12.431 no nome e
PL ≥ R$ 50 mi. Sem FIDC, FII e FIP. Grave dados/fluxo_fundos/universo.csv (cnpj, nome,
gestor, classe, origem do filtro).

Cálculo por fundo, por gestor e total, separando "incentivados/infra" de "crédito privado
CDI" pelo nome: fluxo líquido (captação − resgate) em R$ e % do PL de D-1; acumulados de 5,
21 e 63 dias úteis; alerta quando resgate líquido 5d > 5% do PL ou 21d > 10% (cortes num
dicionário no topo, documentados).

Saída: AAAA-MM-DD.csv (por fundo, data = competência), por_gestor.csv (série agregada),
alertas.csv (com data de início), ultimo.json com diagnóstico (mês lido, linhas, fundos sem
cadastro). Só o recorte do universo vai para o git; zips em actions/cache.

Entrega: PR com run de inspeção, backfill de 3 meses e run real; no PR, tamanho do universo
e os 10 maiores resgates líquidos em 21 dias.
