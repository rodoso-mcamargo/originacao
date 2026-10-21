# -*- coding: utf-8 -*-
"""Camada 'Assembleias de debenturistas e waivers' (CVM/IPE) sobre o HTML final.

    python3 splice-agd-painel.py radar-final.html radar-final.html

Aplicar DEPOIS de splice-run-painel.py. Baixa os arquivos IPE da CVM
(dados.cvm.gov.br, cache em dados/cvm/), pega assembleias de debenturistas,
avisos aos debenturistas e fatos/comunicados com palavra de credito dos
ultimos JANELA dias, casa o nome da companhia com os emissores de debentures
e devedores de CRI/CRA do painel e classifica pelo ASSUNTO do documento:
  credito    = waiver, nao declaracao de vencimento antecipado, covenant,
               divida liquida, cronograma, prazo, repactuacao...
  estrutural = anuencia a M&A, troca de controle, cancelamento de registro,
               resgate, garantias, agente fiduciario...
  sem_pauta  = AGDEB cujo assunto nao diz a pauta (vale abrir o documento)
E triagem por palavra-chave: o documento precisa ser lido antes de concluir.
Descartados (dados/descartados.json) ficam fora. Sem rede para a CVM, a
secao entra vazia (a estrutura do HTML nao muda).
"""
import datetime as dt, io, json, pathlib, re, sys, unicodedata, urllib.request, zipfile

import pandas as pd

AQUI = pathlib.Path(__file__).resolve().parent
SRC, OUT = sys.argv[1], sys.argv[2]
JANELA = 270
CACHE = AQUI / "dados" / "cvm"
CACHE.mkdir(parents=True, exist_ok=True)
inner = open(SRC, encoding="utf-8").read()


def rep(s, a, b):
    assert s.count(a) == 1, f"ancora nao unica ({s.count(a)}): {a[:60]!r}"
    return s.replace(a, b, 1)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s)).strip()


SUF = re.compile(r"\b(S A|SA|S A\.|LTDA|EM RECUPERACAO JUDICIAL|CIA|COMPANHIA|DE|DO|DA|DOS|DAS|E)\b")


def chave(s):
    return re.sub(r"\s+", " ", SUF.sub(" ", norm(s))).strip()


# ---------------------------------------------------------------- dados do painel
i = inner.index("const D = ") + len("const D = ")
D, _ = json.JSONDecoder().raw_decode(inner[i:])
REF = dt.date.fromisoformat(D["data"])
desde = REF - dt.timedelta(days=JANELA)
radar = {}
for p in D.get("papeis", []):
    radar.setdefault(chave(p["emissor"]), ("DEB", p["emissor"]))
for r in (D.get("cricra") or {}).get("rows", []):
    radar.setdefault(chave(r["dev"]), ("CC", r["dev"]))
df = AQUI / "dados" / "descartados.json"
desc = {chave(x) for x in json.loads(df.read_text(encoding="utf-8"))["descartados"]} if df.exists() else set()


# ---------------------------------------------------------------- IPE da CVM
def ipe_ano(ano):
    dest = CACHE / f"ipe_cia_aberta_{ano}.zip"
    url = f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"
    velho = (not dest.exists()) or (dt.datetime.now().timestamp() - dest.stat().st_mtime > 18 * 3600)
    if velho:
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                dest.write_bytes(r.read())
        except Exception as e:
            print(f"[agd] sem acesso a {url}: {e}", file=sys.stderr)
            if not dest.exists():
                return None
    with zipfile.ZipFile(dest) as z:
        return pd.read_csv(io.BytesIO(z.read(f"ipe_cia_aberta_{ano}.csv")), sep=";", encoding="latin1", dtype=str)


