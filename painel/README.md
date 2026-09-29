# painel/ — geração do Radar do Secundário

Estes arquivos moravam como documentos do projeto do Claude (`claude/gerador-dashboard.py` e
companhia) e foram trazidos para o repositório em 29/09/2026. Motivo: são código-fonte, e
código-fonte pertence ao controle de versão — não a um projeto preso a uma conta.

Nada aqui roda em GitHub Actions. É a rotina diária do Claude que baixa estes arquivos por
`raw.githubusercontent.com`, junta com o que os coletores gravaram em `dados/`, gera o HTML e
republica o Artifact.

## Conteúdo

| Arquivo | Papel | Nome anterior no projeto |
|---|---|---|
| `dashboard.py` | gerador: lê `dados/ultimo.json` + `dados/serie.json` (+ CDA, ações, CRI/CRA, ratings), monta o payload e injeta no template | `claude/gerador-dashboard.py` |
| `template.html` | a página inteira — CSS, JS e as 17 seções. O gerador substitui `/*__DADOS__*/null` pelo payload | `claude/gerador-dashboard-template.html` |
| `splice-destaques-painel.py` | 1ª camada: destaques de ação (3 meses). Troca a leitura de `var_12m` por 3m, porque o plano grátis da brapi só entrega 3m | igual |
| `splice-gestoras-painel.py` | 2ª camada: seção de carteira das gestoras + a lista `DESCARTADOS` da triagem | igual |
| `gestoras-flow.json` | retrato mensal fixo das gestoras (fev/2026 contra 3m e 6m). Não é atualizado pela rotina diária | igual |

## Ordem de execução

Os dois splices **não comutam**. A ordem é fixa:

```bash
python3 -c "import sys,pathlib; sys.path.insert(0,'.'); import dashboard as DB; \
  DB.DADOS=pathlib.Path('dados'); DB.gerar(pathlib.Path('radar-secundario.html'), demo=False)"
python3 splice-destaques-painel.py                                   # edita radar-secundario.html no lugar
python3 splice-gestoras-painel.py radar-secundario.html radar-final.html
```

`radar-final.html` é o que vai para o Artifact. **Nunca** gere com `demo=True` — isso carimba a
página como dados simulados.

Cada splice imprime `checks:` no fim. No splice de destaques o esperado é
`True True True False` — o último é `"a.var_12m" in inner`, que **tem** que dar `False`, porque
a camada justamente remove a leitura de 12 meses. Se um splice reclamar de âncora não
encontrada, alguém mexeu no `template.html` sem ajustar o splice: conserte o splice, não o HTML
gerado.

## O que fica de fora daqui, de propósito

- **`ratings-credito.json`** — continua documento do projeto do Claude. É o único estado que a
  rotina **escreve** (`project_write`, quando pesquisa um emissor novo), e uma sessão do Claude
  não tem permissão de push neste repositório. Se um dia tiver, ele pode mudar de casa.
- **`splice-gestoras-cricra.py`** (depreciado — embutia um bloco próprio de CRI/CRA e
  duplicaria a seção; CRI/CRA é nativo no gerador desde a V34) e
  **`patch-template-cricra.py`** (patch pontual já aplicado). Ficam no projeto, como histórico.

## Dependência de caminho

`dashboard.py` resolve `RAIZ = Path(__file__).parent.parent` e lê `template.html` de
`Path(__file__).parent`. Na rotina isso é contornado com `DB.DADOS = Path('dados')` e mantendo
`template.html` ao lado do script. Se for rodar de outro jeito, ajuste `DB.DADOS` — não mova o
template para longe do `dashboard.py`.
