# -*- coding: utf-8 -*-
"""Fila de dossies de credito do Radar do Secundario.

    python3 fila-dossies.py PAINEL.html [N]

Le o payload `const D = {...}` de um HTML do Radar (o publicado serve), mais
dados/dossies.json, dados/iliquidas.json e dados/descartados.json, e imprime em
JSON os N proximos nomes a pesquisar (padrao 8), com o contexto de cada um.

Universo (pedido do rodrigo em 21/10/2026):
  - toda debenture iliquida das carteiras das gestoras (raiz do codigo na CDA);
  - todo emissor de debenture com d_spread_21d >= CORTE_BPS em algum papel;
  - todo devedor de CRI/CRA com abertura na janela (dj) >= CORTE_BPS.
Descartados (nome exato ou termo de grupo) ficam fora.

Cada rodada divide as N vagas: ~60% para debentures e CRI/CRA (maior abertura
primeiro) e ~40% para iliquidas (sinal de estresse ja marcado primeiro, depois
maior posicao das gestoras); vaga que sobra de um lado vai para o outro.
Prioridade dentro de cada lado (maior primeiro):
  1. sem dossie ainda: abertura (bps) ou, para iliquida, posicao das gestoras
     em R$ mi (+5000 se ja tem sinal de estresse); +150 com AGD de credito;
  2. com dossie, mas vencido (> VALIDADE_DIAS), ou com gatilho novo: abertura
     cresceu >= 50 bps desde o dossie, ou AGD de credito posterior ao dossie.
Dossie em dia e sem gatilho nao entra.
"""
import datetime as dt, json, pathlib, re, sys, unicodedata

CORTE_BPS = 40
VALIDADE_DIAS = 30
AQUI = pathlib.Path(__file__).resolve().parent
SRC = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 8


def norm(x):
    x = unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", x)).strip()


SUF = re.compile(r"\b(S A|SA|LTDA|EM RECUPERACAO JUDICIAL|CIA|COMPANHIA|DE|DO|DA|DOS|DAS|E)\b")


def chave(x):
    return re.sub(r"\s+", " ", SUF.sub(" ", norm(x))).strip()


def ler(p, padrao):
    p = AQUI / p
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


s = open(SRC, encoding="utf-8").read()
i = s.index("const D = ") + len("const D = ")
D, _ = json.JSONDecoder().raw_decode(s[i:])
REF = dt.date.fromisoformat(D["data"])
dos = ler("dados/dossies.json", {"dossies": {}})["dossies"]
ili = ler("dados/iliquidas.json", {"raizes": {}})["raizes"]
desc = ler("dados/descartados.json", {"descartados": [], "termos_grupo": []})
nomes_desc = {norm(x) for x in desc.get("descartados", [])}
termos = [norm(t) for t in desc.get("termos_grupo", []) if norm(t)]
RX = re.compile(r"\b(" + "|".join(re.escape(t) for t in termos) + r")\b") if termos else None


def descartado(*vals):
    for v in vals:
        if v and (norm(v) in nomes_desc or (RX and RX.search(norm(v)))):
            return True
    return False


agd = {}
for r in (D.get("agd") or {}).get("rows", []):
    if r.get("classe") == "credito":
        agd[chave(r["nome"])] = {"ult_cred": r.get("ult_cred"),
                                 "docs": [{"d": o["d"], "a": o["a"], "u": o["u"]} for o in r["docs"] if o["c"] == "credito"][:4]}
rat = {chave(e["emissor"]): e for e in (D.get("ratings") or {}).get("emissores", [])}

cand = {}
# debentures com abertura
for p in D.get("papeis", []):
    d21 = p.get("d_spread_21d")
    if d21 is None or d21 < CORTE_BPS or descartado(p["emissor"]):
        continue
    k = chave(p["emissor"])
    c = cand.setdefault(k, {"chave": k, "nome": p["emissor"], "tipo": "debenture", "abertura": d21, "papeis": []})
    c["abertura"] = max(c["abertura"], d21)
    c["papeis"].append({k2: p.get(k2) for k2 in ("codigo", "familia", "indice_bruto", "taxa_emissao", "taxa_indicativa",
                                                  "spread_bps", "d_spread_21d", "pct_pu_par", "vencimento", "liquidez")})
