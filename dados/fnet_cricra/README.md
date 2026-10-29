# dados/fnet_cricra — assembleias e documentos de CRI/CRA (Fundos.NET)

Coletor: `fnet_cricra.py` · workflow: `.github/workflows/fnet-cricra-diaria.yml`
(seg–sex 22:30 UTC, janela dos últimos 5 dias corridos; backfill inicial de 270 dias).

## Fonte

Pesquisa pública do Fundos.NET (B3), aba de certificados de securitização:

- página: `https://fnet.bmfbovespa.com.br/fnet/publico/abrirGerenciadorDocumentosCVM?paginaCertificados=true`
- JSON (o mesmo que a tabela da página chama):
  `https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados?paginaCertificados=true&idCategoriaDocumento=<id>&dataInicial=dd/mm/aaaa&dataFinal=dd/mm/aaaa&d=1&s=<offset>&l=200&o[0][dataEntrega]=desc`
- documento: `https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento?id=<id_documento>`

Sem login nem captcha (o captcha da página só aparece no "Anexo B", que não usamos).
Sem robots.txt (404). Pausa de 1,1 s entre requisições, User-Agent `originacao-radar/1.0`.
`l=1000` devolve resposta vazia; páginas de 200.

Categorias pedidas: Assembleia (2), Assembleia especial de investidores (46), Fato Relevante (1),
Comunicado ao Mercado (3), Aviso aos Investidores (20), Recuperação Judicial (35) e
Extrajudicial (34). Informes mensais ficam de fora. Só entram CRI e CRA (o Fundos.NET mistura
DEB, OTS, CR etc. na mesma aba; esses vão para `diagnostico.descartados`).

## Arquivos

| arquivo | conteúdo |
|---|---|
| `AAAA-MM-DD.json` | documentos de CRI/CRA entregues naquele dia (data de entrega da fonte) |
| `base.csv` | base acumulada, chave `id_documento` |
| `universo.csv` | documentos casados com `dados/cri_cra` (uma linha por documento × código) |
| `ultimo.json` | resumo da última execução + `diagnostico` (lidos, gravados, descartados, erros) |

### Colunas de `base.csv`

`id_documento`, `data_entrega`, `data_referencia` (ISO, da fonte), `securitizadora` (nome curto
do Fundos.NET), `tipo_titulo` (CRI/CRA), `emissao`, `serie` (primeira série), `series_extra`
(o "(+N)" da descrição), `devedor_fnet` (nome cortado em ~20 caracteres pela fonte),
`mes_emissao` (AAAA-MM da descrição), `isin` ou `codigo` (o último campo da descrição: ISIN ou
código B3), `categoria`, `tipo`, `especie`, `assunto`, `status` (ativo/cancelado/inativo),
`versao`, `url`, `match`, `codigos_universo`, `classe`, `texto`, `codigos_no_texto`, `descricao_fnet`.

`universo.csv`: `data_entrega`, `codigo_universo`, `tipo_titulo`, `devedor` (do painel),
`securitizadora`, `emissao`, `serie`, `categoria`, `tipo`, `especie`, `classe`, `match`,
`match_nome`, `ambiguo`, `assunto`, `status`, `url`, `id_documento`.

## Casamento com o painel (`match`)

O universo (`dados/cri_cra/ultimo.json`, ANBIMA) traz código B3, devedor, securitizadora e série,
mas **não traz ISIN nem número da emissão**. Ordem:

1. `isin` — se o universo tiver ISIN (hoje não tem; fica pronto para quando tiver).
2. `codigo` — quando o Fundos.NET mostra o código B3 no lugar do ISIN, ou quando o texto do
   documento cita o código (ex.: "IF CRA022008NB"; coluna `codigos_no_texto`). A citação no texto
   tem precedência sobre o casamento por nome e resolve a ambiguidade entre emissões.
3. `emissao_serie` — se o universo tiver emissão (hoje não tem).
4. `serie_devedor` — **valor novo, fora da lista original**: mesma securitizadora, série dentro
   do intervalo "Série(s): N (+k)", nome do devedor casado pela regra 11 (exato, prefixo — o
   Fundos.NET corta o nome — ou fuzzy `token_set_ratio ≥ 92` com palavra significativa em comum;
   nunca palavra genérica sozinha) **e** mês/ano de emissão compatível com o embutido no código
   (CRI `AAL…`: mesmo mês ±1; CRA `CRA0AA…`: mesmo ano). `match_nome` diz qual regra de nome.
5. `nenhum`.

`ambiguo = True` em `universo.csv` quando o mesmo código ficou ligado a mais de uma emissão do
Fundos.NET (típico em CRA de devedor recorrente, em que o código só dá o ano). Trate com cuidado.

## Classificação da pauta (`classe`)

A listagem do Fundos.NET **não traz assunto** (o campo `assuntos` vem nulo para certificados).
Para os documentos casados com o painel, o script baixa o documento em memória, extrai o texto
com `pypdf`, recorta a "Ordem do Dia" (assembleias) ou usa o início do texto (fato relevante,
comunicado) e aplica as regex do topo de `fnet_cricra.py`:

- `credito`: waiver, renúncia, perdão, (não) declaração/decretação de vencimento antecipado,
  evento de inadimplemento, covenant, índice financeiro, alteração/prorrogação de
  cronograma/prazo/vencimento/remuneração/juros/carência, repactuação, inadimplemento,
  recuperação judicial ou extrajudicial;
- `estrutural`: substituição de devedor/garantia/agente fiduciário/securitizadora/cedente,
  liberação/reforço/alteração de garantia, resgate, amortização extraordinária, mudança de
  controle;
- `sem_pauta`: nenhum dos dois (ex.: aprovação de demonstrações financeiras do patrimônio
  separado) ou sem texto extraível.

`credito` tem precedência. `assunto` guarda só até 300 caracteres da pauta. `texto` diz de onde
veio a classificação: `pauta`, `inicio`, `sem_texto` (PDF escaneado) ou `erro`. Documentos não
casados ficam com `classe` vazia (não são baixados). Nenhum PDF é gravado no repositório.

## Ressalvas

- Triagem automática por regex sobre texto extraído de PDF: confira o documento (`url`) antes de
  qualquer conclusão. Pautas de AGO trazem cláusulas-padrão; a classificação usa só a ordem do dia.
- Securitizadoras do painel sem documento no período (ex.: RIZA, TRUE) simplesmente não casam.
- Repositório público; fonte pública. Não é recomendação.
