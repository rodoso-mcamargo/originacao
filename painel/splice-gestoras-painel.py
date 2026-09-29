# -*- coding: utf-8 -*-
import json, pathlib
AQUI = pathlib.Path(__file__).resolve().parent
import sys
SRC = sys.argv[1]           # HTML "inner" do gerador + splice de destaques
OUT = sys.argv[2]
inner = open(SRC, encoding="utf-8").read()
assert inner.startswith("<meta charset")

# ── Ideias descartadas ────────────────────────────────────────────────
# Emissores retirados da triagem de stress do topo (fonte da verdade).
# Cada nome deve ser a string EXATA do campo emissor em D.papeis.
# Ao descartar: adicione o nome aqui e rode o splice de novo.
DESCARTADOS = [
]
# tira os descartados da fila da triagem (o proximo emissor sobe p/ manter 15)
_tri_line = "if (!p.emissor || p.spread_bps==null) return;"
assert inner.count(_tri_line)==1, "linha triagem nao unica"
inner = inner.replace(_tri_line, _tri_line+"\n    if (DESCARTADOS.includes(p.emissor)) return;", 1)
# insere a secao de registro logo antes do rodape (ultimo bloco da pagina)
_foot = "\n    <footer>"
assert inner.count(_foot)==1, "ancora footer nao unica"
inner = inner.replace(_foot, "\n    ${secaoDescartadas()}\n"+_foot, 1)

# dados: so credito privado, enxuto — agora com fluxo 3m/6m (flow json)
d = json.load(open(AQUI/"gestoras-flow.json", encoding="utf-8"))
CRED={"Debênture","Debênture ilíq.","CRA","Certificado de recebíveis imobiliários","FIDC","Letra Financeira","Nota Promissória/ Commercial Paper/ Export Note","CDB/ RDB","DPGE","Outros Certificados de Recebíveis"}
rows=[r for r in d["rows"] if r["tipo"] in CRED]
def flm(f):  # enxuga o dict de fluxo
    return None if not f else {"s":f["st"],"p":f.get("pct"),"v":f.get("prev")}
slim=[{"g":r["g"],"tipo":r["tipo"],"ent":r["ent"],"tk":r["tk"],"vl":r["vl"],"ncods":r["ncods"],
       "spread":r["spread"],"d21":r["d21"],"v3":r["v3"],"rat":r["rat"],
       "f3":flm(r.get("f3")),"f6":flm(r.get("f6")),"x":1 if r.get("saiu") else 0} for r in rows]
# total por gestora so das posicoes ATUAIS (nao das saidas x=1)
tot_por_g={}
for r in slim:
    if not r["x"]: tot_por_g[r["g"]]=tot_por_g.get(r["g"],0)+r["vl"]
gest=sorted(tot_por_g, key=lambda g:-tot_por_g[g])
gcart={"mes":d["mes"],"mes3":d.get("mes3"),"mes6":d.get("mes6"),"ref":d["ref_radar"],"gestoras":gest,"rows":slim}
PAY=json.dumps(gcart,ensure_ascii=False,separators=(",",":"))
print("linhas credito embutidas:",len(slim),"| gestoras:",len(gest))

# injeta ,"gcart":{...} no D (antes do array principal de papeis)
anchor='"papeis":[{"codigo"'
assert inner.count(anchor)==1, "anchor papeis nao unico: "+str(inner.count(anchor))
inner = inner.replace(anchor, '"gcart":'+PAY+','+anchor, 1)

# CRI/CRA agora é nativo no template/gerador — nada a embutir aqui.

CSS = """
<style>
/* carteira de credito das gestoras */
.gc-bar{display:flex;gap:9px 14px;flex-wrap:wrap;align-items:center;margin:14px 0 8px}
.gc-f{display:flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.gc-cnt{color:var(--muted);font-size:12px;margin:2px 0 10px}
.gc-tp{font-size:11px;color:var(--ink-2)}.gc-tp.il{color:var(--aviso);font-weight:600}
.gc-il{color:var(--abre)}.gc-op{color:var(--abre);font-weight:600}
.gc-tk{font-family:"IBM Plex Mono",monospace;color:var(--muted);font-size:11px;margin-left:5px}
.gc-more{margin:14px 0 0;text-align:center}
/* fluxo 3m/6m */
.gc-flowc{display:flex;gap:4px 5px;align-items:center;flex-wrap:wrap;white-space:nowrap}
.gc-fllb{font-size:9px;letter-spacing:.04em;color:var(--muted);font-weight:600}
.gc-fl{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:600;padding:1px 4px;border-radius:4px;line-height:1.5}
.gc-fl-up{color:var(--abre);background:color-mix(in srgb,var(--abre) 12%,transparent)}
.gc-fl-dn{color:var(--fecha);background:color-mix(in srgb,var(--fecha) 12%,transparent)}
.gc-fl-in{color:var(--abre);background:color-mix(in srgb,var(--abre) 14%,transparent)}
.gc-fl-out{color:var(--aviso);background:color-mix(in srgb,var(--aviso) 16%,transparent)}
.gc-fx{font-size:10.5px;color:var(--muted)}
.gc-xrow td{opacity:.72}
</style>
"""
inner = inner.replace('<div id="app"></div>', CSS+'\n<div id="app"></div>', 1)

