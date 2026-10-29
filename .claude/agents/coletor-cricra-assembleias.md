---
name: coletor-cricra-assembleias
description: Constrói o coletor de assembleias e documentos de CRI/CRA no Fundos.NET, classificados por pauta de crédito, e o levantamento mensal de relatórios de agente fiduciário.
tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch
---
Leia docs/regras-coletores.md e siga à risca. Branch: coleta/cricra-assembleias.
Leia dados/cri_cra/ultimo.json (códigos, ISIN se houver, devedor, securitizadora).

Parte A: Fundos.NET (fnet.bmfbovespa.com.br), script fnet_cricra.py,
workflow fnet-cricra-diaria.yml, cron 30 22 * * 1-5, backfill de 270 dias.
- Descubra o endpoint JSON da pesquisa pública (algo como
  pesquisarGerenciadorDocumentosDados) e os filtros por securitizadora/CRI/CRA, categoria,
  tipo e data. Sem contornar captcha/login.
- Campos: data_entrega, securitizadora, emissao, serie, categoria, tipo, especie, assunto,
  status, id_documento, url (exibirDocumento?id=).
- Casamento com dados/cri_cra: ISIN ou código; senão emissão + série + securitizadora.
  Campo match = isin | codigo | emissao_serie | nenhum.
- Classe por regex (no topo, testadas): credito = waiver, renúncia, perdão, não
  declaração/decretação de vencimento antecipado, covenant, índice financeiro, alteração de
  cronograma/prazo/remuneração, repactuação, inadimplemento, recuperação judicial ou
  extrajudicial; estrutural = substituição de devedor, garantia, agente fiduciário ou
  securitizadora, resgate, amortização extraordinária, mudança de controle; sem_pauta =
  edital/ata sem pauta no assunto.
- Saída em dados/fnet_cricra/: AAAA-MM-DD.json, base.csv (chave id_documento),
  universo.csv (casados, com classe), ultimo.json.

Parte B: agentes fiduciários, script agente_fiduciario.py,
workflow agente-fiduciario-mensal.yml, cron 20 10 5 * * (10:00 UTC é do rj-diario).
- Oliveira Trust, Pentágono e Vórtx: para os CRI/CRA do universo e para as debêntures com
  agente conhecido em dados/snd/caracteristicas.csv (se o arquivo ainda não existir no
  main, pule as debêntures e diga isso no PR), ache a página da emissão e extraia só
  metadados: agente, emissão, ano do relatório, url, flag se o texto contém
  inadimplemento/descumprimento/não atendido/vencimento antecipado/waiver, e trecho de até
  300 caracteres. Sem PDF no repositório. Login → README e pula.
- Saída: dados/agente_fiduciario/relatorios.csv.

Entrega: PR com runs de inspeção, backfill e real; no PR, contagem por classe e 10 linhas de
universo.csv.
