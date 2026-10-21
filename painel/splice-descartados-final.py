# -*- coding: utf-8 -*-
"""Tira os descartados (e o grupo economico deles) de TODAS as listas do painel.

    python3 splice-descartados-final.py radar-final.html radar-final.html

Aplicar POR ULTIMO, depois de todos os outros splices. Le dados/descartados.json:
  "descartados":   nomes exatos (como ja era)
  "termos_grupo":  palavras que identificam o grupo (HAPVIDA, CSN, RUMO...), casadas
                   como palavra inteira no nome sem acento/pontuacao.
Remove do payload D: papeis, cricra.rows, acoes.(acoes|emissores|destaques),
gcart.rows (inclusive iliquidas pela raiz do codigo), ratings.emissores, agd.rows,
runAtiva.run.rows, cda.concentrados. Os papeis tirados vao para D.descRef, que so a
secao "Ideias descartadas" le (spread de referencia). Agregados de mercado
(medianas, curva, evolucao) continuam com o universo inteiro.
"""
import json, pathlib, re, sys, unicodedata

AQUI = pathlib.Path(__file__).resolve().parent
SRC, OUT = sys.argv[1], sys.argv[2]
s = open(SRC, encoding="utf-8").read()
cfg = json.loads((AQUI / "dados" / "descartados.json").read_text(encoding="utf-8"))


def norm(x):
    x = unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", x)).strip()


nomes = {norm(x) for x in cfg["descartados"]}
termos = [norm(t) for t in cfg.get("termos_grupo", []) if norm(t)]
RX = re.compile(r"\b(" + "|".join(re.escape(t) for t in termos) + r")\b") if termos else None


def bate(*vals):
    for v in vals:
        if not v:
            continue
        nv = norm(v)
        if nv in nomes or (RX and RX.search(nv)):
            return True
    return False


i0 = s.index("const D = ") + len("const D = ")
D, fim = json.JSONDecoder().raw_decode(s[i0:])
cont = {}

# debentures: emissor
pap = D.get("papeis", [])
fora = [p for p in pap if bate(p.get("emissor"))]
D["papeis"] = [p for p in pap if not bate(p.get("emissor"))]
D["descRef"] = fora
cods = {p["codigo"] for p in fora}
raizes = {re.match(r"[A-Z]{4}", c).group(0) for c in cods if re.match(r"[A-Z]{4}", c)}
cont["papeis"] = len(fora)

# acoes: ticker do grupo (pelo vinculo emissor->ticker) + nome
A = D.get("acoes") or {}
tks = {e.get("ticker") for e in A.get("emissores", []) if bate(e.get("emissor"), e.get("empresa"))}
tks.discard(None)
for k, campos in (("emissores", ("emissor", "empresa")), ("destaques", ("empresa",)), ("acoes", ("empresa",))):
    if k in A:
        antes = len(A[k])
        A[k] = [x for x in A[k] if not (x.get("ticker") in tks or bate(*[x.get(c) for c in campos]))]
        cont["acoes." + k] = antes - len(A[k])

# CRI/CRA: devedor
cc = D.get("cricra") or {}
if "rows" in cc:
    antes = len(cc["rows"])
    cc["rows"] = [r for r in cc["rows"] if not bate(r.get("dev"))]
    cont["cricra"] = antes - len(cc["rows"])

# gestoras: entidade, ticker, raiz de iliquida
g = D.get("gcart") or {}
if "rows" in g:
    antes = len(g["rows"])
    g["rows"] = [r for r in g["rows"]
                 if not (bate(r.get("ent")) or (r.get("tk") in tks) or r.get("ent") in raizes)]
    cont["gcart"] = antes - len(g["rows"])

# ratings
R = D.get("ratings") or {}
if "emissores" in R:
    antes = len(R["emissores"])
    R["emissores"] = [e for e in R["emissores"] if not bate(e.get("emissor"))]
    cont["ratings"] = antes - len(R["emissores"])

# AGD, run, CDA
ag = D.get("agd") or {}
if "rows" in ag:
    antes = len(ag["rows"])
    ag["rows"] = [r for r in ag["rows"] if not bate(r.get("nome"), r.get("emp"))]
    cont["agd"] = antes - len(ag["rows"])
run = ((D.get("runAtiva") or {}).get("run") or {})
if run.get("rows"):
    antes = len(run["rows"])
    run["rows"] = [r for r in run["rows"]
                   if not (bate(r.get("emissor"), r.get("dev"), r.get("risco")) or r.get("cod") in cods)]
    cont["run"] = antes - len(run["rows"])
cda = D.get("cda") or {}
if cda.get("concentrados"):
    antes = len(cda["concentrados"])
    cda["concentrados"] = [c for c in cda["concentrados"] if c.get("codigo") not in cods]
    cont["cda"] = antes - len(cda["concentrados"])

novo = json.dumps(D, ensure_ascii=True, separators=(",", ":"))
s = s[:i0] + novo + s[i0 + fim:]

a = "D.papeis.filter(p=>p.emissor===nome"
assert s.count(a) == 1, "ancora da secao de descartados nao encontrada"
s = s.replace(a, "(D.descRef||[]).concat(D.papeis).filter(p=>p.emissor===nome", 1)

open(OUT, "w", encoding="utf-8").write(s)
print("descartados removidos:", cont, "| tickers:", sorted(tks), "| raizes:", sorted(raizes))
print("emissores tirados:", sorted({p["emissor"] for p in fora}))
