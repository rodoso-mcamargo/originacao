# Originação — regras para o Claude

Repositório da rotina "Radar do Secundário" (projeto "Originação" do rodrigo).

## Dossiês de crédito (dados/dossies/)
- `dados/dossies/dossies.json` é a cópia versionada da base de dossiês de crédito (a base viva fica
  no projeto "Originação" do Claude, `claude/dossies.json`). Mesma coisa para `iliquidas.json` e
  `descartados.json`.
- **Antes de pesquisar qualquer emissor/devedor, leia `dados/dossies/dossies.json`.** Se já existe
  dossiê da chave com `pesquisado_em` de 30 dias ou menos e sem gatilho novo (abertura +50 bps desde
  o dossiê ou AGD de crédito posterior), use o que está lá e não pesquise de novo.
- Ao mesclar com outra cópia: por chave, fica o dossiê de `pesquisado_em` mais recente; nunca apague
  chave. Em `iliquidas.json`, por raiz, fica o nome não nulo e o `est` de data mais recente.
  `descartados.json`: a versão do projeto manda; nunca tire nome ou termo por conta própria.
- Nunca redigite esses JSON à mão: copie arquivos (cp) e valide com `json.load`.
- O repositório é público: tudo aqui é visível. Triagem com fontes públicas, não é recomendação.

## Commits
- Commits de dados só mexem em `dados/`. Push com `git pull --rebase` antes (os workflows do Actions
  também commitam na `main`).
