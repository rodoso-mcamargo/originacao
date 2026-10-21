# -*- coding: utf-8 -*-
"""Nome do emissor e sinal de estresse nas debentures iliquidas (secao de gestoras).

    python3 splice-iliquidas-painel.py radar-final.html radar-final.html

Aplicar DEPOIS de splice-gestoras-painel.py (qualquer ponto depois dele).
Le dados/iliquidas.json: {"raizes": {RAIZ: {"nome", "est": {nivel, resumo, data, fonte, url}}}}.
A CDA so traz a raiz do codigo (TOTS, CBRD...) para debenture iliquida; aqui
ela vira o nome da empresa, com a raiz ao lado, e o sinal de estresse (quando
houver) entra como selo na coluna Rating.
"""
import json, pathlib, sys

AQUI = pathlib.Path(__file__).resolve().parent
SRC, OUT = sys.argv[1], sys.argv[2]
s = open(SRC, encoding="utf-8").read()
mapa = json.loads((AQUI / "dados" / "iliquidas.json").read_text(encoding="utf-8"))["raizes"]


def rep(t, a, b):
    assert t.count(a) == 1, f"ancora nao unica ({t.count(a)}): {a[:70]!r}"
    return t.replace(a, b, 1)


CSS = """
<style>
/* debentures iliquidas: nome do emissor e sinal de estresse */
.gc-raiz{font-family:"IBM Plex Mono",monospace;color:var(--muted);font-size:10.5px;margin-left:6px}
.gc-est{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:700;padding:0 5px;border:1px solid;white-space:nowrap;text-decoration:none;margin-left:4px}
.gc-est.evento{color:var(--critico);border-color:var(--critico)}
.gc-est.atencao{color:var(--aviso);border-color:var(--aviso)}
</style>
"""
s = rep(s, '<div id="app"></div>', CSS + '\n<div id="app"></div>')

JS = "const GC_ILIQ = " + json.dumps(mapa, ensure_ascii=True, separators=(",", ":")) + ";\n" + r"""
const GC_EST_ROT = {evento:"evento de crédito", atencao:"atenção"};
function gcIliq(r){ return r.tipo==="Debênture ilíq." ? (GC_ILIQ[r.ent]||null) : null; }
function gcEnt(r){
  const m = gcIliq(r);
  if (!m || !m.nome) return `<span title="${esc(r.ent)}${r.tk?" · "+esc(r.tk):""}">${esc(r.ent)}</span>`;
  return `<span title="${esc(m.nome)} · raiz ${esc(r.ent)}">${esc(m.nome)}</span><span class="gc-raiz">${esc(r.ent)}</span>`;
}
function gcEstChip(r){
  const m = gcIliq(r); if (!m || !m.est) return "";
  const e = m.est, tit = `${e.resumo}${e.data?" ("+e.data+")":""} — ${e.fonte}`;
  return `<a class="gc-est ${e.nivel}" href="${esc(e.url)}" target="_blank" rel="noopener" title="${esc(tit)}">${GC_EST_ROT[e.nivel]||e.nivel} ↗</a>`;
}
"""
s = rep(s, "function gcRow(r){", JS + "function gcRow(r){")
s = rep(s, '<td class="l"><span title="${esc(r.ent)}${r.tk?" · "+esc(r.tk):""}">${esc(r.ent)}</span>',
        '<td class="l">${gcEnt(r)}')
s = rep(s, '<td class="l">${gcRatChip(r)}</td></tr>`;', '<td class="l">${gcRatChip(r)}${gcEstChip(r)}</td></tr>`;')
s = rep(s, "A tabela lista as <b>60 maiores posições por gestora</b>",
        "Nas <b>ilíquidas</b>, a CDA só traz a raiz do código: o nome do emissor foi resolvido pelo código "
        "(raiz ao lado, em cinza), e o selo <b>evento de crédito</b> ou <b>atenção</b> marca o que uma busca na web "
        "achou de estresse (passe o mouse para ler e clique para a fonte) — triagem, não análise de crédito. "
        "A tabela lista as <b>60 maiores posições por gestora</b>")
open(OUT, "w", encoding="utf-8").write(s)
n = sum(1 for v in mapa.values() if v.get("nome")); e = sum(1 for v in mapa.values() if v.get("est"))
print(f"iliquidas: {n} nomes, {e} com sinal de estresse | bytes: {len(s.encode())}")