partes = [x for x in (ipe_ano(a) for a in range(desde.year, REF.year + 2)) if x is not None]
rows, ate = [], None
if partes:
    d = pd.concat(partes, ignore_index=True)
    d["Data_Entrega"] = pd.to_datetime(d.Data_Entrega, errors="coerce").dt.date
    d = d[d.Data_Entrega >= desde]
    d["Versao"] = pd.to_numeric(d.Versao, errors="coerce")
    d = d.sort_values("Versao").drop_duplicates("Protocolo_Entrega", keep="last")
    ate = d.Data_Entrega.max()
    txt = (d.Assunto.fillna("") + " " + d.Especie.fillna("")).map(norm)
    cat = d.Categoria.fillna("")
    # forte: vale sozinho; fraco: so conta se o assunto nao for de M&A/estrutura
    FORTE = ("WAIVER|RENUNCIA TEMPORARIA|PERDAO|NAO DECLARA|NAO DECRETA|VENCIMENTO ANTECIPADO|"
             "INDICE FINANCEIRO|INDICES FINANCEIROS|COVENANT|DIVIDA LIQUIDA|CRONOGRAMA DE AMORTIZACAO|"
             "CRONOGRAMA DE PAGAMENTO|REPERFIL|CARENCIA|STANDSTILL|INADIMPL|NAO PAGAMENTO|"
             "REESTRUTURACAO DA DIVIDA|REESTRUTURACAO FINANCEIRA|RECUPERACAO JUDICIAL|RECUPERACAO EXTRAJUDICIAL|"
             "SUSPENSAO DO PAGAMENTO|SUSPENSAO DE PAGAMENTO|LIBERACAO DO CUMPRIMENTO")
    FRACO = ("ALTERACAO DO PRAZO|DATA DE VENCIMENTO|REPACTUA|ALTERACAO DE DETERMINADOS TERMOS|"
             "ALTERACOES EM ITENS DA ESCRITURA|ALTERACAO DA ESCRITURA")
    # em fato relevante/comunicado so o nucleo duro (repactuacao de concessao, waiver da B3 etc. ficam fora)
    TEXTUAL = ("WAIVER|NAO DECLARA|NAO DECRETA|VENCIMENTO ANTECIPADO|COVENANT|INDICE FINANCEIRO|INDICES FINANCEIROS|"
               "REESTRUTURACAO DA DIVIDA|REESTRUTURACAO FINANCEIRA|RECUPERACAO JUDICIAL|RECUPERACAO EXTRAJUDICIAL|"
               "STANDSTILL|INADIMPL|NAO PAGAMENTO|SUSPENSAO DE PAGAMENTO|SUSPENSAO DO PAGAMENTO|REPERFIL")
    FALSO = "WAIVER B3|FREE FLOAT|ACOES EM CIRCULACAO|PERCENTUAL MINIMO DE ACOES"
    ESTR = ("ANUENCIA|COMBINACAO|INCORPORA|CISAO|CONTROLE|CANCELAMENTO D[OE] REGISTRO|CONVERSAO D[OE] REGISTRO|"
            "RESGATE|AMORTIZACAO EXTRAORDINARIA|FIANCA|GARANTIA|AGENTE FIDUCIARIO|SUBSTITUICAO|CONTA RESERVA|"
            "PRESTADORES|AQUISICAO|ALIENACAO|DIVIDENDO|RECOMPRA")
    falso = txt.str.contains(FALSO)
    estr = txt.str.contains(ESTR)
    cred = (txt.str.contains(FORTE) | (txt.str.contains(FRACO) & ~estr)) & ~falso
    textual = cat.isin(["Fato Relevante", "Comunicado ao Mercado"]) | cat.str.startswith("Outros Comunicados")
    # aviso aos debenturistas so entra com pauta (pagamento de juros e afins e rotina)
    aviso = cat == "Aviso aos Debenturistas"
    agd = d.Tipo.fillna("").str.upper().str.contains("AGDEB")
    sel = agd | (aviso & (cred | estr)) | (textual & txt.str.contains(TEXTUAL) & ~falso)
    d = d[sel].assign(classe=pd.Series("sem_pauta", index=d.index)[sel]
                      .mask(estr[sel], "estrutural").mask(cred[sel], "credito"))
    d["ck"] = d.Nome_Companhia.map(chave)
    d = d[d.ck.isin(radar.keys()) & ~d.ck.isin(desc)]
    ORD = {"credito": 0, "estrutural": 1, "sem_pauta": 2}
    for ck, g in d.groupby("ck"):
        g = g.assign(_o=g.classe.map(ORD)).sort_values(["_o", "Data_Entrega"], ascending=[True, False])
        tipo, nome = radar[ck]
        docs = []
        for _, r in g.drop_duplicates(subset=["Assunto", "Especie"]).head(8).iterrows():
            a = re.sub(r"\s*\|\|\s*", " · ", str(r.Assunto if isinstance(r.Assunto, str) else "")).strip()
            docs.append({"d": r.Data_Entrega.isoformat(), "e": r.Especie if isinstance(r.Especie, str) else (r.Categoria or ""),
                         "a": a[:260], "c": r.classe, "u": r.Link_Download})
        rows.append({"emp": g.Nome_Companhia.iloc[0], "nome": nome, "tp": tipo,
                     "classe": g.classe.map(ORD).min(), "n": int(len(g)),
                     "n_cred": int((g.classe == "credito").sum()),
                     "ult": g.Data_Entrega.max().isoformat(),
                     "ult_cred": (g[g.classe == "credito"].Data_Entrega.max().isoformat() if (g.classe == "credito").any() else None),
                     "docs": docs})
    for r in rows:
        r["classe"] = ["credito", "estrutural", "sem_pauta"][r["classe"]]