JS = r"""
/* ── Carteira de credito das gestoras (CDA/CVM) ───────────────────────── */
const GC_CRED_TNICE={"Debênture":"Debênture","Debênture ilíq.":"Deb. ilíq.","Certificado de recebíveis imobiliários":"CRI","CRA":"CRA","FIDC":"FIDC","Letra Financeira":"Letra Fin.","Nota Promissória/ Commercial Paper/ Export Note":"Nota com.","CDB/ RDB":"CDB","DPGE":"DPGE","Outros Certificados de Recebíveis":"Out. CR"};
const gcEstado={ord:"vl",asc:false,g:"",tipo:"todos",classe:"todos",aberto:false};
function gcDados(){ return (D.gcart&&D.gcart.rows)?D.gcart.rows:[]; }
function gcBrl(v){ return v==null?"—":v>=1e6?(v/1e6).toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})+" tri":v>=1e3?(v/1e3).toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})+" bi":f0(v)+" mi"; }
function gcPassa(r){
  if(gcEstado.g && r.g!==gcEstado.g) return false;
  if(gcEstado.tipo!=="todos" && r.tipo!==gcEstado.tipo) return false;
  const c=gcEstado.classe;
  // saidas (x=1) ficam escondidas por padrao — so aparecem no filtro "saiu"
  if(c!=="saiu" && r.x) return false;
  if(c==="todos") return true;
  if(c==="acao") return !!r.tk;
  if(c==="caiu") return r.v3!=null&&r.v3<0;
  if(c==="abrindo") return r.d21!=null&&r.d21>0;
  if(c==="rating") return r.rat&&r.rat.length;
  if(c==="iliq") return r.tipo==="Debênture ilíq.";
  if(c==="entrou") return r.f6&&r.f6.s==="entrou";
  if(c==="aumentou") return r.f6&&(r.f6.s==="aumentou"||r.f6.s==="entrou");
  if(c==="reduziu") return r.f6&&r.f6.s==="reduziu";
  if(c==="saiu") return !!r.x;
  return true;
}
function gcMovVal(r){ // magnitude p/ ordenar por variacao 6m
  const f=r.f6; if(!f)return -1e9;
  if(f.s==="entrou")return 1e8; if(f.s==="saiu")return -1e8;
  return f.p==null?-1e9:f.p;
}
function gcKey(r){
  const o=gcEstado.ord;
  if(o==="v3")return r.v3==null?9999:r.v3;
  if(o==="d21")return r.d21==null?-1e9:r.d21;
  if(o==="spread")return r.spread==null?-1e9:r.spread;
  if(o==="mov6")return gcMovVal(r);
  if(o==="g")return r.g; if(o==="ent")return r.ent;
  return r.vl;
}
const GC_FLOW_ST={entrou:"entrou (nova no book profundo)",aumentou:"aumentou a posição",reduziu:"reduziu a posição",estavel:"posição estável",saiu:"saiu do book",sembase:"sem base no mês"};
function gcFlBadge(f){
  if(!f) return '<span class="gc-fx" title="sem comparação">·</span>';
  const s=f.s, tt=GC_FLOW_ST[s]||s;
  if(s==="sembase") return '<span class="gc-fx" title="mês-base indisponível">n/d</span>';
  if(s==="entrou") return '<span class="gc-fl gc-fl-in" title="'+tt+'">novo</span>';
  if(s==="saiu") return '<span class="gc-fl gc-fl-out" title="'+tt+(f.v?" (era "+gcBrl(f.v)+")":"")+'">saiu</span>';
  if(s==="estavel") return '<span class="gc-fx" title="'+tt+" ("+(f.p>0?"+":"")+f.p+'% no período)">=</span>';
  const up=s==="aumentou", cap=Math.abs(f.p)>=1000?(f.p>0?"+999":"-999"):((f.p>0?"+":"")+f.p);
  return '<span class="gc-fl '+(up?"gc-fl-up":"gc-fl-dn")+'" title="'+tt+" "+(f.p>0?"+":"")+f.p+"% (era "+gcBrl(f.v)+')">'+(up?"↑":"↓")+cap+'%</span>';
}
function gcFlowCell(r){
  return '<div class="gc-flowc"><span class="gc-fllb">3m</span>'+gcFlBadge(r.f3)+'<span class="gc-fllb">6m</span>'+gcFlBadge(r.f6)+'</div>';
}
function gcOrdena(xs){
  const s=xs.slice().sort((a,b)=>{let x=gcKey(a),y=gcKey(b);return typeof x==="string"?x.localeCompare(y,"pt-BR"):x-y;});
  let desc=(gcEstado.ord==="vl"||gcEstado.ord==="spread"||gcEstado.ord==="d21"||gcEstado.ord==="mov6");
  if(gcEstado.asc!==null&&["vl","v3","d21","spread","mov6"].includes(gcEstado.ord))desc=!gcEstado.asc;
  return desc?s.reverse():s;
}
function gcRatChip(r){
  if(!r.rat||!r.rat.length)return "";
  const a=r.rat.find(x=>x.url)||r.rat[0];
  const tit=r.rat.map(x=>`${x.ag}: ${(x.acao||"").replace(/_/g," ")} — ${x.res}`).join(" · ");
  return a.url?`<a class="selo baixa" href="${esc(a.url)}" target="_blank" rel="noopener" title="${esc(tit)}" style="text-decoration:none">rating ↗</a>`:`<span class="selo baixa" title="${esc(tit)}">rating</span>`;
}
function gcRow(r){
  const il=r.tipo==="Debênture ilíq.";
  const v3=r.v3==null?'<span style="color:var(--muted)">—</span>':`<span class="${r.v3<0?"gc-il":""}" style="${r.v3<0?"font-weight:600":""}">${sgn(r.v3)}%</span>`;
  const d21=r.d21==null?"—":`<span class="${r.d21>0?"gc-op":""}">${r.d21>0?"+":""}${f0(r.d21)}${r.d21>0?"↑":""}</span>`;
  const pos=r.x?'<span style="color:var(--muted)">saiu</span>':gcBrl(r.vl);
  return `<tr class="${r.x?"gc-xrow":""}">
    <td class="l" style="font-weight:600;font-size:12px">${esc(r.g)}</td>
    <td class="l gc-tp ${il?"il":""}">${esc(GC_CRED_TNICE[r.tipo]||r.tipo)}</td>
    <td class="l"><span title="${esc(r.ent)}${r.tk?" · "+esc(r.tk):""}">${esc(r.ent)}</span>${r.tk?`<span class="gc-tk">${esc(r.tk)}</span>`:""}</td>
    <td class="mono" style="color:var(--muted)">${r.ncods||"—"}</td>
    <td class="mono" style="font-weight:600">${pos}</td>
    <td>${gcFlowCell(r)}</td>
    <td>${r.spread==null?"—":f0(r.spread)}</td>
    <td>${d21}</td>
    <td>${v3}</td>
    <td class="l">${gcRatChip(r)}</td></tr>`;
}
function gcRender(){
  const all=gcOrdena(gcDados().filter(gcPassa));
  const vis=gcEstado.aberto?all:all.slice(0,10), rest=all.length-vis.length;
  const soma=all.reduce((s,r)=>s+r.vl,0);
  const rotO={vl:"maior posição",v3:"maior queda 3m",d21:"maior Δ21d",spread:"maior spread",mov6:"maior variação 6m"}[gcEstado.ord]||"maior posição";
  const cnt=document.getElementById("gcCnt");
  if(cnt) cnt.textContent=`${all.length} posições${gcEstado.g?" · "+gcEstado.g:""}${gcEstado.tipo!=="todos"?" · "+(GC_CRED_TNICE[gcEstado.tipo]||gcEstado.tipo):""} · Σ ${gcBrl(soma)} · ordenado por ${rotO} · mostrando ${vis.length}`;
  document.getElementById("gcTb").innerHTML=vis.map(gcRow).join("")||`<tr><td class="l" colspan="10" style="padding:18px;color:var(--muted)">Nada com esses filtros.</td></tr>`;
  document.getElementById("gcMore").innerHTML=rest>0?`<button class="btn" id="gcMb">▼ ver mais (${rest})</button>`:(gcEstado.aberto&&all.length>10?`<button class="btn" id="gcMb">▲ recolher</button>`:"");
  const mb=document.getElementById("gcMb"); if(mb) mb.onclick=()=>{gcEstado.aberto=!gcEstado.aberto;gcRender();};
  document.querySelectorAll("#gcTab th[data-gs]").forEach(t=>t.setAttribute("aria-sort", t.dataset.gs===gcEstado.ord?(gcEstado.asc?"ascending":"descending"):"none"));
}
function secaoGestoras(){
  if(!gcDados().length) return "";
  const tipos=[...new Set(gcDados().map(r=>r.tipo))];
  const g=D.gcart;
  return `<section>
    <div class="head"><h2>Carteira de crédito das gestoras</h2>
      <span class="eyebrow">CDA/CVM ${esc(g.mes)} · ${g.gestoras.length} casas · quem carrega o quê</span></div>
    <p class="note">Posições de crédito das principais casas na composição de carteiras da CVM, por emissor — debêntures (líquidas e <b>ilíquidas</b>), CRI, CRA, FIDC, letra financeira e nota. Nas debêntures, ao lado, o spread, a abertura de 21 pregões (Δ21d), a queda da ação e a ação de rating do mesmo emissor. A coluna <b>Fluxo</b> compara esta foto (${esc(g.mes)}) com <b>3 e 6 meses antes</b> (${esc(g.mes3||"—")} e ${esc(g.mes6||"—")}): <span class="gc-fl gc-fl-up">↑</span> aumentou, <span class="gc-fl gc-fl-dn">↓</span> reduziu, <span class="gc-fl gc-fl-in">novo</span> entrou, <span class="gc-fl gc-fl-out">saiu</span> zerou, <b>=</b> estável (±15%). A comparação usa o book profundo (top-250) de cada mês, então entrada/saída é real, não ruído da borda do corte; "saiu" só para emissor nomeado. A CDA tem defasagem estrutural (~6-7 meses) — é fluxo de <b>quem é dono</b>, não do pregão de hoje. A tabela lista as <b>60 maiores posições por gestora</b>; a ausência de um papel não significa que a casa não o carrega.</p>
    <div class="gc-bar">
      <label class="gc-f">gestora <select id="gcG"><option value="">todas (${g.gestoras.length})</option>${g.gestoras.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join("")}</select></label>
      <label class="gc-f">tipo <select id="gcT"><option value="todos">crédito (todos)</option>${tipos.map(t=>`<option value="${esc(t)}">${esc(GC_CRED_TNICE[t]||t)}</option>`).join("")}</select></label>
      <label class="gc-f">classificar <select id="gcC">
        <option value="todos">todas</option>
        <optgroup label="movimento (6m)"><option value="entrou">entrou / novo</option><option value="aumentou">aumentou</option><option value="reduziu">reduziu</option><option value="saiu">saiu (zerou)</option></optgroup>
        <optgroup label="mercado"><option value="acao">com ação listada</option><option value="caiu">ação caiu (3m&lt;0)</option><option value="abrindo">spread abrindo (Δ21d↑)</option><option value="rating">com ação de rating</option><option value="iliq">só ilíquidas</option></optgroup></select></label>
      <label class="gc-f">ordenar <select id="gcO">
        <option value="vl">maior posição</option><option value="mov6">maior variação 6m</option><option value="v3">maior queda 3m</option>
        <option value="d21">maior Δ21d</option><option value="spread">maior spread</option></select></label>
    </div>
    <div class="gc-cnt" id="gcCnt"></div>
    <div class="tw"><table id="gcTab"><thead><tr>
      <th class="l" data-gs="g">Gestora</th><th class="l">Tipo</th><th class="l" data-gs="ent">Emissor / papel</th>
      <th data-gs="ncods">Séries</th><th data-gs="vl">Posição</th><th data-gs="mov6">Fluxo 3m·6m</th><th data-gs="spread">Spread</th>
      <th data-gs="d21">Δ21d</th><th data-gs="v3">Ação 3m</th><th class="l">Rating</th>
    </tr></thead><tbody id="gcTb"></tbody></table></div>
    <div class="gc-more" id="gcMore"></div>
  </section>`;
}
function initGestoras(){
  if(!gcDados().length) return;
  const G=document.getElementById("gcG"),T=document.getElementById("gcT"),C=document.getElementById("gcC"),O=document.getElementById("gcO");
  if(G)G.onchange=e=>{gcEstado.g=e.target.value;gcEstado.aberto=false;gcRender();};
  if(T)T.onchange=e=>{gcEstado.tipo=e.target.value;gcEstado.aberto=false;gcRender();};
  if(C)C.onchange=e=>{gcEstado.classe=e.target.value;gcEstado.aberto=false;gcRender();};
  if(O)O.onchange=e=>{gcEstado.ord=e.target.value;gcEstado.asc=null;gcEstado.aberto=false;gcRender();};
  document.querySelectorAll("#gcTab th[data-gs]").forEach(t=>t.onclick=()=>{const s=t.dataset.gs;if(gcEstado.ord===s){gcEstado.asc=!gcEstado.asc;}else{gcEstado.ord=s;gcEstado.asc=(s==="g"||s==="ent");}const o=document.getElementById("gcO");if(o)o.value=["vl","v3","d21","spread","mov6"].includes(gcEstado.ord)?gcEstado.ord:"vl";gcRender();});
  gcRender();
}
/* ── Ideias descartadas (registro) ─────────────────────────────────── */
function secaoDescartadas(){
  if(!DESCARTADOS.length){
    return `<section style="margin-top:34px">
      <div class="head"><h2>Ideias descartadas</h2><span class="eyebrow">registro</span></div>
      <p class="note" style="color:var(--muted)">Nenhum nome descartado ainda. Quando você pedir para tirar um emissor da triagem de stress do topo, ele sai da fila (o próximo sobe no lugar) e fica registrado aqui.</p>
    </section>`;
  }
  const linhas = DESCARTADOS.map((nome,i)=>{
    const ps = D.papeis.filter(p=>p.emissor===nome && p.spread_bps!=null)
                       .sort((a,b)=>(b.spread_bps||0)-(a.spread_bps||0));
    const p = ps[0];
    return `<tr>
      <td class="mov-rk">${i+1}</td>
      <td class="l"><span class="cod">${esc(nome)}</span>${p?`<span class="emi">papel de referência ${esc(p.codigo)}</span>`:`<span class="emi" style="color:var(--muted)">sem papel com spread hoje</span>`}</td>
      <td class="mono">${p?f0(p.spread_bps):"—"}</td>
      <td>${p&&p.d_spread_21d!=null?`<span style="color:${p.d_spread_21d>0?"var(--abre)":"var(--fecha)"}">${sgn(p.d_spread_21d)}</span>`:"—"}</td>
    </tr>`;
  }).join("");
  return `<section style="margin-top:34px">
    <div class="head"><h2>Ideias descartadas</h2>
      <span class="eyebrow">${DESCARTADOS.length} ${DESCARTADOS.length===1?"nome":"nomes"} · registro</span></div>
    <p class="note">Nomes retirados da triagem de stress do topo por decisão de quem acompanha. Ficam aqui <b>só como registro</b> — não entram mais na fila do topo, e o próximo emissor sobe no lugar para a lista seguir com ${TRIAGEM_N}. O spread ao lado é o do papel de referência hoje, apenas para contexto.</p>
    <div class="tw"><table><thead><tr><th class="mov-rk">#</th><th class="l">Emissor</th><th>Spread</th><th>Δ21d</th></tr></thead>
      <tbody>${linhas}</tbody></table></div>
  </section>`;
}
"""
JS = "const DESCARTADOS = "+json.dumps(DESCARTADOS, ensure_ascii=False)+";\n"+JS
inner = inner.replace("function montar(){", JS+"\nfunction montar(){", 1)
inner = inner.replace("    ${secaoRatings()}", "    ${secaoRatings()}\n\n    ${secaoGestoras()}", 1)
inner = inner.replace("  initDestaques();\n}", "  initDestaques();\n  initGestoras();\n}", 1)

out=pathlib.Path(OUT)
out.write_text(inner,encoding="utf-8")
# valida D
i2=inner.index("const D = ")+len("const D = ")
depth=0;instr=False;esc=False;s0=i2;end=None
for j in range(i2,len(inner)):
    c=inner[j]
    if instr:
        if esc:esc=False
        elif c=="\\":esc=True
        elif c=='"':instr=False
    else:
        if c=='"':instr=True
        elif c=="{":depth+=1
        elif c=="}":
            depth-=1
            if depth==0:end=j+1;break
D2=json.loads(inner[s0:end])
print("D reparseia OK; gcart.rows:",len(D2["gcart"]["rows"]))
print("checks:", "secaoGestoras" in inner, "initGestoras()" in inner, "${secaoGestoras()}" in inner)
print("bytes inner:", len(inner))
