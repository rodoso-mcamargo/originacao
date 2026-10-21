# -*- coding: utf-8 -*-
"""Secao "Taxa acima de DI+5% e IPCA+10%" (debentures e CRI/CRA) sobre o HTML final.

    python3 splice-taxa-alta-painel.py radar-final.html radar-final.html

Aplicar DEPOIS de splice-agd-painel.py e ANTES de splice-descartados-final.py
(que continua sendo o ultimo: ele limpa D.papeis e D.cricra.rows, que esta secao le).
Pedido do rodrigo em 20/10/2026. So JS/CSS: le D.papeis e D.cricra.rows em tempo de
execucao, nao mexe no payload.
  Debentures: DI+ com taxa indicativa >= 5,00% (spread sobre o CDI) ou IPCA+ com taxa
  real >= 10,00%. Seletor de criterio: taxa de hoje, taxa de emissao, ou qualquer das
  duas. Sem filtro de liquidez (credito estressado e o ponto), liquidez mostrada.
  CRI/CRA: mesma regua e mesmo seletor; a taxa de emissao sai do campo de indexador
  da ANBIMA ("IPCA + 6,5%", "DI + 1,5%" -> 6,5 / 1,5; "+ 0%" fica sem taxa, e
  remuneracao nao informada). % do DI e prefixado ficam fora.
"""
import json, pathlib, sys

SRC, OUT = sys.argv[1], sys.argv[2]
s = open(SRC, encoding="utf-8").read()
CORTE = {"DI_SPREAD": 5.0, "IPCA": 10.0}


def rep(t, a, b):
    assert t.count(a) == 1, f"ancora nao unica ({t.count(a)}): {a[:70]!r}"
    return t.replace(a, b, 1)


CSS = """
<style>
/* taxa acima de DI+5% / IPCA+10% */
.ta-bar{display:flex;gap:9px 14px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
.ta-f{display:flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.ta-cnt{color:var(--muted);font-size:12px;margin:2px 0 10px}
.ta-more{margin:12px 0 0;text-align:center}
.ta-hi{color:var(--abre);font-weight:600}
.ta-sub{margin-top:26px}
.ta-sub h3{font-size:14px;font-weight:600;margin:0 0 8px}
</style>
"""
s = rep(s, '<div id="app"></div>', CSS + '\n<div id="app"></div>')

