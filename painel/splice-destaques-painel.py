# -*- coding: utf-8 -*-
"""Aplica a camada 'Destaques — quedas de acao x credito' sobre o HTML gerado hoje.

Adaptado de claude/splice-destaques-painel.py: em vez de partir do HTML vivo
publicado, parte do arquivo que o gerador do projeto acabou de produzir (que ja
e o 'inner', sem o esqueleto que o publish adiciona).
"""
import json, statistics as st, pathlib
from collections import defaultdict

AQUI = pathlib.Path(__file__).resolve().parent
SRC = AQUI / "radar-secundario.html"
inner = SRC.read_text(encoding="utf-8")
assert inner.startswith("<meta charset"), inner[:40]

# D gerado
i = inner.index("const D = ") + len("const D = ")
depth = 0; instr = False; esc = False; start = i; end = None
for j in range(i, len(inner)):
    c = inner[j]
    if instr:
        if esc: esc = False
        elif c == "\\": esc = True
        elif c == '"': instr = False
    else:
        if c == '"': instr = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: end = j + 1; break
D = json.loads(inner[start:end])
papeis = D["papeis"]

u = json.load(open(AQUI / "dados/acoes/ultimo.json", encoding="utf-8"))
mapa = json.load(open(AQUI / "dados/acoes/mapa.json", encoding="utf-8"))["mapa"]

razoes = defaultdict(list); vinc = {}
for x in mapa:
    razoes[x["ticker"]].append(x["emissor"])
    if x.get("vinculo") == "propria" or x["ticker"] not in vinc:
        vinc[x["ticker"]] = x.get("vinculo", "propria")

stock = {}
for e in u["emissores"]:
    stock.setdefault(e["ticker"], e)
nome_emp = {a["ticker"]: a["empresa"] for a in u["acoes"]}

def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 1) if xs else None

rat_por = [(e["emissor"].upper(), e["achados"]) for e in D["ratings"]["emissores"] if e.get("achados")]
REL = {"rebaixamento", "perspectiva_negativa", "em_revisao", "elevacao"}

def rating_de(rz):
    for r in rz:
        k = r.upper()
        for nome, ach in rat_por:
            if nome == k or nome.startswith(k[:18]) or k.startswith(nome[:18]):
                out = [{"agencia": a["agencia"], "acao": a["acao"], "resumo": a["resumo"],
                        "url": a.get("url", "")} for a in ach if a.get("acao") in REL]
                if out: return out
    return []

destaques = []
for tk, rz in razoes.items():
    s = stock.get(tk)
    if not s or s.get("var_3m") is None: continue
    ups = {r.upper() for r in rz}
    ps = [p for p in papeis if p["emissor"].upper() in ups]
    if not ps: continue
    liq = [p for p in ps if (p.get("liquidez") or 0) >= 45]
    d21 = [p.get("d_spread_21d") for p in (liq or ps) if p.get("d_spread_21d") is not None]
    destaques.append({
        "ticker": tk, "empresa": nome_emp.get(tk, tk), "vinculo": vinc.get(tk, "propria"),
        "var_1m": s.get("var_1m"), "var_3m": s.get("var_3m"), "queda_max_3m": s.get("queda_max_3m"),
        "n_deb": len(ps), "spread_med": med([p.get("spread_bps") for p in ps]),
        "dspread21_max": round(max(d21), 1) if d21 else None,
        "dur_med": med([p.get("duration_anos") for p in ps]),
        "familias": sorted({p.get("familia") for p in ps if p.get("familia")}),
        "rating": rating_de(rz),
    })
destaques.sort(key=lambda x: x["var_3m"])

acoes_obj = dict(u)
acoes_obj["janela"] = "3m"
acoes_obj["destaques"] = destaques
acoes_json = json.dumps(acoes_obj, ensure_ascii=True, separators=(",", ":"))
print("destaques:", len(destaques), "| emissores:", len(acoes_obj["emissores"]),
      "| com rating:", sum(1 for d in destaques if d["rating"]))