PAY = json.dumps({"desde": desde.isoformat(), "ate": ate.isoformat() if ate else None, "janela": JANELA, "rows": rows},
                 ensure_ascii=True, separators=(",", ":"))
inner = rep(inner, '"papeis":[{"codigo"', '"agd":' + PAY + ',"papeis":[{"codigo"')

CSS = """
<style>
/* assembleias de debenturistas e waivers (CVM/IPE) */
.ag-bar{display:flex;gap:9px 14px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
.ag-f{display:flex;align-items:center;gap:6px;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.ag-cnt{color:var(--muted);font-size:12px;margin:2px 0 10px}
.ag-docs{margin:6px 0 0;padding:0;list-style:none;font-size:11.5px;line-height:1.45;white-space:normal}
.ag-docs li{padding:2px 0;color:var(--ink-2)}
.ag-docs a{color:var(--ink-2);text-decoration:underline;text-underline-offset:2px;text-decoration-color:var(--line-strong)}
.ag-docs .dt{font-family:"IBM Plex Mono",monospace;color:var(--muted);font-size:10.5px;margin-right:6px}
.ag-c{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:700;padding:0 5px;border:1px solid;white-space:nowrap}
.ag-c.credito{color:var(--abre);border-color:var(--abre)}
.ag-c.estrutural{color:var(--ink-2);border-color:var(--line-strong)}
.ag-c.sem_pauta{color:var(--aviso);border-color:var(--aviso)}
.ag-more{margin:12px 0 0;text-align:center}
</style>
"""
inner = rep(inner, '<div id="app"></div>', CSS + '\n<div id="app"></div>')