# CRI/CRA com abertura
for r in (D.get("cricra") or {}).get("rows", []):
    dj = r.get("dj")
    if dj is None or dj < CORTE_BPS or descartado(r["dev"]):
        continue
    k = chave(r["dev"])
    c = cand.setdefault(k, {"chave": k, "nome": r["dev"], "tipo": "cri_cra", "abertura": dj, "papeis": []})
    c["abertura"] = max(c["abertura"], dj)
    c["papeis"].append({k2: r.get(k2) for k2 in ("cod", "tp", "idx", "tx", "spread", "dj", "par", "venc", "score", "sec")})
# iliquidas das gestoras
pos = {}
for r in (D.get("gcart") or {}).get("rows", []):
    if r.get("tipo") != "Debênture ilíq.":
        continue
    m = pos.setdefault(r["ent"], {"vl": 0.0, "gestoras": set()})
    m["vl"] += r.get("vl") or 0
    m["gestoras"].add(r.get("g"))
for raiz, m in pos.items():
    if not re.fullmatch(r"[A-Z0-9]{4}", raiz or ""):
        continue
    nome = (ili.get(raiz) or {}).get("nome")
    if descartado(nome):
        continue
    k = "RAIZ:" + raiz
    cand[k] = {"chave": k, "nome": nome or f"(raiz {raiz} — nome a resolver)", "tipo": "iliquida", "raiz": raiz,
               "abertura": None, "posicao_gestoras_rs_mi": round(m["vl"], 1), "gestoras": sorted(x for x in m["gestoras"] if x),
               "estresse_atual": (ili.get(raiz) or {}).get("est")}

fila = []
for k, c in cand.items():
    base = (c["abertura"] or 0) if c["tipo"] != "iliquida" else c["posicao_gestoras_rs_mi"] + (5000 if c.get("estresse_atual") else 0)
    ck = chave(c["nome"])
    if ck in agd:
        c["agd_credito"] = agd[ck]
        base += 150
    if ck in rat:
        c["rating_no_painel"] = rat[ck].get("achados", [])
    d = dos.get(k)
    if d is None:
        c["motivo"] = "novo"
        c["prioridade"] = 10000 + base
    else:
        idade = (REF - dt.date.fromisoformat(d["pesquisado_em"])).days
        cresceu = c["abertura"] is not None and (d.get("abertura_no_dossie") or 0) + 50 <= c["abertura"]
        agd_nova = ck in agd and (agd[ck]["ult_cred"] or "") > d["pesquisado_em"]
        if not (idade > VALIDADE_DIAS or cresceu or agd_nova):
            continue
        c["motivo"] = ("abertura +50 bps desde o dossie" if cresceu else "AGD de credito nova" if agd_nova
                       else f"dossie com {idade} dias")
        c["prioridade"] = (5000 if (cresceu or agd_nova) else 0) + base
        c["dossie_anterior"] = {"pesquisado_em": d["pesquisado_em"], "nivel": d.get("nivel"), "resumo": d.get("resumo")}
    c["papeis"] = sorted(c.get("papeis", []), key=lambda x: -(x.get("d_spread_21d") or x.get("dj") or 0))[:6]
    fila.append(c)

fila.sort(key=lambda c: -c["prioridade"])
spr = [c for c in fila if c["tipo"] != "iliquida"]
ilq = [c for c in fila if c["tipo"] == "iliquida"]
na = min(len(spr), max(N - len(ilq), round(N * 0.6)))
prox = spr[:na] + ilq[:N - na]
out = {"ref": D["data"], "universo": len(cand), "com_dossie": sum(1 for k in cand if k in dos),
       "pendentes": len(fila), "pendentes_abertura": len(spr), "pendentes_iliquidas": len(ilq), "proximos": prox}
print(json.dumps(out, ensure_ascii=False, indent=1))
