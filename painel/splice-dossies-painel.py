# -*- coding: utf-8 -*-
"""Secao "Dossies de credito" do Radar do Secundario.

    python3 splice-dossies-painel.py radar-final.html radar-final.html

Aplicar DEPOIS de splice-agd-painel.py (a secao entra logo depois das AGDs) e
ANTES de splice-descartados-final.py. Le dados/dossies.json (mantido pela
tarefa "Dossies de credito — Radar") e dados/descartados.json: dossie de
descartado (nome exato ou termo de grupo) nao entra. Tambem soma o motivo
"dossie: evento/atencao" na triagem de debentures quando o emissor tem dossie
com esse nivel. Sem dossies.json, a secao entra vazia (a estrutura nao muda).
"""
import json, pathlib, re, sys, unicodedata

AQUI = pathlib.Path(__file__).resolve().parent


def base(nome):
    """Acha uma base de dados/dossies/: primeiro na raiz do repositorio
    (<raiz>/dados/dossies/<nome>), depois no caminho antigo (AQUI/dados/<nome>),
    usado pelas tarefas que copiam scripts e bases para uma pasta de trabalho."""
    for p in (AQUI.parent / "dados" / "dossies" / nome, AQUI / "dados" / nome):
        if p.exists():
            return p
    return AQUI / "dados" / nome

SRC, OUT = sys.argv[1], sys.argv[2]
s = open(SRC, encoding="utf-8").read()


def rep(t, a, b):
    assert t.count(a) == 1, f"ancora nao unica ({t.count(a)}): {a[:70]!r}"
    return t.replace(a, b, 1)


def norm(x):
    x = unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", x)).strip()