JS = r"""
/* ── Assembleias de debenturistas e waivers (CVM/IPE) ─────────────────
   Pedido de waiver, de não declaração de vencimento antecipado ou mudança de
   covenant/cronograma é o emissor sentando com credor: a conversa de passivo
   já começou. Lido pelo ASSUNTO do documento na CVM — abra a ata antes de
   concluir. Classe "sem pauta": a AGD existe mas o assunto não diz o quê. */
const AG = D.agd || {rows:[]};
const AG_ROT = {credito:"crédito", estrutural:"estrutural", sem_pauta:"sem pauta no assunto"};
const agEstado = {classe:"credito", aberto:false};
const AG_EMI = new Map(), AG_CC = new Map();
(AG.rows||[]).forEach(r=>{ (r.tp==="DEB"?AG_EMI:AG_CC).set(r.nome, r); });
function agCtx(r){
  if (r.tp==="DEB"){
    const ps = D.papeis.filter(p=>p.emissor===r.nome);
    const p = ps.slice().sort((a,b)=>(b.d_spread_21d??-999)-(a.d_spread_21d??-999))[0];
    return {p, n:ps.length, d21:p?p.d_spread_21d:null, spread:p?p.spread_bps:null, liq:p?p.liquidez:null, cod:p?p.codigo:""};
  }
  const cs = CC.filter(c=>c.dev===r.nome);
  const c = cs.slice().sort((a,b)=>(b.score??0)-(a.score??0))[0];
  return {c, n:cs.length, d21:null, spread:c?c.spread:null, score:c?c.score:null, cod:c?c.cod:""};
}
function agDeb(p){ const r = AG_EMI.get(p.emissor); return r && r.classe==="credito" ? r : null; }
function agLinhas(){
  const ord = {credito:0, estrutural:1, sem_pauta:2};
  return (AG.rows||[]).map(r=>({...r, x:agCtx(r)}))
    .filter(r=> agEstado.classe==="todos" || r.classe===agEstado.classe)
    .sort((a,b)=> (ord[a.classe]-ord[b.classe]) || ((b.x.d21??b.x.score??-999)-(a.x.d21??a.x.score??-999)) || (b.ult>a.ult?1:-1));
}
function agRow(r){
  const x = r.x;
  const docs = r.docs.map(o=>`<li><span class="dt">${dataBR(o.d)}</span><span class="ag-c ${o.c}">${AG_ROT[o.c].split(" ")[0]}</span>
    ${esc(o.e)} — <a href="${esc(o.u)}" target="_blank" rel="noopener">${esc(o.a||"(sem assunto)")}</a></li>`).join("");
  return `<tr${x.p?` data-i="${D.papeis.indexOf(x.p)}"`:x.c?` data-cc="${CC.indexOf(x.c)}"`:""} style="vertical-align:top">
    <td class="l" style="white-space:normal;min-width:420px"><span class="cod">${esc(r.nome)}</span>
      ${r.tp!=="DEB"?`<span class="cc-tp">CRI/CRA</span>`:""} <span class="ag-c ${r.classe}">${AG_ROT[r.classe]}</span>
      <span class="emi">${x.n} ${r.tp==="DEB"?"debênture(s)":"CRI/CRA"} no painel${x.cod?` · referência ${esc(x.cod)}`:""} · ${r.n} documento(s) na janela</span>
      <ul class="ag-docs">${docs}</ul></td>
    <td class="mono">${dataBR(r.ult_cred||r.ult)}</td>
    <td>${x.d21==null?"—":`<span class="${x.d21>0?"rn-up":"rn-dn"}">${sgn(x.d21)}</span>`}</td>
    <td>${x.spread==null?"—":f0(x.spread)}</td>
    <td>${r.tp==="DEB"?(x.liq==null?"—":medidorLiq(x.liq)):(x.score==null?"—":`score ${f0(x.score)}`)}</td>
  </tr>`;
}
function agRender(){
  const all = agLinhas(), vis = agEstado.aberto ? all : all.slice(0,15);
  const cnt = document.getElementById("agCnt");
  if (cnt) cnt.textContent = `${all.length} emissores · mostrando ${vis.length}`;
  document.getElementById("agTb").innerHTML = vis.map(agRow).join("")
    || `<tr><td class="l" colspan="5" style="padding:18px;color:var(--muted)">Nada nesta classe na janela.</td></tr>`;
  const m = document.getElementById("agMore");
  m.innerHTML = all.length>15 ? `<button type="button" class="btn" id="agMb" aria-expanded="${agEstado.aberto}">${agEstado.aberto?"▲ recolher":"▼ ver todos ("+(all.length-15)+" restantes)"}</button>` : "";
  const b = document.getElementById("agMb"); if (b) b.onclick = () => { agEstado.aberto=!agEstado.aberto; agRender(); };
}
function secaoAgd(){
  const R = AG.rows||[];
  const n = k => R.filter(r=>r.classe===k).length;
  return `<section>
    <div class="head"><h2>Assembleias de debenturistas e waivers</h2>
      <span class="eyebrow">CVM · documentos de ${dataBR(AG.desde)} a ${dataBR(AG.ate)} · ${n("credito")} emissores com pauta de crédito</span></div>
    <p class="note">Assembleias de debenturistas, avisos aos debenturistas e fatos relevantes/comunicados com
    palavra de crédito publicados na CVM nos últimos ${AG.janela} dias, casados pelo nome da companhia com os
    emissores e devedores deste painel. <b>Crédito</b>: o assunto fala em <i>waiver</i>, não declaração de
    vencimento antecipado, índice financeiro/dívida líquida, cronograma, prazo ou repactuação — o emissor já está
    conversando com o credor, ponto de entrada natural para originação (refinanciamento, alongamento, nova
    estrutura). <b>Estrutural</b>: anuência a M&A, troca de controle, cancelamento de registro, resgate, garantias —
    em geral rotina. <b>Sem pauta no assunto</b>: a AGD existe mas o assunto não diz o quê; abra o documento.
    É triagem pelo <b>assunto</b>, não pelo texto: leia a ata antes de concluir. Só cobre companhias com registro
    na CVM — SPEs sem registro ficam de fora — e descartados não aparecem. O sinal de crédito aparece também
    como motivo na triagem de debêntures.</p>
    <div class="ag-bar">
      <label class="ag-f">classe <select id="agCl">
        <option value="credito">crédito (${n("credito")})</option>
        <option value="sem_pauta">sem pauta no assunto (${n("sem_pauta")})</option>
        <option value="estrutural">estrutural (${n("estrutural")})</option>
        <option value="todos">todas (${R.length})</option></select></label>
    </div>
    <div class="ag-cnt" id="agCnt"></div>
    <div class="tw"><table><thead><tr>
      <th class="l">Emissor e documentos</th><th title="último documento de crédito (ou o último da janela)">Última</th>
      <th title="variação de spread em 21 pregões, papel do emissor que mais abriu">Δ 21d</th><th>Spread</th><th>Liquidez / score</th>
    </tr></thead><tbody id="agTb"></tbody></table></div>
    <div class="ag-more" id="agMore"></div>
  </section>`;
}
function initAgd(){
  if (!document.getElementById("agTb")) return;
  document.getElementById("agCl").onchange = e => { agEstado.classe=e.target.value; agEstado.aberto=false; agRender(); };
  agRender();
}
"""
inner = rep(inner, "function montar(){", JS + "\nfunction montar(){")
inner = rep(inner, "    ${secaoRatings()}", "    ${secaoRatings()}\n\n    ${secaoAgd()}")  # depois de Rating (pedido de 21/10/2026)
inner = rep(inner, """function motivosTriagem(s){
  const p = s.p, m = [];""", """function motivosTriagem(s){
  const p = s.p, m = [];
  { const g = agDeb(p); if (g) m.push(["alerta", `AGD de crédito ${dataBR(g.ult_cred)}`]); }""")
inner = rep(inner, "  initRun();\n", "  initRun();\n  initAgd();\n")

open(OUT, "w", encoding="utf-8").write(inner)
i = inner.index("const D = ") + len("const D = ")
D2, _ = json.JSONDecoder().raw_decode(inner[i:])
from collections import Counter
print("agd:", len(D2["agd"]["rows"]), "emissores", dict(Counter(r["classe"] for r in D2["agd"]["rows"])),
      "| ipe ate", D2["agd"]["ate"], "| bytes:", len(inner.encode()))