def rep(s, a, b, n=1):
    assert s.count(a) >= 1, "NAO ACHOU: " + a[:70]
    return s.replace(a, b, n)

# o gerador ja carrega dados/acoes/ultimo.json; aqui o objeto e substituido pelo
# que tem tambem 'janela' e 'destaques'. Reserializa o bloco const D inteiro, que
# e mais seguro que casar texto do payload.
D["acoes"] = acoes_obj
inner = inner[:start] + json.dumps(D, ensure_ascii=True, separators=(",", ":")) + inner[end:]

inner = rep(inner, "const ACAO_FORTE = -40, ACAO_MODERADA = -20;",
                   "const ACAO_FORTE = -30, ACAO_MODERADA = -15;")
inner = rep(inner, "const ACAO_DD_FORTE = -50, ACAO_DD_MODERADA = -35;",
                   "const ACAO_DD_FORTE = -35, ACAO_DD_MODERADA = -25;")
inner = rep(inner, "  const v = a.var_12m, dd = a.queda_max_52s;",
                   "  const v = a.var_3m, dd = a.queda_max_3m;")
inner = rep(inner, "  if (a.var_12m != null) return `${sgn(a.var_12m)}% em 12m`;",
                   "  if (a.var_3m != null) return `${sgn(a.var_3m)}% em 3m`;")
inner = rep(inner, "  if (a.queda_max_52s != null) return `${sgn(a.queda_max_52s)}% da máx. 52s`;",
                   "  if (a.queda_max_3m != null) return `${sgn(a.queda_max_3m)}% da máx. 3m`;")
inner = rep(inner, "  p.acao_var = a.var_12m != null ? a.var_12m : a.queda_max_52s;",
                   "  p.acao_var = a.var_3m != null ? a.var_3m : a.queda_max_3m;")
inner = rep(inner, '{k:"acao_var", r:"Ação 12m", l:false,', '{k:"acao_var", r:"Ação 3m", l:false,')
inner = rep(inner,
'''        if (a.var_3m  != null) linhas.push(["3 meses",  `${sgn(a.var_3m)}%`]);
        if (a.var_6m  != null) linhas.push(["6 meses",  `${sgn(a.var_6m)}%`]);
        if (a.var_12m != null) linhas.push(["12 meses", `${sgn(a.var_12m)}%`]);
        if (a.queda_max_52s != null) linhas.push(["Da máx. 52s", `${sgn(a.queda_max_52s)}%`]);''',
'''        if (a.var_1m  != null) linhas.push(["1 mês",    `${sgn(a.var_1m)}%`]);
        if (a.var_3m  != null) linhas.push(["3 meses",  `${sgn(a.var_3m)}%`]);
        if (a.queda_max_3m != null) linhas.push(["Da máx. 3m", `${sgn(a.queda_max_3m)}%`]);''')
assert inner.count("(a.var_12m==null && a.queda_max_52s==null)") == 2
inner = inner.replace("(a.var_12m==null && a.queda_max_52s==null)",
                      "(a.var_3m==null && a.queda_max_3m==null)")
inner = rep(inner, "        const v = a.var_12m!=null ? a.var_12m : a.queda_max_52s;",
                   "        const v = a.var_3m!=null ? a.var_3m : a.queda_max_3m;")
inner = rep(inner, '          + (a.var_12m==null ? " · sem 12 meses de série, mostrando a queda desde a máxima de 52 semanas" : "")',
                   '          + (a.var_3m==null ? " · série de 3 meses" : "")')
# 4.9 cabecalho da tabela de triagem
inner = rep(inner, 'variação da ação do emissor em 12 meses — ou, quando a série é mais curta, a queda desde a máxima de 52 semanas.',
                   'variação da ação do emissor em 3 meses — ou, quando a série é mais curta, a queda desde a máxima de 3 meses.')
