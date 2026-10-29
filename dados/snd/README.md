# dados/snd — características e agenda das debêntures (SND)

Coletor: `snd.py` · workflow: `.github/workflows/snd-diaria.yml` (seg-sex 09:30 UTC).

## Fonte
SND, www.debentures.com.br (ANBIMA/B3). A própria página de consulta oferece exportação em
lote ("texto delimitado por Tab"), então o coletor faz 2 requisições por dia (≤ 1 req/s,
User-Agent identificável; o site não tem robots.txt):

- Características, todos os códigos:
  `https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/caracteristicas_e.asp?Ativo=`
- Agenda de eventos, de hoje a 2070:
  `https://www.debentures.com.br/exploreosnd/consultaadados/eventosfinanceiros/agenda_e.asp?emissor=&ativo=&evento=&dt_ini=DD/MM/AAAA&dt_fim=31/12/2070&dt_pgto_ini=&dt_pgto_fim=`

Como o lote traz todos os códigos, não há cache por código: código novo no universo entra no
mesmo dia. A página por código (`caracteristicas_d.asp?selecao=<CODIGO>`) não é usada.

O cabeçalho da exportação avisa que a consulta de características e documentos está migrando
para data.anbima.com.br. Se o lote sair do ar, `python snd.py --inspecionar` mostra o erro.

## Universo
CNPJs dos papéis de `dados/ultimo.json`. Entram todas as debêntures registradas no SND desses
CNPJs (para o muro de vencimentos do emissor) e os papéis do painel já excluídos do SND.
`no_universo` marca os códigos que estão no painel.

## Arquivos
| arquivo | conteúdo |
|---|---|
| `caracteristicas.csv` | um por código (chave `codigo`) |
| `agenda.csv` | eventos futuros por código (chave `codigo`+`data_evento`+`tipo`) |
| `vencimentos_emissor.csv` | principal a vencer por CNPJ |
| `AAAA-MM-DD.csv` | foto do dia de `vencimentos_emissor.csv` |
| `ultimo.json` | resumo, top 10 e `diagnostico` (lidos, gravados, descartados e motivo, erros) |

### caracteristicas.csv
`codigo, isin, emissor, cnpj_emissor, numero_emissao, serie, data_emissao, vencimento,
volume_emissao` (quantidade emitida × VN de emissão), `quantidade, valor_nominal` (VN de
emissão), `indexador` (campo "índice" do SND), `remuneracao` (texto montado com índice,
% multiplicador e taxa de juros do SND, ex. `DI + 1,6%`, `108% DI`, `IPCA + 10%`), `garantia`
(como a fonte diz), `incentivada` (Lei 12.431), `agente_fiduciario, coordenador_lider, situacao,
atualizado_em` (hora em que o SND gerou o arquivo). Extras da mesma fonte: `no_universo,
quantidade_mercado, valor_nominal_atual, data_vna, saldo_atual` (quantidade em mercado × VNA
atual), `percentual_indexador, taxa_juros, familia` (DI_SPREAD, DI_PCT, IPCA, PRE, IGPM...).

`vencimento` usa "Data de Saída / Novo Vencimento" quando existe.

### agenda.csv
`codigo, data_evento, tipo` (juros, amortizacao, vencimento, resgate; premio, participacao e
outro quando a fonte traz), `percentual_ou_valor` (taxa/percentual da fonte), `data_pagamento,
evento_fonte, base_fonte` (ex. `VNA VAR/UNIF`), `liquidacao`. Só eventos de hoje em diante.

### vencimentos_emissor.csv
`cnpj_emissor, emissor, no_universo, n_papeis, saldo_atual, venc_0_6m, venc_6_12m, venc_12_18m,
venc_18_24m, venc_0_24m, pct_saldo_0_24m, saldo_estimado, data_referencia`. Valores em R$, pela
data de pagamento; janelas fechadas à direita (0-6m = até hoje + 6 meses).

Como o principal é calculado, por código, a partir do saldo atual:
- `VNA VAR ...`: % sobre o saldo remanescente;
- `VNA FIX ...` / `VNE ...`: % sobre o VN de emissão × quantidade em mercado;
- vencimento e resgate total: todo o saldo remanescente; juros, prêmio e participação não entram.

`saldo_estimado=true` quando falta quantidade em mercado ou VNA na fonte (aí usa volume de
emissão) ou quando há amortização em % do VN de emissão em papel corrigido por índice de
preço (o valor sai sem a correção monetária acumulada). Quantidade em mercado zero (papel
resgatado) conta como saldo zero.

## Ressalvas
- Estimativa de fluxo de principal a partir da agenda pública, não substitui a escritura.
- Eventos de resgate parcial e amortização extraordinária futuros só aparecem quando agendados.
- Triagem com fontes públicas, não é recomendação.
