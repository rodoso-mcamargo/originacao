# Ações de rating das agências (Moody's Local, S&P, Fitch)

Coletor: `ratings_agencias.py`. Workflow: `.github/workflows/ratings-agencias-diaria.yml`
(seg-sex às 13:15 e 23:00 UTC; `workflow_dispatch` com `inspecionar` = sim/não e `desde` =
AAAA-MM-DD para backfill de até 12 meses).

Repositório público: de cada comunicado guardamos só título, data, nota, perspectiva e link,
nunca o texto. Triagem com fonte pública, não é recomendação.

## Fontes e situação

| Agência | URL lida | Situação (out/2026) |
|---|---|---|
| Moody's Local Brasil | `moodyslocal.com.br/reporte/rating-action/feed/` (RSS), depois `/wp-json/wp/v2/`, depois o HTML | **Bloqueado.** As três URLs respondem HTTP 403 com desafio anti-robô do Cloudflare ("Just a moment..."). Não contornamos. O script tenta a cada run e registra o bloqueio em `ultimo.json`; se o feed abrir, o parser de RSS (testado) entra sozinho, com paginação `?paged=N` para o backfill. |
| S&P Global Ratings Brasil | `brazil.ratings.spglobal.com/ratings/pt/regulatory/ratings-actions` e `/pt` | **Parcial.** A lista de ações vem de uma API que exige token Bearer; esse token não é reaproveitado. Lemos só o que o servidor já entrega no HTML (`__NEXT_DATA__`): a janela de 1 dia da página de ações (`ratingAction`, vazia nos testes) e os artigos em destaque da home (em geral comentários setoriais → `relatorio`). Sem backfill. |
| Fitch | `www.fitchratings.com/sitemap-research.xml` (listado no robots.txt) | **Parcial.** Os comunicados são montados no navegador e `/page-data/` é vedado pelo robots.txt, então só a URL é lida. O sitemap traz os ~500 research mais recentes do mundo todo (3 a 4 dias). Entram as URLs `/research/pt/`, as com `-bra-` no slug e as internacionais cuja entidade casa **exatamente** com o painel. Sem backfill além desses dias. |
| Austin Rating | `austin.com.br` | Não coletado: sem feed/wp-json (404), só HTML em IIS. |
| Liberum Ratings | `liberumratings.com.br` | Não coletado: domínio não resolve. |

Requisições: ≤ 1 por segundo, User-Agent `RadarSecundario-coletor/1.0 (+repo)`, IPv4.

## Arquivos

- `AAAA-MM-DD.json`: todas as ações com data de publicação naquele dia (`{data, n, acoes}`).
  Ação sem data na fonte vai para o arquivo do dia da coleta, com `data: null`.
- `base.csv`: base acumulada, chave **agencia + url**; `coletado_em` é a primeira vez que vimos.
- `universo.csv`: só ações casadas com o painel. Colunas: `agencia, data, entidade, acao,
  nota_anterior, nota_nova, perspectiva, codigos` (códigos do painel separados por `;`),
  `emissor_painel, tipo_match` (`exato`, `fuzzy` ou `grupo:<controladora>`), `score, url`.
- `grupos.csv`: tabela editável `controladora;subsidiaria` (começa vazia).
- `ultimo.json`: último run. `contagens` (por agência, por ação, base) e `diagnostico.agencias.<ag>`
  com `status` (ok, vazio, bloqueado, erro), `fonte_usada`, `tentativas` (URL, HTTP, bloqueio),
  `lidos`, `validos`, `novos_na_base`, `descartados` (por motivo) e `erros`.

## Colunas da ação

| Coluna | Conteúdo |
|---|---|
| agencia | `moodys_local`, `sp`, `fitch` |
| data | data da publicação (ISO), da fonte; sem data → null |
| entidade | nome como aparece na fonte (Fitch: tirado do slug, em maiúsculas) |
| escala | `nacional` / `internacional` / null |
| acao | rebaixamento, elevacao, perspectiva_negativa, perspectiva_positiva, em_revisao, afirmacao, atribuicao, retirada, relatorio |
| nota_anterior, nota_nova | notas como escritas (AA.br, brAA+, AA(bra)). **Fitch: sempre null**, porque o slug perde o +/- |
| perspectiva | estavel, positiva, negativa, em_desenvolvimento, em_revisao_negativa, em_revisao_positiva |
| instrumento | debentures, cri, cra, fidc, notas_comerciais, letras_financeiras, bonds ou null |
| titulo | título do comunicado. **Fitch: o slug da URL em texto** (em inglês, sem acentos) |
| url | link do comunicado |

## Classificação

Regex sobre título + primeiro parágrafo, lista `CLASSIFICADOR` no topo do script; a primeira
regra que casa vence: em_revisao (CreditWatch, Rating Watch, "em revisão para rebaixamento") →
rebaixamento → elevacao → perspectiva_negativa → perspectiva_positiva → retirada → atribuicao →
afirmacao. Nada casou: `relatorio`.

## Casamento com o painel

Universo: `dados/ultimo.json` (`papeis[].emissor`) e `dados/cri_cra/ultimo.json` (`ativos[].devedor`
ou `emissor`, que ali é o risco de crédito). Nomes normalizados: sem acento, maiúsculas, sem
pontuação, sem sufixos (S A, SA, LTDA, CIA, COMPANHIA, PARTICIPACOES, EM RECUPERACAO JUDICIAL).
Exato primeiro; depois fuzzy (`rapidfuzz.token_set_ratio` ≥ 92), marcado como `fuzzy`. Nunca casa
por palavra genérica (ENERGIA, SANEAMENTO, TRANSMISSORA, BRASIL...: lista `GENERICAS`) nem quando
a única diferença é um país ("TELEFONICA SA" não casa com "TELEFONICA BRASIL").

## Grupo econômico (`grupos.csv`)

Separador `;`. Quando a agência fala da controladora, as subsidiárias da tabela que estão no
painel entram em `universo.csv` com `tipo_match = grupo:<controladora>`. O script não infere
controle sozinho: só o que estiver escrito aqui. Exemplos do formato (não estão na tabela;
confira o controle antes de copiar):

```
controladora;subsidiaria
Equatorial S.A.;Equatorial Pará Distribuidora de Energia S.A.
Energisa S.A.;Energisa Mato Grosso - Distribuidora de Energia S.A.
Aegea Saneamento e Participações S.A.;Águas do Rio 4 SPE S.A.
Rumo S.A.;Rumo Malha Norte S.A.
CPFL Energia S.A.;Companhia Paulista de Força e Luz
```

## Ressalvas de uso

- Cobertura incompleta: Moody's Local bloqueada, S&P só o HTML do dia, Fitch só a URL dos últimos
  3 a 4 dias. Falta de ação aqui **não** quer dizer que não houve ação.
- Notas e entidade saem de texto livre; confira no link antes de usar.
- Os comunicados são propriedade das agências; aqui só o link e metadados.
