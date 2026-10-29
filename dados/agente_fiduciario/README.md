# dados/agente_fiduciario — relatórios anuais de agente fiduciário

Coletor: `agente_fiduciario.py` · workflow: `.github/workflows/agente-fiduciario-mensal.yml`
(dia 5 de cada mês, 10:20 UTC; anos de referência: anterior e corrente).

## Fontes

| agente | listagem | arquivo |
|---|---|---|
| Oliveira Trust | JSON `https://services-ft.oliveiratrust.com.br/app/v1/titulos/documentos?search=Relatório Anual&tipo_ativo=cri\|cra&ano=AAAA&page=N&limit=100` (o mesmo da página `/investidor/documentos`) | `…/titulos/documentos/download?uid[]=<uid>` |
| Pentágono | HTML `POST https://www.pentagonotrustee.com.br/RelatoriosAnuais` (`Prod=CRI\|CRA`, `Ano=AAAA`) | `/Docs/RelatAnuais/Relatorio_Anual_<PROD>_<ANO>/<nome>.pdf` |
| Vórtx | busca `https://www.vortx.com.br/investidor/dcm?busca=<código IF>` (payload da própria página) + JSON `https://apis.vortx.com.br/vxsite/api/operacao/<id>/relatorios-anuais` | link assinado (expira); guardamos a página da operação |

Nenhuma das três pede login para a listagem nem para o arquivo. robots.txt: Oliveira Trust
bloqueia `/ri`, `/scot/`, `/cri/`, `/downloads/` do site institucional, que não usamos (a listagem
e o download ficam em `services-ft.oliveiratrust.com.br`); Pentágono bloqueia `/Download` e
`/Uploads`, não `/Docs`; Vórtx sem robots.txt. Pausa de 1,1 s, User-Agent `originacao-radar/1.0`.
Se alguma fonte passar a exigir login, o coletor registra `texto=login` e segue (não contorna).

## Casamento

- Vórtx: pelo código IF, que é o mesmo código da ANBIMA em `dados/cri_cra` → `match=codigo`.
- Oliveira Trust e Pentágono publicam por securitizadora + emissão + série. A emissão de cada
  código vem do casamento do `fnet_cricra` (`dados/fnet_cricra/universo.csv`, linhas não
  ambíguas) → `match=emissao_serie`. Código sem emissão conhecida fica de fora nesses dois.
- **Debêntures: fora por enquanto.** Dependem de `dados/snd/caracteristicas.csv` (agente de cada
  emissão), que ainda não existe na main.

## Colunas de `relatorios.csv`

`agente`, `tipo` (CRI/CRA), `codigo`, `devedor`, `securitizadora`, `emissao`, `serie`,
`ano_relatorio` (exercício de referência, como a fonte informa), `arquivo` (nome do arquivo na
fonte), `url`, `match`, `flag_alerta` (o texto contém inadimplemento / descumprimento / não
atendido / vencimento antecipado / waiver), `flag_sem_negacao` (há ao menos uma ocorrência que
não vem logo depois de "não houve", "inexistência" etc.), `termos`, `trecho` (até 300 caracteres
em volta da primeira ocorrência, preferindo a não negada), `texto` (`pdf`, `nao_pdf`, `login`,
`erro`), `coletado_em`.

Chave: `agente` + `codigo` + `url` + `arquivo`. Um relatório já lido não é baixado de novo.
`ultimo.json` traz contagens e o bloco `diagnostico`.

## Ressalvas

- Relatório anual de AF é cheio de texto-padrão ("não houve inadimplemento…"): use
  `flag_sem_negacao`, e mesmo assim leia o relatório antes de concluir algo.
- Só metadados e trecho curto; o PDF não é gravado. Repositório público; não é recomendação.