def ler(p, padrao):
    p = base(pathlib.Path(p).name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


dos = ler("dados/dossies.json", {"dossies": {}})["dossies"]
desc = ler("dados/descartados.json", {"descartados": [], "termos_grupo": []})
nomes = {norm(x) for x in desc.get("descartados", [])}
termos = [norm(t) for t in desc.get("termos_grupo", []) if norm(t)]
RX = re.compile(r"\b(" + "|".join(re.escape(t) for t in termos) + r")\b") if termos else None
rows = []
for k, d in dos.items():
    nm = norm(d.get("nome"))
    if nm in nomes or (RX and RX.search(nm)):
        continue
    rows.append({"k": k, **d})
ORD = {"evento": 0, "atencao": 1, "sem_sinal": 2}
rows.sort(key=lambda r: (ORD.get(r.get("nivel"), 3), r.get("pesquisado_em", "")), reverse=False)
PAY = json.dumps({"rows": rows}, ensure_ascii=True, separators=(",", ":"))
s = rep(s, '"papeis":[{"codigo"', '"dossies":' + PAY + ',"papeis":[{"codigo"')

CSS = """
<style>
/* dossies de credito */
.ds-bar{display:flex;gap:9px 14px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
.ds-f{display:flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.ds-cnt{color:var(--muted);font-size:12px;margin:2px 0 10px}
.ds-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:10px}
.ds-c{background:var(--surface);border:1px solid var(--line);padding:12px 14px;font-size:12.5px;line-height:1.5}
.ds-c.evento{box-shadow:inset 3px 0 0 var(--critico)}
.ds-c.atencao{box-shadow:inset 3px 0 0 var(--aviso)}
.ds-h{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ds-h b{font-size:13.5px}
.ds-n{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:700;padding:0 5px;border:1px solid;white-space:nowrap}
.ds-n.evento{color:var(--critico);border-color:var(--critico)}
.ds-n.atencao{color:var(--aviso);border-color:var(--aviso)}
.ds-n.sem_sinal{color:var(--muted);border-color:var(--line-strong)}
.ds-t{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-2);border:1px solid var(--line-strong);padding:0 4px}
.ds-dt{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--muted)}
.ds-g{color:var(--muted);font-size:11.5px;margin:2px 0 6px}
.ds-c details{margin-top:6px}
.ds-c summary{cursor:pointer;color:var(--ink-2);font-size:11.5px;font-weight:600}
.ds-c ul{margin:4px 0 0;padding-left:16px}
.ds-c li{margin:2px 0}
.ds-c a{color:var(--ink-2);text-decoration:underline;text-underline-offset:2px;text-decoration-color:var(--line-strong)}
.ds-o{margin-top:6px;padding:6px 8px;background:var(--surface-2);border:1px solid var(--line)}
.ds-more{margin:12px 0 0;text-align:center}
</style>
"""
s = rep(s, '<div id="app"></div>', CSS + '\n<div id="app"></div>')

JS = r"""
/* ── Dossiês de crédito ───────────────────────────────────────────────
   Pesquisa automática (tarefa "Dossiês de crédito — Radar") sobre toda
   debênture ilíquida das gestoras e todo emissor/devedor com abertura de
   spread >= 40 bps. Triagem com fontes públicas, não análise de crédito. */
const DS = D.dossies || {rows:[]};
const DS_ROT = {evento:"evento de crédito", atencao:"atenção", sem_sinal:"sem sinal"};
const DS_TP = {debenture:"debênture", cri_cra:"CRI/CRA", iliquida:"ilíquida"};
const dsEstado = {nivel:"sinal", tipo:"todos", q:"", aberto:false};
const DS_NOME = new Map(); (DS.rows||[]).forEach(r=>DS_NOME.set(r.nome, r));
function dsDeb(p){ const r = DS_NOME.get(p.emissor); return r && (r.nivel==="evento"||r.nivel==="atencao") ? r : null; }
function dsLinhas(){
  const q = dsEstado.q.trim().toLowerCase();
  return (DS.rows||[]).filter(r=>
    (dsEstado.nivel==="todos" || (dsEstado.nivel==="sinal" ? r.nivel!=="sem_sinal" : r.nivel===dsEstado.nivel)) &&
    (dsEstado.tipo==="todos" || r.tipo===dsEstado.tipo) &&
    (!q || (r.nome+" "+(r.codigos||[]).join(" ")+" "+(r.resumo||"")).toLowerCase().includes(q)));
}
function dsCard(r){
  const sin = (r.sinais||[]).map(x=>`<li>${x.data?`<span class="mono">${dataBR(x.data)}</span> · `:""}${esc(x.texto)}${x.url?` — <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.fonte||"fonte")}</a>`:""}</li>`).join("");
  const fon = (r.fontes||[]).map(x=>`<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.titulo||x.url)}</a></li>`).join("");
  const pen = (r.pendencias||[]).map(x=>`<li>${esc(x)}</li>`).join("");
  return `<div class="ds-c ${r.nivel}">
    <div class="ds-h"><b>${esc(r.nome)}</b><span class="ds-t">${DS_TP[r.tipo]||r.tipo}</span>
      <span class="ds-n ${r.nivel}">${DS_ROT[r.nivel]||r.nivel}</span><span class="ds-dt">${dataBR(r.pesquisado_em)}</span></div>
    <div class="ds-g">${esc((r.codigos||[]).join(", "))}${r.gatilho?` · entrou por: ${esc(r.gatilho)}`:""}</div>
    <div>${esc(r.resumo||"")}</div>
    ${r.originacao?`<div class="ds-o"><b>Originação:</b> ${esc(r.originacao)}</div>`:""}
    <details><summary>detalhes${(r.sinais||[]).length?` · ${(r.sinais||[]).length} sinal(is)`:""}</summary>
      ${r.papel?`<div style="margin-top:4px"><b>Papel:</b> ${esc(r.papel)}</div>`:""}
      ${r.controle?`<div><b>Controle:</b> ${esc(r.controle)}</div>`:""}
      ${r.numeros?`<div><b>Números:</b> ${esc(r.numeros)}</div>`:""}
      ${sin?`<div style="margin-top:4px"><b>Sinais</b><ul>${sin}</ul></div>`:""}
      ${pen?`<div><b>Falta checar</b><ul>${pen}</ul></div>`:""}
      ${fon?`<div><b>Fontes</b><ul>${fon}</ul></div>`:""}
    </details></div>`;
}
function dsRender(){
  const all = dsLinhas(), vis = dsEstado.aberto ? all : all.slice(0,12);
  document.getElementById("dsCnt").textContent = `${all.length} dossiê(s) · mostrando ${vis.length} · ${(DS.rows||[]).length} na base`;
  document.getElementById("dsGrid").innerHTML = vis.map(dsCard).join("")
    || `<div class="ds-c" style="color:var(--muted)">Nenhum dossiê neste filtro.</div>`;
  const m = document.getElementById("dsMore");
  m.innerHTML = all.length>12 ? `<button type="button" class="btn" aria-expanded="${dsEstado.aberto}">${dsEstado.aberto?"▲ recolher":"▼ ver todos ("+(all.length-12)+" restantes)"}</button>` : "";
  const b = m.querySelector("button"); if (b) b.onclick = () => { dsEstado.aberto=!dsEstado.aberto; dsRender(); };
}
function secaoDossies(){
  const R = DS.rows||[], n = k => R.filter(r=>r.nivel===k).length;
  return `<section>
    <div class="head"><h2>Dossiês de crédito</h2>
      <span class="eyebrow">${R.length} empresas pesquisadas · ${n("evento")} com evento de crédito · ${n("atencao")} em atenção</span></div>
    <p class="note">Pesquisa automática, todo dia útil, sobre <b>toda debênture ilíquida</b> das carteiras das gestoras
    e <b>todo emissor ou devedor com abertura de spread de 40 bps ou mais</b> (debêntures em 21 pregões; CRI/CRA na
    janela). Cada dossiê reúne rating, covenants, waivers e AGDs, demonstrações, poder concedente, notícias e uma leitura
    para originação, com as fontes. O dossiê é refeito a cada 30 dias, ou antes se o spread abrir mais 50 bps ou sair AGD de
    crédito nova. <b>Evento de crédito</b>: default, RJ/RE, covenant rompido, waiver, vencimento antecipado em pauta,
    rebaixamento forte. <b>Atenção</b>: pressão relevante sem evento formal. É triagem com fontes públicas, não análise
    de crédito; descartados não aparecem.</p>
    <div class="ds-bar">
      <label class="ds-f">nível <select id="dsNv">
        <option value="sinal">com sinal (${n("evento")+n("atencao")})</option>
        <option value="evento">evento de crédito (${n("evento")})</option>
        <option value="atencao">atenção (${n("atencao")})</option>
        <option value="sem_sinal">sem sinal (${n("sem_sinal")})</option>
        <option value="todos">todos (${R.length})</option></select></label>
      <label class="ds-f">tipo <select id="dsTp">
        <option value="todos">todos</option><option value="debenture">debêntures</option>
        <option value="cri_cra">CRI/CRA</option><option value="iliquida">ilíquidas</option></select></label>
      <label class="ds-f">busca <input type="search" id="dsQ" placeholder="empresa ou código"></label>
    </div>
    <div class="ds-cnt" id="dsCnt"></div>
    <div class="ds-grid" id="dsGrid"></div>
    <div class="ds-more" id="dsMore"></div>
  </section>`;
}
function initDossies(){
  if (!document.getElementById("dsGrid")) return;
  document.getElementById("dsNv").onchange = e => { dsEstado.nivel=e.target.value; dsEstado.aberto=false; dsRender(); };
  document.getElementById("dsTp").onchange = e => { dsEstado.tipo=e.target.value; dsEstado.aberto=false; dsRender(); };
  document.getElementById("dsQ").oninput = e => { dsEstado.q=e.target.value; dsEstado.aberto=false; dsRender(); };
  dsRender();
}
"""
s = rep(s, "function montar(){", JS + "\nfunction montar(){")
s = rep(s, "    ${secaoAgd()}", "    ${secaoAgd()}\n\n    ${secaoDossies()}")
s = rep(s, """function motivosTriagem(s){
  const p = s.p, m = [];""", """function motivosTriagem(s){
  const p = s.p, m = [];
  { const g = dsDeb(p); if (g) m.push(["alerta", `dossiê: ${DS_ROT[g.nivel]}`]); }""")
s = rep(s, "  initAgd();\n", "  initAgd();\n  initDossies();\n")
open(OUT, "w", encoding="utf-8").write(s)
from collections import Counter
print("dossies:", len(rows), dict(Counter(r.get("nivel") for r in rows)), "| bytes:", len(s.encode()))
