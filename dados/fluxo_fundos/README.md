# Fluxo dos fundos (informe diario CVM)

Captacao e resgate diarios dos fundos de credito privado e dos incentivados/infra, por
fundo, por gestor e total, com alerta de resgate forte. Gerado por `fluxo_fundos.py`
(workflow `fluxo-fundos-diaria.yml`, seg-sex 21:00 UTC).

## Fontes

- Informe diario: `https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_AAAAMM.zip`
  (colunas pos-Res. 175: `TP_FUNDO_CLASSE; CNPJ_FUNDO_CLASSE; ID_SUBCLASSE; DT_COMPTC;
  VL_TOTAL; VL_QUOTA; VL_PATRIM_LIQ; CAPTC_DIA; RESG_DIA; NR_COTST`).
- Cadastro pos-175: `https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip`
  (`registro_classe.csv` + `registro_fundo.csv`: nome, gestor, tipo, classificacao,
  situacao, PL).
- Leitura (sem alterar): `dados/cda/AAAA-MM.csv` mais recente e `painel/gestoras-flow.json`.

A rotina diaria confere o mes corrente e o anterior (HEAD; so baixa de novo se mudou) e
le mais 3 meses do cache para as janelas de 63 dias uteis. Os zips ficam no
`actions/cache`, nunca no git.

## Universo (`universo.csv`)

Colunas: `cnpj, nome, gestor, classe, gestora_painel, pl, origem`.

- `origem = cda`: todo fundo/classe que aparece na CDA mais recente do radar
  (`dados/cda/`), sem corte de PL.
- `origem = filtro_nome`: classe FIF de renda fixa, em funcionamento normal, com
  `CRED PRIV`, `CREDITO PRIVADO`, `INCENTIVAD`, `INFRA`, `DEBENTURE` ou `12.431` no nome
  (classe ou fundo, sem acento) e PL >= R$ 50 mi (PL do ultimo informe; sem informe, o do
  cadastro).
- Fora: FIDC, FII, FIP (e FIAGRO/FMIEE), pelo tipo do cadastro ou pelo nome.
- `gestora_painel`: marca da gestora quando o gestor do cadastro e uma das gestoras de
  `painel/gestoras-flow.json`. Esse arquivo so traz o nome das gestoras (nao lista
  fundos), por isso ele marca o fundo em vez de trazer fundos novos.
- `classe` (pelo nome): `incentivado_infra` (INCENTIVAD, INFRA, 12.431),
  `credito_privado` (demais palavras de credito) ou `outros` (fundo da CDA sem palavra de
  credito no nome, p. ex. RF referenciado DI).

## Arquivos

| Arquivo | Chave | Conteudo |
|---|---|---|
| `AAAA-MM-DD.csv` | cnpj (data = competencia) | um por dia util; uma linha por fundo do universo que informou |
| `por_gestor/AAAA-MM.csv` | data, gestor, classe | serie agregada, um arquivo por mes (a rodada diaria so regrava os meses recalculados); `todas` = soma das classes do gestor; `TOTAL` = universo |
| `alertas.csv` | cnpj, regra, data_inicio | episodios de resgate forte |
| `universo.csv` | cnpj | recorte da rodada |
| `ultimo.json` | - | data de referencia, totais, 10 maiores resgates em 21 du, `diagnostico` |

Colunas do arquivo diario: `data, cnpj, pl, captacao, resgate, liquido, pct_pl_d1,
liq_5d, pct_5d, liq_21d, pct_21d, liq_63d, pct_63d, cotistas, quota, alerta`
(R$ inteiros; % com 3 casas). Nome, gestor e classe: join com `universo.csv`.

## Calculo

- `liquido = captacao - resgate` do dia; `pct_pl_d1` = liquido / PL do dia util anterior.
- `liq_Nd` (N = 5, 21, 63): soma do liquido nos ultimos N dias uteis; `pct_Nd` = sobre o PL
  do dia util anterior ao inicio da janela. Sem historico suficiente: vazio.
- Dia util = data do informe com pelo menos 20% da mediana de linhas por dia, de segunda a
  sexta (datas avulsas e o ultimo dia ainda incompleto ficam de fora ate encorparem).
- Classe com subclasses: o informe traz uma linha por subclasse; PL, captacao, resgate e
  cotistas sao somados e a `quota` fica vazia.
- Dia sem informe de um fundo conta fluxo zero nas janelas e o PL segue o ultimo informado.
- Campo vazio na fonte nunca vira zero (regra 8): captacao **ou** resgate vazio deixa `liquido`
  e `pct_pl_d1` null, e toda janela (5/21/63 du) que contem esse dia fica null. Numa classe com
  subclasses, se alguma subclasse veio sem o campo, o total da classe fica null (soma parcial
  seria inventada). Nos agregados, fundo com valor null fica fora da soma daquele campo
  (`n_fundos` conta todos os que informaram).
- Agregados: % = soma dos fluxos / soma das bases dos fundos com base valida.
- Data de referencia (`ultimo.json`): ultimo dia util em que >= 80% dos fundos com dado
  informaram.

## Alertas

Cortes no dicionario `CORTES` no topo do script:

- `resgate_5d`: resgate liquido em 5 du > 5% do PL base;
- `resgate_21d`: resgate liquido em 21 du > 10% do PL base.

Um episodio e a sequencia de dias seguidos (entre os dias em que o fundo informou) com a
regra disparada; `ativo` = o episodio vai ate o ultimo informe do fundo. Episodio que ja
vinha de rodadas anteriores herda a `data_inicio` gravada (`dias` e `pior_pct` contam so o
trecho recalculado na rodada). Os episodios sao recalculados a partir do 22o dia util lido,
quando as duas janelas ja tem base.

## Ressalvas

- Fluxo de FIC e de master aparecem nos dois: a soma por gestor conta duas vezes o
  dinheiro que entra num FIC e desce para o master. Para fluxo do investidor final, olhe
  o fundo/FIC; para pressao de venda na carteira, olhe o master.
- Resgate em fundos exclusivos/restritos (um cotista) costuma ser movimento de tesouraria
  ou realocacao, nao sinal de mercado; confira `cotistas`.
- A CVM republica o mes corrente diariamente e aceita reenvio de meses anteriores: a
  rodada regrava os dois ultimos meses.
- Dado publico da CVM, sem ajuste. Triagem, nao recomendacao.