inner = rep(inner, 'apenas não pontua neste eixo">Ação 12m</th>', 'apenas não pontua neste eixo">Ação 3m</th>')
# 4.10 nota do rodape
inner = rep(inner, """      de 12 meses além de ${Math.abs(ACAO_FORTE)}%, ou de ${Math.abs(ACAO_DD_FORTE)}% desde a
      máxima de 52 semanas, pesa como rebaixamento de nota;""",
                   """      de 3 meses além de ${Math.abs(ACAO_FORTE)}%, ou de ${Math.abs(ACAO_DD_FORTE)}% desde a
      máxima de 3 meses, pesa como rebaixamento de nota;""")

CSS = """
<style>
/* destaques quedas x credito */
.dq-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.dq-tag{display:inline-flex;align-items:center;background:var(--ink);color:var(--surface);
  padding:6px 12px;font-size:12.5px;font-weight:600}
.dq-f{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px;
  letter-spacing:.06em;text-transform:uppercase;font-weight:600}
.dq-f.push{margin-left:auto}
.dq-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line)}
.dq-c{background:var(--surface);padding:11px 13px}
.dq-c.rt{box-shadow:inset 3px 0 0 var(--abre)}
.dq-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.dq-nm{font-size:13px;font-weight:600;line-height:1.2}
.dq-nm .tk{font-family:"IBM Plex Mono",monospace;color:var(--muted);font-weight:600;font-size:11px;margin-left:5px}
.dq-nm .ct{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
  border:1px solid var(--line-strong);padding:0 4px;margin-left:5px}
.dq-d{font-family:"IBM Plex Mono",monospace;font-size:17px;font-weight:700;color:var(--abre);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.dq-ch{display:flex;flex-wrap:wrap;gap:4px;margin-top:7px}
.dq-k{font-size:11px;padding:1px 6px;border:1px solid var(--line-strong);color:var(--ink-2);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.dq-k.stk{border-style:dashed;color:var(--muted)}
.dq-k.op{color:var(--abre);border-color:var(--abre);font-weight:600}
.dq-k.deb{color:var(--ink-2);background:var(--surface-2)}
.dq-sep{width:100%;height:0;border-top:1px dashed var(--line);margin:7px 0 1px}
.dq-rt{margin-top:8px;padding:6px 8px;background:var(--surface-2);border:1px solid var(--line);font-size:11px;line-height:1.4}
.dq-rt .rh{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:700;color:var(--abre)}
.dq-rt a{color:var(--ink-2);text-decoration:underline;text-underline-offset:2px}
.dq-rt .li{margin-top:2px}
.dq-more{margin:14px 0 0;text-align:center}
.dq-lo.op{color:var(--abre);font-weight:600}
.dq-cnt{color:var(--muted);font-size:12px;margin:2px 0 12px}
</style>
"""
inner = inner.replace('<div id="app"></div>', CSS + '\n<div id="app"></div>', 1)

