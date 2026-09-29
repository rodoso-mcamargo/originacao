# -*- coding: utf-8 -*-
"""Camada 'Run da mesa (Ativa) x ANBIMA' + aviso de fonte, sobre o HTML final.

    python3 splice-run-painel.py radar-final.html radar-final.html

Le dados/run/ultimo.json e o bloco "fonte" de dados/ultimo.json (ambos
gravados por run_ativa.py). Sem run, as secoes ficam vazias mas o codigo entra
igual. Aplicar DEPOIS de splice-gestoras-painel.py.
"""
import json, pathlib, sys

AQUI = pathlib.Path(__file__).resolve().parent
SRC, OUT = sys.argv[1], sys.argv[2]
inner = open(SRC, encoding="utf-8").read()

run_f = AQUI / "dados" / "run" / "ultimo.json"
run = json.loads(run_f.read_text(encoding="utf-8")) if run_f.exists() else {}
ult = json.loads((AQUI / "dados" / "ultimo.json").read_text(encoding="utf-8"))
fonte = ult.get("fonte") if (ult.get("fonte") or {}).get("tipo") == "run_ativa" else None
fonte_papel = {p["codigo"]: p["fonte_taxa"] for p in ult.get("papeis", [])
               if p.get("fonte_taxa") and p["fonte_taxa"] != "anbima_run"} if fonte else {}

# Sem run e sem fallback o codigo entra do mesmo jeito (as secoes so nao
# desenham nada): assim a estrutura do HTML nao muda de um dia para o outro e
# a conferencia do passo 6 nao acusa funcao sumida.

def rep(s, a, b):
    assert s.count(a) == 1, f"ancora nao unica ({s.count(a)}): {a[:60]!r}"
    return s.replace(a, b, 1)

# CRI/CRA: o payload do painel so traz parte dos papeis; a referencia ANBIMA
# dos demais vem da base completa (dados/cri_cra/ultimo.json, ultimo pregao).
cc_f = AQUI / "dados" / "cri_cra" / "ultimo.json"
if run and run.get("rows") and cc_f.exists():
    tx = {}
    for a in json.loads(cc_f.read_text(encoding="utf-8")).get("ativos", []):
        if a.get("taxa_indicativa") is not None and a.get("referencia", "") >= tx.get(a["codigo"], ("",))[0]:
            tx[a["codigo"]] = (a["referencia"], a["taxa_indicativa"])
    for x in run["rows"]:
        if x.get("tp") in ("CRI", "CRA") and x["cod"] in tx:
            x["anbima_ref"], x["anbima"] = tx[x["cod"]]

PAY = json.dumps({"run": run or None, "fonte": fonte, "fontePapel": fonte_papel},
                 ensure_ascii=True, separators=(",", ":"))
inner = rep(inner, '"papeis":[{"codigo"', '"runAtiva":' + PAY + ',"papeis":[{"codigo"')

CSS = """
<style>
/* run da mesa (Ativa) e aviso de fonte */
.fonte-av{background:var(--demo-bg);color:var(--demo-ink);border:1px solid var(--demo-line);
  padding:11px 14px;margin:14px 0 0;font-size:12.5px;line-height:1.5}
.fonte-av b{font-weight:700}
.rn-bar{display:flex;gap:9px 14px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
.rn-f{display:flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.rn-cnt{color:var(--muted);font-size:12px;margin:2px 0 10px}
.rn-more{margin:12px 0 0;text-align:center}
.rn-up{color:var(--abre);font-weight:600}.rn-dn{color:var(--fecha)}
.rn-src{font-size:10.5px;color:var(--muted);margin-left:4px}
</style>
"""
inner = rep(inner, '<div id="app"></div>', CSS + '\n<div id="app"></div>')