JS = "const TA_CORTE = " + json.dumps(CORTE) + ";\n" + r"""
/* ── Taxa acima de DI+5% e IPCA+10% (pedido de 20/10/2026) ───────────── */
const TA_ROT = {DI_SPREAD:"DI +", IPCA:"IPCA +"};
const taEstado = {crit:"hoje", fam:"todas", aberto:false, abertoCC:false};
function taPassa(fam, v){ return v!=null && TA_CORTE[fam]!=null && v >= TA_CORTE[fam]; }
function taDeb(){
  return D.papeis.filter(p=>{
    if (!TA_CORTE[p.familia]) return false;
    if (taEstado.fam!=="todas" && p.familia!==taEstado.fam) return false;
    const h = taPassa(p.familia, p.taxa_indicativa), e = taPassa(p.familia, p.taxa_emissao);
    return taEstado.crit==="hoje" ? h : taEstado.crit==="emissao" ? e : (h||e);
  }).sort((a,b)=> (a.familia<b.familia?-1:a.familia>b.familia?1:0) || ((b.taxa_indicativa??-99)-(a.taxa_indicativa??-99)));
}
function taEmiCC(r){
  const m = /^(?:IPCA|DI)\s*\+\s*([\d.,]+)\s*%/i.exec(r.idx||"");
  if (!m) return null;
  const v = parseFloat(m[1].replace(/\./g,"").replace(",","."));
  return isFinite(v) && v>0 ? v : null;
}
function taCC(){
  return CC.filter(r=>{
    if (!TA_CORTE[r.fam]) return false;
    if (taEstado.fam!=="todas" && r.fam!==taEstado.fam) return false;
    const h = taPassa(r.fam, r.tx), e = taPassa(r.fam, taEmiCC(r));
    return taEstado.crit==="hoje" ? h : taEstado.crit==="emissao" ? e : (h||e);
  }).sort((a,b)=> (a.fam<b.fam?-1:a.fam>b.fam?1:0) || ((b.tx??-99)-(a.tx??-99)));
}
function taTx(fam, v){ return v==null ? "—" : `<span class="${taPassa(fam,v)?"ta-hi":""}">${f2(v)}%</span>`; }
function taRowDeb(p){
  return `<tr data-i="${D.papeis.indexOf(p)}">
    <td class="l"><span class="cod">${esc(p.codigo)}</span><span class="emi">${esc(p.emissor)}</span></td>
    <td class="l"><span class="fam">${TA_ROT[p.familia]}</span></td>
    <td>${taTx(p.familia, p.taxa_indicativa)}</td>
    <td>${taTx(p.familia, p.taxa_emissao)}</td>
    <td>${p.taxa_indicativa!=null&&p.taxa_emissao!=null?sgn((p.taxa_indicativa-p.taxa_emissao)*100):"—"}</td>
    <td>${f0(p.spread_bps)}</td>
    <td>${p.d_spread_21d==null?"—":`<span class="${p.d_spread_21d>0?"rn-up":"rn-dn"}">${sgn(p.d_spread_21d)}</span>`}</td>
    <td>${p.pct_pu_par==null?"—":f1(p.pct_pu_par)+"%"}</td>
    <td>${f1(p.duration_anos)}</td>
    <td class="mono">${dataBR(p.vencimento)}</td>
    <td>${p.liquidez==null?"—":medidorLiq(p.liquidez)}</td>
  </tr>`;
}
function taRowCC(r){
  const em = taEmiCC(r);
  return `<tr data-cc="${CC.indexOf(r)}">
    <td class="l"><span class="cod">${esc(r.cod)}</span><span class="cc-tp">${esc(r.tp)}</span><span class="emi">${esc(r.dev)}</span></td>
    <td class="l"><span class="fam">${TA_ROT[r.fam]}</span></td>
    <td>${taTx(r.fam, r.tx)}</td>
    <td>${taTx(r.fam, em)}</td>
    <td>${r.tx!=null&&em!=null?sgn((r.tx-em)*100):"—"}</td>
    <td>${f0(r.spread)}</td>
    <td>${r.dj==null?"—":`<span class="${r.dj>0?"rn-up":"rn-dn"}">${sgn(r.dj)}</span>`}</td>
    <td>${r.par==null?"—":f1(r.par)+"%"}</td>
    <td>${f1(r.dur)}</td>
    <td class="mono">${dataBR(r.venc)}</td>
    <td>${r.score==null?"—":f0(r.score)}</td>
  </tr>`;
}
function taRender(){
  const deb = taDeb(), cc = taCC();
  const vd = taEstado.aberto ? deb : deb.slice(0,20), vc = taEstado.abertoCC ? cc : cc.slice(0,20);
  const nf = (l,k,f)=> l.filter(x=>x[k]===f).length;
  document.getElementById("taCnt").textContent =
    `${deb.length} debêntures (${nf(deb,"familia","DI_SPREAD")} DI+, ${nf(deb,"familia","IPCA")} IPCA+) · mostrando ${vd.length}`;
  document.getElementById("taCntCC").textContent =
    `${cc.length} CRI/CRA (${nf(cc,"fam","DI_SPREAD")} DI+, ${nf(cc,"fam","IPCA")} IPCA+) · mostrando ${vc.length}`;
  document.getElementById("taTb").innerHTML = vd.map(taRowDeb).join("")
    || `<tr><td class="l" colspan="11" style="padding:18px;color:var(--muted)">Nenhuma debênture acima do corte.</td></tr>`;
  document.getElementById("taTbCC").innerHTML = vc.map(taRowCC).join("")
    || `<tr><td class="l" colspan="11" style="padding:18px;color:var(--muted)">Nenhum CRI/CRA acima do corte.</td></tr>`;
  const bt = (id, n, ab, key) => {
    const m = document.getElementById(id);
    m.innerHTML = n>20 ? `<button type="button" class="btn" aria-expanded="${ab}">${ab?"▲ recolher":"▼ ver todos ("+(n-20)+" restantes)"}</button>` : "";
    const b = m.querySelector("button"); if (b) b.onclick = () => { taEstado[key]=!taEstado[key]; taRender(); };
  };
  bt("taMore", deb.length, taEstado.aberto, "aberto");
  bt("taMoreCC", cc.length, taEstado.abertoCC, "abertoCC");
}
function secaoTaxaAlta(){
  return `<section>
    <div class="head"><h2>Taxa acima de DI+5% e IPCA+10%</h2>
      <span class="eyebrow">ANBIMA · taxa indicativa de ${dataBR(D.data)} · debêntures e CRI/CRA</span></div>
    <p class="note">Papéis cuja taxa passa de <b>DI + 5,00%</b> (spread sobre o CDI) ou de <b>IPCA + 10,00%</b>
    (taxa real). Dá para escolher a régua: taxa indicativa de hoje, taxa de emissão, ou qualquer das duas — a
    coluna Δ mostra quanto a taxa andou desde a emissão, em bps. Nos CRI/CRA a taxa de emissão é a remuneração
    registrada na ANBIMA (campo de indexador, ex.: "IPCA + 6,5%"); papel sem remuneração informada fica com "—". Sem filtro de liquidez: crédito caro é justamente o que se quer ver, mas papel
    com liquidez baixa tem marcação menos confiável. % do DI e prefixado ficam de fora; descartados não aparecem.</p>
    <div class="ta-bar">
      <label class="ta-f">critério <select id="taCrit">
        <option value="hoje">taxa de hoje</option>
        <option value="emissao">taxa de emissão</option>
        <option value="qualquer">hoje ou emissão</option></select></label>
      <label class="ta-f">indexador <select id="taFam">
        <option value="todas">DI+ e IPCA+</option>
        <option value="DI_SPREAD">só DI+ (≥ 5%)</option>
        <option value="IPCA">só IPCA+ (≥ 10%)</option></select></label>
    </div>
    <div class="ta-cnt" id="taCnt"></div>
    <div class="tw"><table><thead><tr>
      <th class="l">Debênture</th><th class="l">Índice</th><th>Taxa hoje</th><th>Taxa emissão</th>
      <th title="taxa de hoje menos taxa de emissão, em bps">Δ emissão</th><th>Spread</th><th>Δ 21d</th>
      <th>% par</th><th>Duration</th><th>Vencimento</th><th>Liquidez</th>
    </tr></thead><tbody id="taTb"></tbody></table></div>
    <div class="ta-more" id="taMore"></div>
    <div class="ta-sub"><h3>CRI e CRA</h3>
    <div class="ta-cnt" id="taCntCC"></div>
    <div class="tw"><table><thead><tr>
      <th class="l">CRI/CRA e devedor</th><th class="l">Índice</th><th>Taxa hoje</th><th>Taxa emissão</th>
      <th title="taxa de hoje menos taxa de emissão, em bps">Δ emissão</th><th>Spread</th>
      <th>Δ janela</th><th>% par</th><th>Duration</th><th>Vencimento</th><th>Score</th>
    </tr></thead><tbody id="taTbCC"></tbody></table></div>
    <div class="ta-more" id="taMoreCC"></div></div>
  </section>`;
}
function initTaxaAlta(){
  if (!document.getElementById("taTb")) return;
  document.getElementById("taCrit").onchange = e => { taEstado.crit=e.target.value; taEstado.aberto=false; taEstado.abertoCC=false; taRender(); };
  document.getElementById("taFam").onchange = e => { taEstado.fam=e.target.value; taEstado.aberto=false; taEstado.abertoCC=false; taRender(); };
  taRender();
}
"""
s = rep(s, "function montar(){", JS + "\nfunction montar(){")
s = rep(s, "    ${secaoTriagemCC()}", "    ${secaoTaxaAlta()}\n\n    ${secaoTriagemCC()}")
s = rep(s, "  initAgd();\n", "  initAgd();\n  initTaxaAlta();\n")
open(OUT, "w", encoding="utf-8").write(s)

i = s.index("const D = ") + len("const D = ")
D, _ = json.JSONDecoder().raw_decode(s[i:])
deb = [p for p in D["papeis"] if p.get("familia") in CORTE and (p.get("taxa_indicativa") or -99) >= CORTE[p["familia"]]]
emi = [p for p in D["papeis"] if p.get("familia") in CORTE and (p.get("taxa_emissao") or -99) >= CORTE[p["familia"]]]
cc = [r for r in (D.get("cricra") or {}).get("rows", []) if r.get("fam") in CORTE and (r.get("tx") or -99) >= CORTE[r["fam"]]]
print(f"taxa alta (antes dos descartados): deb hoje {len(deb)} | deb emissao {len(emi)} | cri/cra {len(cc)} | bytes {len(s.encode())}")
