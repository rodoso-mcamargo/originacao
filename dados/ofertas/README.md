# dados/ofertas — ofertas públicas CVM (Res. 160) e primário vs secundário

Coletor: `ofertas.py` · workflow: `.github/workflows/ofertas-diaria.yml` (seg-sex 22:00 UTC).

## Fonte
Dados abertos da CVM, conjunto [oferta-distrib](https://dados.cvm.gov.br/dataset/oferta-distrib):
`https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip` (~6 MB). Usa o
arquivo `oferta_resolucao_160.csv` (ofertas pelo rito da Res. 160, desde 2023); o
`oferta_distribuicao.csv` (ICVM 400/476) não é usado. O zip não é commitado: fica em
`~/.cache/originacao` (actions/cache no workflow) e só é baixado de novo quando o
`Last-Modified` muda.

Escopo: Debêntures, CRI e CRA com `Data_requerimento` nos últimos 24 meses. O valor mobiliário
genérico "Certificados de Recebíveis" (Lei 14.430) e notas comerciais ficam fora.

O arquivo da Res. 160 **não traz série, vencimento, indexador nem remuneração**: em `base.csv`
`indexador` e `remuneracao` ficam vazios. Para debêntures, esses campos vêm do SND
(`dados/snd/caracteristicas.csv`) em `primario_vs_secundario.csv`.

## Arquivos
| arquivo | conteúdo |
|---|---|
| `base.csv` | todas as ofertas vistas, chave `numero_requerimento` (nunca apaga) |
| `AAAA-MM-DD.csv` | ofertas novas ou alteradas no dia (hash dos campos mudou). Na primeira carga não é gravado. |
| `primario_vs_secundario.csv` | uma linha por série de debênture ofertada |
| `ultimo.json` | resumo e `diagnostico` (lidos, gravados, descartados e motivo, erros) |

### base.csv
`numero_requerimento, numero_processo, tipo` (DEBENTURE/CRI/CRA), `valor_mobiliario, status, rito,
tipo_requerimento, tipo_oferta, data_requerimento, data_registro, data_encerramento,
cnpj_emissor, nome_emissor, cnpj_lider, nome_lider, emissao, quantidade, volume` (valor total
registrado), `titulo_incentivado, publico_alvo, sustentavel, regime_distribuicao, devedores`
(campo livre da CVM, cortado em 300 caracteres; em CRI/CRA é onde está o devedor), `indexador,
remuneracao` (vazios, a fonte não traz), `no_universo, casamento, nome_universo, hash`.

Casamento com o universo (`casamento`):
- `cnpj`: debênture de CNPJ com papel em `dados/ultimo.json` (via SND);
- `exato` / `fuzzy`: nome normalizado (sem acento, maiúsculas, sem pontuação e sem sufixos
  S A, SA, LTDA, CIA, COMPANHIA, PARTICIPACOES, EM RECUPERACAO JUDICIAL) contra o emissor de
  `dados/ultimo.json` (debêntures) ou o devedor de `dados/cri_cra/ultimo.json` (CRI/CRA, a
  partir do campo `devedores`). Fuzzy = rapidfuzz token_set_ratio ≥ 92 com pelo menos 2
  palavras específicas em comum; nunca casa por palavra genérica (ENERGIA, SANEAMENTO,
  TRANSMISSORA...) nem entre SPEs de numeral diferente (I ≠ II). Confira os `fuzzy`.

### primario_vs_secundario.csv
`numero_requerimento, data_oferta` (data de registro ou, sem ela, do requerimento),
`cnpj_emissor, emissor, emissao, codigo_primario` (série no SND com o mesmo CNPJ e número de
emissão, emitida até 180 dias da oferta), `familia, remuneracao_primario, taxa_primario`
(spread sobre DI, taxa real IPCA, taxa pré ou % do DI, conforme a família), `vencimento,
duration_ref_anos, duration_ref_fonte` (duration ANBIMA do papel novo se já está no painel;
senão o prazo até o vencimento), `codigo_secundario, duration_secundario_anos` (papel do mesmo
CNPJ e família em `dados/ultimo.json`, fora desta emissão, com duration mais próxima),
`taxa_secundario, data_taxa_secundario` (taxa indicativa ANBIMA em `dados/serie/` na data da
oferta ou até 7 dias antes), `diferenca_bps` ((primário − secundário) × 100),
`diferenca_pct_cdi` (só para % do DI, em pontos de % do CDI), `observacao`.

`diferenca_bps` fica vazio quando falta taxa numa ponta: a série histórica de taxas em
`dados/serie/` começa em 2026-02-23, então ofertas anteriores não têm taxa de secundário.

## Boletim ANBIMA de mercado de capitais
Não é processado (PDF). Link: https://data.anbima.com.br/publicacoes/boletim-de-mercado-de-capitais

## Ressalvas
- A duration de referência por prazo até o vencimento é maior que a duration real em papéis
  com amortização; serve só para escolher o papel comparável.
- Volume é o registrado na CVM para a oferta inteira (todas as séries).
- Triagem com fontes públicas, não é recomendação.