JS = r"""
/* ── Aviso de fonte: pregão reconstruído a partir do run da Ativa ───── */
const RA = D.runAtiva || {};
function bannerFonte(){
  const f = RA.fonte; if (!f) return "";
  const fams = {DI_SPREAD:"DI+", DI_PCT:"% do DI", IPCA:"IPCA+", IGPM:"IGP-M"};
  const sem = (f.familias_sem_run||[]).map(x=>fams[x]||x);
  return `<div class="fonte-av"><b>ANBIMA de debêntures indisponível desde ${dataBR(f.anbima_ate)}.</b>
    Este painel é de ${dataBR(D.data)} e foi reconstruído com o run da Ativa de ${dataBR(f.email)}:
    ${f0(f.n_anbima_run)} papéis pela coluna ANBIMA do run (a taxa indicativa de D-1),
    ${f0(f.n_mid)} pelo meio entre compra e venda e ${f0(f.n_repetida)} com a última taxa conhecida —
    esses dois últimos grupos não geram variação nem sinal de movimento.
    ${sem.length ? `Sem run de ${sem.join(", ")} neste dia: taxas repetidas. ` : ""}
    A NTN-B não chega pelo run: foi estimada pelo movimento mediano das debêntures IPCA+ de cada vértice
    (${sgn(f.ntnb_desloc_mediano_bps)} bps na mediana desde ${dataBR(f.ntnb_base)}), então a mediana de
    spread IPCA+ destes dias não mede o mercado — só a posição de cada papel contra os pares.
    PU, liquidez e CDI são de ${dataBR(f.pu_de)}.</div>`;
}

/* ── Run da mesa (Ativa) × ANBIMA ─────────────────────────────────────── */
const rnEstado = {u:"todos", ord:"oferta", q:"", soOferta:true, aberto:false};
const RN_U = {todos:"todos", DI_SPREAD:"deb. DI+", DI_PCT:"deb. % do DI", IPCA:"deb. IPCA+", CRI:"CRI", CRA:"CRA"};
const RN_PAP = new Map(D.papeis.map(p=>[p.codigo,p]));
const RN_CC = new Map(CC.map(r=>[r.cod,r]));
function rnLinhas(){
  const rows = (RA.run && RA.run.rows) || [];
  return rows.map(x=>{
    const deb = x.tp==="DEB";
    const p = deb ? RN_PAP.get(x.cod) : null, c = deb ? null : RN_CC.get(x.cod);
    // referência: a marcação do painel quando existe; senão a coluna ANBIMA do run (D-1)
    let ref = null, refSrc = null;
    if (p && p.taxa_indicativa!=null){ ref = p.taxa_indicativa; refSrc = "painel"; }
    else if (c && c.tx!=null){ ref = c.tx; refSrc = "ANBIMA CRI/CRA"; }
    else if (x.anbima!=null){ ref = x.anbima; refSrc = deb ? "run" : "ANBIMA CRI/CRA"; }
    // diferença em bps de taxa; em % do DI converte pelo CDI
    const k = x.fam==="DI_PCT" ? (D.cdi||0)/100 : 1;
    const bp = v => (v==null || ref==null) ? null : Math.round((v-ref)*100*k*10)/10;
    return {...x, p, c, ref, refSrc, dOf: bp(x.venda), dCp: bp(x.compra),
      ba: (x.compra!=null && x.venda!=null) ? Math.round((x.compra-x.venda)*100*k*10)/10 : null,
      spread: p ? p.spread_bps : (c ? c.spread : null), liq: p ? p.liquidez : null,
      u: deb ? x.fam : x.tp};
  });
}
function rnPassa(r){
  if (rnEstado.u!=="todos" && r.u!==rnEstado.u) return false;
  if (rnEstado.soOferta && r.venda==null) return false;
  const q = rnEstado.q.trim().toUpperCase();
  if (q && !(r.cod.includes(q) || semAcento(r.emissor).includes(semAcento(q)))) return false;
  return true;
}
function rnOrdena(xs){
  const k = {oferta:"dOf", compra:"dCp", ba:"ba", spread:"spread", dur:"dur"}[rnEstado.ord];
  const asc = rnEstado.ord==="compra";
  return xs.slice().sort((a,b)=>{ const x=a[k], y=b[k];
    if (x==null) return 1; if (y==null) return -1; return asc ? x-y : y-x; });
}
function rnTx(v, fam){ return v==null ? "—" : fmtTaxa(v, fam==="OUTRO"?"PRE":fam, true); }
function rnRow(r){
  const tags = (r.isenta?`<span class="cc-tp" title="debênture incentivada (seção Isentas do run)">isenta</span>`:"")
    + (r.tp!=="DEB"?`<span class="cc-tp">${esc(r.tp)}</span>`:"")
    + (RA.fontePapel && RA.fontePapel[r.cod] ? `<span class="cc-tp" title="taxa do painel neste papel: ${RA.fontePapel[r.cod]==="mid_run"?"meio do run":"repetida do último dado"}">${RA.fontePapel[r.cod]==="mid_run"?"mid":"rep."}</span>` : "");
  const dd = v => v==null ? "—" : `<span class="${v>0?"rn-up":v<0?"rn-dn":""}">${sgn(v)}</span>`;
  return `<tr${r.p?` data-i="${D.papeis.indexOf(r.p)}"`:r.c?` data-cc="${CC.indexOf(r.c)}"`:""}>
    <td class="l"><span class="cod">${esc(r.cod)}</span>${tags}${r.rating?`<span class="rn-src">${esc(r.rating)}</span>`:""}
      <span class="emi" title="${esc(r.emissor)}">${esc(r.emissor)}</span></td>
    <td class="l"><span class="fam"><span class="dot" style="background:${CORF[r.fam]||"var(--neutro)"}"></span>${esc(r.idx||"")}</span></td>
    <td>${r.dur!=null?f1(r.dur):(r.c&&r.c.dur!=null?f1(r.c.dur):"—")}</td>
    <td class="mono">${rnTx(r.ref, r.fam)}<span class="rn-src">${r.refSrc==="run"?"run":""}</span></td>
    <td class="mono">${r.so_pu?"PU":rnTx(r.compra, r.fam)}</td>
    <td class="mono">${r.so_pu?"PU":rnTx(r.venda, r.fam)}</td>
    <td><b>${dd(r.dOf)}</b></td>
    <td>${dd(r.dCp)}</td>
    <td>${r.ba==null?"—":f0(r.ba)}</td>
    <td class="mono" style="color:var(--muted)">${esc(r.vol||"—")}</td>
    <td>${r.spread==null?"—":f0(r.spread)}</td>
    <td>${r.liq==null?"—":medidorLiq(r.liq)}</td>
  </tr>`;
}
function rnRender(){
  const all = rnOrdena(rnLinhas().filter(rnPassa));
  const vis = rnEstado.aberto ? all : all.slice(0,25);
  const cnt = document.getElementById("rnCnt");
  if (cnt) cnt.textContent = `${all.length} papéis · mostrando ${vis.length}`;
  document.getElementById("rnTb").innerHTML = vis.map(rnRow).join("")
    || `<tr><td class="l" colspan="12" style="padding:18px;color:var(--muted)">Nada com esses filtros.</td></tr>`;
  const m = document.getElementById("rnMore");
  m.innerHTML = all.length>25 ? `<button type="button" class="btn" id="rnMb" aria-expanded="${rnEstado.aberto}">${rnEstado.aberto?"▲ recolher":"▼ ver todos ("+(all.length-25)+" restantes)"}</button>` : "";
  const b = document.getElementById("rnMb"); if (b) b.onclick = () => { rnEstado.aberto=!rnEstado.aberto; rnRender(); };
}
function secaoRun(){
  const R = RA.run; if (!R || !R.rows || !R.rows.length) return "";
  const dt = R.datas || {};
  const us = Object.keys(RN_U).filter(u=>u==="todos" || R.rows.some(x=>(x.tp==="DEB"?x.fam:x.tp)===u));
  return `<section>
    <div class="head"><h2>Run da mesa — Ativa × ANBIMA</h2>
      <span class="eyebrow">run Ativa · CDI ${dataBR(dt.CDI)} · IPCA ${dataBR(dt.IPCA)} · CRI/CRA ${dataBR(dt.CRICRA)}</span></div>
    <p class="note">Compra e venda que a mesa da Ativa mostrou no run do dia, ao lado da <b>marcação
    ANBIMA</b> que o painel usa (debênture: taxa indicativa de ${dataBR(D.data)}; CRI/CRA: ANBIMA de
    ${CC.length?dataBR(D.cricra.ref):"—"}; sem marcação no painel, a coluna ANBIMA do próprio run,
    marcada <span class="rn-src">run</span>). <b>Oferta − ANBIMA</b> é a taxa de venda menos a
    marcação, em bps: positivo quer dizer que a mesa <b>vende acima da marcação</b>, o papel sai mais
    barato do que a tela ANBIMA sugere. <b>Compra − ANBIMA</b> negativo: a mesa compra com taxa abaixo
    da marcação, ou seja, <b>paga mais caro</b> que a tela — sinal de procura pelo papel. Em % do DI a diferença é convertida pelo CDI. É cotação indicativa de uma
    mesa, com o volume ao lado — não é negócio fechado nem recomendação.</p>
    <div class="rn-bar">
      <label class="rn-f">universo <select id="rnU">${us.map(u=>`<option value="${u}">${RN_U[u]}</option>`).join("")}</select></label>
      <label class="rn-f">ordenar <select id="rnO">
        <option value="oferta">oferta mais acima da ANBIMA</option>
        <option value="compra">compra mais abaixo da ANBIMA (procura)</option>
        <option value="ba">maior bid-ask</option>
        <option value="spread">maior spread</option>
        <option value="dur">maior duration</option></select></label>
      <label class="chk"><input type="checkbox" id="rnSo" checked> só com oferta de venda</label>
      <input type="search" id="rnQ" placeholder="código ou emissor" aria-label="Buscar no run">
    </div>
    <div class="rn-cnt" id="rnCnt"></div>
    <div class="tw"><table><thead><tr>
      <th class="l">Papel</th><th class="l">Indexador</th><th>Dur.</th>
      <th title="marcação ANBIMA de referência">ANBIMA</th>
      <th title="taxa a que a mesa compra">Compra</th><th title="taxa a que a mesa vende">Venda</th>
      <th title="venda menos ANBIMA, bps">Oferta − ANB.</th><th title="compra menos ANBIMA, bps">Compra − ANB.</th>
      <th title="compra menos venda, bps">Bid-ask</th><th>Volume</th>
      <th title="spread do painel, bps">Spread</th><th>Liquidez</th>
    </tr></thead><tbody id="rnTb"></tbody></table></div>
    <div class="rn-more" id="rnMore"></div>
  </section>`;
}
function initRun(){
  if (!document.getElementById("rnTb")) return;
  const U=document.getElementById("rnU"), O=document.getElementById("rnO"),
        S=document.getElementById("rnSo"), Q=document.getElementById("rnQ");
  U.onchange=e=>{rnEstado.u=e.target.value; rnEstado.aberto=false; rnRender();};
  O.onchange=e=>{rnEstado.ord=e.target.value; rnEstado.aberto=false; rnRender();};
  S.onchange=e=>{rnEstado.soOferta=e.target.checked; rnRender();};
  let t; Q.oninput=e=>{clearTimeout(t); t=setTimeout(()=>{rnEstado.q=e.target.value; rnRender();},140);};
  rnRender();
}
"""
inner = rep(inner, "function montar(){", JS + "\nfunction montar(){")
inner = rep(inner, "    </div></header>\n", "    </div></header>\n    ${bannerFonte()}\n")
inner = rep(inner, "    ${secaoParalelo()}", "    ${secaoParalelo()}\n\n    ${secaoRun()}")
inner = rep(inner, "  initGestoras();\n", "  initGestoras();\n  initRun();\n")

open(OUT, "w", encoding="utf-8").write(inner)
i = inner.index("const D = ") + len("const D = ")
D2, _ = json.JSONDecoder().raw_decode(inner[i:])
print("run:", len((D2["runAtiva"]["run"] or {}).get("rows", [])), "linhas | fallback:",
      bool(D2["runAtiva"]["fonte"]), "| bytes:", len(inner.encode()))
