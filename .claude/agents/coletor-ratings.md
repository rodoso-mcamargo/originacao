---
name: coletor-ratings
description: Constrói o coletor diário de ações de rating publicadas por Moody's Local, S&P Brasil e Fitch, datadas e casadas com emissores e devedores do painel.
tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch
---
Leia docs/regras-coletores.md e siga à risca. Branch: coleta/ratings-agencias.
Script ratings_agencias.py; workflow ratings-agencias-diaria.yml, cron 15 13 * * 1-5 e
0 23 * * 1-5; workflow_dispatch com `desde` (AAAA-MM-DD) para backfill de até 12 meses.

Fontes, nesta ordem (prefira feed/JSON):
1. Moody's Local Brasil (moodyslocal.com.br/reporte/rating-action/); teste /feed/ e
   /wp-json/wp/v2/ antes de HTML.
2. S&P Global Ratings Brasil (brazil.ratings.spglobal.com): ações recentes de escala nacional.
3. Fitch (fitchratings.com, comunicados em português). Se bloquear robô ou pedir login, não
   contorne: README e siga.
4. Opcional se aberto: Austin Rating, Liberum Ratings.

Campos: agencia, data (da publicação; sem data → null), entidade, escala, acao
(rebaixamento, elevacao, perspectiva_negativa, perspectiva_positiva, em_revisao, afirmacao,
atribuicao, retirada, relatorio), nota_anterior, nota_nova, perspectiva, instrumento,
titulo, url. Classificador por regex sobre título + primeiro parágrafo, em lista no topo do
script e testado ("rebaixa"/"downgrade" → rebaixamento; "CreditWatch negativo"/"em revisão
para rebaixamento" → em_revisao). Na dúvida: relatorio.

Casamento com o universo pela regra 11. Grupo econômico: tabela editável
dados/ratings_agencias/grupos.csv (controladora; subsidiaria), começando vazia com 5
exemplos no README. Não infira controle sozinho.

Saída: AAAA-MM-DD.json (todas as ações do dia), base.csv (chave agencia+url), universo.csv
(só casadas: codigo(s), emissor_painel, tipo_match, score), ultimo.json com diagnóstico
por agência.

Entrega: PR com run de inspeção, backfill de 30 dias e run real; no PR, contagem por agência
e por ação e 10 linhas de universo.csv.