JS = r"""
/* ── Destaques: quedas de acao x credito (janela 3m) ──────────────────── */
const dqEstado = {ord:"queda", filtro:"todos", aberto:false};
function dqDados(){ return (D.acoes && D.acoes.destaques) ? D.acoes.destaques : []; }
function dqFmt(v){ return v==null?"—":(v>0?"+":"")+v.toFixed(1); }
function dqAbrindo(l){ return l.dspread21_max!=null && l.dspread21_max>0; }
function dqFam(f){ return {DI_SPREAD:"DI+",DI_PCT:"%DI",IPCA:"IPCA+",IGPM:"IGPM+",PRE:"pré"}[f]||f; }
function dqPassa(l){
  const f=dqEstado.filtro;
  if(f==="todos") return true;
  if(f==="rating") return l.rating&&l.rating.length;
  if(f==="abrindo") return dqAbrindo(l);
  return (l.familias||[]).includes(f);
}
function dqOrdenar(xs){
  const g=x=>x.dspread21_max!=null?x.dspread21_max:-1e9;
  const s=x=>x.spread_med!=null?x.spread_med:-1e9;
  if(dqEstado.ord==="abrindo") return xs.sort((a,b)=>g(b)-g(a));
  if(dqEstado.ord==="spread")  return xs.sort((a,b)=>s(b)-s(a));
  return xs.sort((a,b)=>a.var_3m-b.var_3m);
}
function dqCard(l){
  const ct=l.vinculo==="controladora"?'<span class="ct" title="ticker da controladora — sinal mais fraco">ctrl</span>':"";
  const stk=[];
  if(l.var_1m!=null) stk.push(`<span class="dq-k stk" title="último mês (~21 pregões)">1m ${dqFmt(l.var_1m)}%</span>`);
  if(l.queda_max_3m!=null) stk.push(`<span class="dq-k stk" title="queda desde a máxima de 3 meses">máx ${dqFmt(l.queda_max_3m)}%</span>`);
  const c=[`<span class="dq-k deb">${l.n_deb} deb</span>`];
  if(l.familias&&l.familias.length) c.push(`<span class="dq-k">${l.familias.map(dqFam).join("·")}</span>`);
  if(l.spread_med!=null) c.push(`<span class="dq-k">spread ${f0(l.spread_med)}</span>`);
  if(l.dspread21_max!=null){const op=l.dspread21_max>0;
    c.push(`<span class="dq-k ${op?"op":""}" title="maior abertura de spread do emissor em 21 pregões">Δ21d ${op?"+":""}${f0(l.dspread21_max)}${op?"↑":""}</span>`);}
  if(l.dur_med!=null) c.push(`<span class="dq-k">${f1(l.dur_med)}a</span>`);
  let rt="";
  if(l.rating&&l.rating.length) rt=`<div class="dq-rt"><span class="rh">⚠ ação de rating</span>`+
    l.rating.map(r=>`<div class="li">${esc(r.agencia)} — ${esc((r.acao||"").replace("_"," "))}: ${esc(r.resumo)} ${r.url?`<a href="${esc(r.url)}" target="_blank" rel="noopener">fonte</a>`:""}</div>`).join("")+`</div>`;
  return `<div class="dq-c ${l.rating&&l.rating.length?"rt":""}"><div class="dq-h">
    <div class="dq-nm">${esc(l.empresa)}<span class="tk">${esc(l.ticker)}</span>${ct}</div>
    <div class="dq-d">${dqFmt(l.var_3m)}%</div></div>
    <div class="dq-ch">${stk.join("")}</div><div class="dq-sep"></div>
    <div class="dq-ch">${c.join("")}</div>${rt}</div>`;
}
function dqRow(l){
  const op=dqAbrindo(l);
  return `<tr>
    <td class="l"><span class="cod">${esc(l.ticker)}</span> <span class="emi" title="${esc(l.empresa)}">${esc(l.empresa)}</span></td>
    <td><b style="color:var(--abre)">${dqFmt(l.var_3m)}%</b></td>
    <td class="mono" style="color:var(--muted)">${dqFmt(l.var_1m)}%</td>
    <td>${l.spread_med!=null?f0(l.spread_med):"—"}</td>
    <td class="dq-lo ${op?"op":""}">${l.dspread21_max!=null?(op?"+":"")+f0(l.dspread21_max)+(op?"↑":""):"—"}</td>
    <td>${l.rating&&l.rating.length?'<span class="selo baixa">rating</span>':""}</td></tr>`;
}
function dqRender(){
  const all=dqOrdenar(dqDados().filter(dqPassa));
  const cards=all.slice(0,10), rest=all.slice(10);
  const rot={queda:"maior queda 3m",abrindo:"maior Δ21d",spread:"maior spread"}[dqEstado.ord];
  const cnt=document.getElementById("dqCnt");
  if(cnt) cnt.textContent=`${all.length} emissores${dqEstado.filtro!=="todos"?" (filtrado)":""} · ordenado por ${rot} · cartões: top ${Math.min(10,all.length)}`;
  document.getElementById("dqGrid").innerHTML=cards.map(dqCard).join("");
  document.getElementById("dqMore").innerHTML = rest.length
    ? `<button class="btn" id="dqBtn" aria-expanded="${dqEstado.aberto}">${dqEstado.aberto?"▲ recolher lista":"▼ ver lista completa ("+rest.length+" restantes)"}</button>` : "";
  const b=document.getElementById("dqBtn"); if(b) b.onclick=()=>{dqEstado.aberto=!dqEstado.aberto;dqRender();};
  document.getElementById("dqLista").innerHTML = (dqEstado.aberto&&rest.length)
    ? `<div class="tw" style="margin-top:12px"><table><thead><tr><th class="l">Papel</th><th>Ação 3m</th><th>1m</th><th>Spread</th><th>Δ21d</th><th>Rating</th></tr></thead><tbody>${rest.map(dqRow).join("")}</tbody></table></div>` : "";
}
function secaoDestaques(){
  if(!dqDados().length) return "";
  const ref = D.acoes && D.acoes.data_referencia ? dataBR(D.acoes.data_referencia) : dataBR(D.data);
  return `<section>
    <div class="head"><h2>Destaques — quedas de ação × crédito</h2>
      <span class="eyebrow">ações × debêntures · janela 3 meses · ${ref}</span></div>
    <p class="note">Só emissores de debênture. Ranking pela maior <b>queda da ação em 3 meses</b>;
    ao lado, o que o <b>crédito</b> do mesmo emissor está fazendo — spread, abertura de 21 pregões (Δ21d)
    e ação de rating. Leituras lado a lado, <b>sem afirmar causa</b>: a queda da ação corrobora o crédito
    quando vem com spread largo, Δ21d abrindo e rebaixamento. No plano gratuito do brapi só há janela de
    3 meses (6m/12m exigem plano pago).</p>
    <div class="dq-bar">
      <span class="dq-tag">quedas de 3 meses</span>
      <label class="dq-f">ordenar
        <select id="dqOrd"><option value="queda">maior queda 3m</option>
          <option value="abrindo">maior Δ21d</option><option value="spread">maior spread</option></select></label>
      <label class="dq-f push">filtrar
        <select id="dqFil"><option value="todos">todos</option>
          <option value="rating">com ação de rating</option>
          <option value="abrindo">spread abrindo (Δ21d↑)</option>
          <option value="IPCA">indexador IPCA+</option>
          <option value="DI_SPREAD">indexador DI+</option>
          <option value="DI_PCT">indexador %DI</option></select></label>
    </div>
    <div class="dq-cnt" id="dqCnt"></div>
    <div class="dq-grid" id="dqGrid"></div>
    <div class="dq-more" id="dqMore"></div>
    <div id="dqLista"></div>
  </section>`;
}
function initDestaques(){
  if(!dqDados().length) return;
  const o=document.getElementById("dqOrd"), f=document.getElementById("dqFil");
  if(o) o.onchange=e=>{dqEstado.ord=e.target.value;dqEstado.aberto=false;dqRender();};
  if(f) f.onchange=e=>{dqEstado.filtro=e.target.value;dqEstado.aberto=false;dqRender();};
  dqRender();
}
"""
inner = rep(inner, "function montar(){", JS + "\nfunction montar(){")
inner = rep(inner, "    ${secaoRatings()}", "    ${secaoDestaques()}\n\n    ${secaoRatings()}")
inner = rep(inner, "  movimentos();\n  tabela();\n}\n\nmontar();",
                   "  movimentos();\n  tabela();\n  initDestaques();\n}\n\nmontar();")

SRC.write_text(inner, encoding="utf-8")
print("escrito:", SRC, len(inner), "bytes")

i2 = inner.index("const D = ") + len("const D = ")
depth = 0; instr = False; esc = False; start = i2; end = None
for j in range(i2, len(inner)):
    c = inner[j]
    if instr:
        if esc: esc = False
        elif c == "\\": esc = True
        elif c == '"': instr = False
    else:
        if c == '"': instr = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: end = j + 1; break
D2 = json.loads(inner[start:end])
print("D reparseia OK; acoes.destaques:", len(D2["acoes"]["destaques"]),
      "| acoes.emissores:", len(D2["acoes"]["emissores"]))
print("checks:", "secaoDestaques" in inner, "initDestaques()" in inner,
      'r:"Ação 3m"' in inner, "a.var_12m" in inner)
