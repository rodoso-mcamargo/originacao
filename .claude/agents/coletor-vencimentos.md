---
name: coletor-vencimentos
description: Constrói o coletor de características e agenda de pagamentos das debêntures (SND) e de ofertas públicas da CVM, para saber quem precisa refinanciar e comparar taxa de primário com secundário.
tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch
---
Leia docs/regras-coletores.md e siga à risca. Branch: coleta/vencimentos.

Objetivo: para cada debênture do universo (dados/ultimo.json), características e cronograma
de pagamentos; e as emissões novas, comparadas com o secundário do mesmo emissor.

Parte A: SND (www.debentures.com.br), script snd.py
- Páginas de características (exploreosnd/consultaadados/emissoesdedebentures/
  caracteristicas_d.asp?selecao=<CODIGO>) e agenda de eventos (juros, amortização,
  vencimento). Procure primeiro exportação em lote (txt/csv); só caia para página por código
  se não houver, com cache de 30 dias por código (código novo no universo entra na hora).
- dados/snd/caracteristicas.csv (um por código): codigo, isin, emissor, cnpj_emissor,
  numero_emissao, serie, data_emissao, vencimento, volume_emissao, quantidade,
  valor_nominal, indexador, remuneracao, garantia (como a fonte disser), incentivada,
  agente_fiduciario, coordenador_lider, situacao, atualizado_em.
- dados/snd/agenda.csv: codigo, data_evento, tipo (juros/amortizacao/vencimento/resgate),
  percentual_ou_valor.
- dados/snd/vencimentos_emissor.csv: por cnpj, principal a vencer em 0-6, 6-12, 12-18 e
  18-24 meses (amortização + vencimento). Sem saldo atual na fonte, use volume × % e marque
  saldo_estimado=true.
- Workflow snd-diaria.yml, cron 30 9 * * 1-5.

Parte B: ofertas CVM, script ofertas.py
- Dados abertos (https://dados.cvm.gov.br/dataset/oferta-distrib). Inspecione o zip e use o
  arquivo das ofertas Res. 160 (valor mobiliário, emissor, CNPJ, data, volume, rito,
  remuneração/indexador se houver).
- Debêntures, CRI e CRA dos últimos 24 meses. dados/ofertas/AAAA-MM-DD.csv (novas ou
  alteradas) e dados/ofertas/base.csv (chave: protocolo/requerimento).
- dados/ofertas/primario_vs_secundario.csv: para debêntures, cruze por CNPJ com
  dados/snd/caracteristicas.csv e dados/ultimo.json; papel do mesmo emissor e indexador com
  duration mais próxima; taxa indicativa na data da oferta; diferença em bps (null se faltar
  taxa numa ponta).
- Workflow ofertas-diaria.yml, cron 0 22 * * 1-5.
- Boletim ANBIMA de mercado de capitais: só registre o link no README, sem parsing de PDF.

Entrega: PR com os dois coletores, testes e o resumo dos runs.
