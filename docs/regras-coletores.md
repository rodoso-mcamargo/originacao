# Regras para coletores de dados do Radar

1. Leia CLAUDE.md e .github/workflows/ antes de escrever qualquer coisa.
2. Branch própria (`coleta/<fonte>`), PR para main. Nunca push direto na main.
3. Escopo: só crie/edite o seu script (raiz do repo, `<fonte>.py`), o seu workflow
   (`.github/workflows/<fonte>-diaria.yml`), a sua pasta em `dados/<fonte>/`, os seus testes
   (`tests/test_<fonte>.py`, `tests/fixtures/<fonte>/`) e o README da pasta. Não toque em
   painel/, monitor.py, reune.py, cri_cra.py, coleta-diaria.yml, workflows de outros nem em
   dados/ de outras fontes.
4. Push no workflow, sempre com:
   for i in 1 2 3 4 5; do git pull --rebase -X theirs origin main && git push && break; [ $i = 5 ] && exit 1; sleep $((i*10)); done
   Não use um grupo de `concurrency` compartilhado entre workflows: o GitHub guarda só um
   run pendente por grupo e cancela o anterior quando chega outro. O laço acima já resolve
   a disputa de push.
   O passo de commit/push só grava quando `GITHUB_REF` é `refs/heads/main`; fora da main
   ele só lista o que gravaria (`git diff --staged --stat`), como o job `salvar` do
   coleta-diaria.yml.
5. IPv6: o runner não tem rota IPv6. Force IPv4: `curl -4`; em Python, no topo do script,
   import socket; _gai = socket.getaddrinfo
   socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)
6. workflow_dispatch com `inspecionar` (sim/não): com sim, só descreve a fonte (status,
   colunas reais, 3 registros) no GITHUB_STEP_SUMMARY, sem gravar. Traceback não tratado
   também vai para o summary.
7. Fonte sem API: descubra o endpoint que a própria página usa (RSS, wp-json, JSON do site
   antes de HTML). Respeite robots.txt e termos, ≤ 1 req/s, User-Agent identificável. Nunca
   contorne login, paywall ou captcha: registre no README e siga com o resto.
8. Não invente dado: o que a fonte não traz fica null. Datas em ISO, sempre da fonte.
9. Saída: um arquivo por dia (AAAA-MM-DD.csv/json), base acumulada com chave explícita,
   `ultimo.json` com bloco `diagnostico` (lidos, gravados, descartados e motivo, erros por
   fonte) e README.md (fonte, URLs, colunas, frequência, ressalvas de uso).
10. Não commite arquivo bruto grande (zip, PDF). Use actions/cache para downloads.
11. Normalização de nomes de empresa (para casar com o universo do painel): sem acento,
    maiúsculas, sem pontuação, sem sufixos (S A, SA, LTDA, CIA, COMPANHIA, PARTICIPACOES,
    EM RECUPERACAO JUDICIAL). Exato primeiro; fuzzy (rapidfuzz token_set_ratio ≥ 92) marcado
    como tal; nunca casar por palavra genérica sozinha (ENERGIA, SANEAMENTO, TRANSMISSORA).
    Universo: dados/ultimo.json (campo emissor) e dados/cri_cra/ultimo.json (devedor).
12. Testes pytest com fixture pequena em tests/fixtures/<fonte>/, cobrindo parsing,
    normalização e classificação.
13. Antes de abrir o PR:
    a. Rode na sessão: `pytest tests/test_<fonte>.py`, `python <fonte>.py --inspecionar` e
       um run real (e backfill, se houver) gravando localmente em dados/<fonte>/.
    b. Rode o workflow no GitHub Actions na sua branch. O GitHub só deixa disparar à mão um
       workflow que já existe na main, então adicione um gatilho temporário
       `push: { branches: [coleta/<fonte>] }`, faça push, acompanhe o run (inspecionar fica
       vazio = run real; o passo de push só lista, pela regra 4) e, depois do run, REMOVA o
       gatilho temporário num commit antes de abrir o PR.
    c. No PR, cole o resumo do job, contagens e 10 linhas de saída.
14. Repositório público: tudo o que for commitado fica visível. De comunicados de agência,
    guarde título, data, nota e link, nunca o texto integral.
15. Crons já ocupados (UTC): 10:00 todo dia (rj-diario), 11:40 seg-sex (coleta-diaria),
    13:00 seg (cda-mensal). Não use esses minutos.
