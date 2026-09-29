# -*- coding: utf-8 -*-
"""
Run diario da Ativa (pasta "Debentures" do Outlook) -> Radar do Secundario.

Duas funcoes, nesta ordem de prioridade:

1. FALLBACK DA ANBIMA. Quando dados/ultimo.json (coleta ANBIMA do GitHub
   Actions) esta atrasado, reconstroi os pregoes que faltam a partir do run.
   Regra de fonte, por papel:
     a) a coluna "Anbima" do run (e a taxa indicativa ANBIMA de D-1: o run de
        22/09 casa 549/549 com a ANBIMA de 21/09);
     b) se ela estiver vazia, o meio entre compra e venda da Ativa;
     c) papel que o run nao cota fica com a ultima taxa conhecida.
   Os campos que o run nao traz (PU, desvio, % Reune, curva de NTN-B e CDI)
   ficam os da ultima ANBIMA. Papel por (b) ou (c) NAO gera variacao nem entra
   na serie: trocar de regua (ANBIMA -> mid da mesa) inventaria movimento.

2. SECAO NOVA. Grava dados/run/ultimo.json com compra/venda da mesa para
   debentures CDI, IPCA e CRI/CRA, que o splice-run-painel.py desenha.

Entrada: os anexos .xlsx dos e-mails, no formato de texto que o conector do
Microsoft 365 devolve (tabulado, "=== Sheet: ... ==="), salvos em ./run/.
Nome do arquivo nao importa: tipo e data saem do conteudo.

    python3 run_ativa.py            # usa ./run, ./dados, ./monitor.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import re
import statistics
import sys

AQUI = pathlib.Path(__file__).resolve().parent
DADOS = AQUI / "dados"
RUN = AQUI / "run"
EXCEL0 = dt.date(1899, 12, 30)


# ── parser ─────────────────────────────────────────────────────────────────
def _num(v):
    v = (v or "").strip()
    if v in ("", "-", "--", "-/-") or "DoPar" in v or "(PU)" in v:
        return None
    try:
        return float(v.replace(",", ".")) if v.count(",") == 1 and "." not in v else float(v)
    except ValueError:
        return None


def _data_excel(v):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if n < 30000 or n > 80000:
        return None
    return (EXCEL0 + dt.timedelta(days=n)).isoformat()


def _familia(idx: str) -> str:
    u = (idx or "").upper().replace(" ", "")
    if u.startswith("%CDI") or u.startswith("%DI"):
        return "DI_PCT"
    if u.startswith("CDI+") or u.startswith("DI+"):
        return "DI_SPREAD"
    if u.startswith("IPCA"):
        return "IPCA"
    if u.startswith("IGP"):
        return "IGPM"
    if u.startswith("PR"):
        return "PRE"
    return "OUTRO"


MESES = {m: i + 1 for i, m in enumerate("jan fev mar abr mai jun jul ago set out nov dez".split())}


def _data_any(v):
    """Serial do Excel (anexo), dd/mm/aaaa ou 'dez/31' (corpo do e-mail)."""
    v = (v or "").strip()
    d = _data_excel(v)
    if d:
        return d
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", v)
    if m:
        return f"{m[3]}-{m[2]}-{m[1]}"
    m = re.fullmatch(r"([a-z]{3})/(\d{2})", v.lower())
    if m and m[1] in MESES:
        return f"20{m[2]}-{MESES[m[1]]:02d}-15"
    return None


def _html_para_tsv(msg: dict) -> str:
    """E-mail inteiro (JSON do conector) -> texto no mesmo formato do anexo.
    A tabela do corpo tem as mesmas colunas da planilha, com 2 casas."""
    import html as H
    h = msg["body"]["content"]
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", msg.get("subject", ""))
    out = [f"\t20{m[3]}-{m[2]}-{m[1]}"] if m else []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        cs = [re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", "", c))).strip()
              for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if any(cs):
            # o corpo intercala celulas vazias de layout; o cabecalho diz as posicoes
            out.append("\t" + "\t".join(cs))
    return "\n".join(out)


def parse_run(texto: str) -> dict | None:
    """Um anexo (ou o e-mail inteiro) -> {'tipo', 'data', 'rows'}"""
    origem = "anexo"
    if texto.lstrip().startswith("{"):
        try:
            texto = _html_para_tsv(json.loads(texto))
        except (ValueError, KeyError):
            return None
        origem = "corpo"
    # quebra de linha DENTRO de celula vem como \r\n ("Duration\r\n(Anos)")
    linhas = texto.replace("\r\n", " ").split("\n")
    data = None
    for l in linhas[:8]:
        for c in l.split("\t"):
            data = data or _data_excel(c) or (c.strip() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c.strip()) else None)
    tipo, cab, secao, rows = None, None, "", []
    for l in linhas:
        cs = [c.strip() for c in l.split("\t")]
        while cs and cs[0] == "":
            cs = cs[1:]
        if not any(cs):
            continue
        if cs[0] in ("Ativo", "Código"):
            cab = cs
            if "Lote Padrão" in cs:
                tipo = "CDI"
            elif any(c.startswith("Spread Over B") for c in cs) and cs[0] == "Ativo":
                tipo = "IPCA"
            elif cs[0] == "Código":
                tipo = "CRICRA"
            continue
        if len([c for c in cs if c]) == 1 or (cs[0] and cs[0].startswith("Debêntures")):
            secao = cs[0]
            continue
        if not cab or len(cs) < 6:
            continue
        v = dict(zip(cab, cs + [""] * (len(cab) - len(cs))))
        cod = (v.get("Ativo") or v.get("Código") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{5,14}", cod):
            continue
        compra, venda = _num(v.get("Compra (%)")), _num(v.get("Venda (%)"))
        r = {
            "cod": cod,
            "emissor": re.sub(r"\s+", " ", v.get("Emissor") or v.get("Risco") or "").strip(),
            "venc": _data_any(v.get("Vencimento")),
            "idx": v.get("Indexador"),
            "fam": _familia(v.get("Indexador")),
            "compra": compra, "venda": venda,
            "mid": round((compra + venda) / 2, 4) if compra is not None and venda is not None else None,
            "vol": (v.get("Volume") or v.get("Lote Padrão") or "").strip() or None,
            "so_pu": "DoPar" in (v.get("Compra (%)") or "") or "(PU)" in (v.get("Compra (%)") or ""),
        }
        if tipo in ("CDI", "IPCA"):
            r["anbima"] = _num(v.get("Anbima"))
            dur = _num(next((v[k] for k in v if k.startswith("Duration")), None))
            r["dur"] = dur if dur else None
            rt = (v.get("Rating") or "").strip()
            r["rating"] = None if rt in ("", "-") else rt
            r["isenta"] = "Isentas" in secao
            r["tp"] = "DEB"
        else:
            r["tp"] = "CRA" if secao.startswith("CRA") else "CRI" if secao.startswith("CRI") else None
            r["curva"] = _num(v.get("Curva (%)") or v.get("Curva"))
        rows.append(r)
    if not tipo or not data or not rows:
        return None
    return {"tipo": tipo, "data": data, "rows": rows, "origem": origem}


def carregar_runs(pasta: pathlib.Path) -> dict:
    """{(tipo, data): rows}. O anexo (4 casas) vence o corpo do e-mail (2 casas);
    entre dois da mesma origem, o arquivo mais recente."""
    out, origem = {}, {}
    for f in sorted(pasta.glob("*"), key=lambda p: p.stat().st_mtime):
        if not f.is_file():
            continue
        try:
            # newline="": preserva o \r\n que o Excel poe DENTRO da celula
            with open(f, encoding="utf-8", errors="replace", newline="") as fh:
                p = parse_run(fh.read())
        except Exception as e:  # anexo estranho nao derruba o resto
            print(f"[run] ignorado {f.name}: {e}", file=sys.stderr)
            continue
        if p:
            k = (p["tipo"], p["data"])
            if origem.get(k) == "anexo" and p["origem"] == "corpo":
                continue
            out[k], origem[k] = p["rows"], p["origem"]
    return out


# ── fallback da ANBIMA ─────────────────────────────────────────────────────
def dia_util_anterior(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


FAMS_DO_RUN = {"CDI": ("DI_SPREAD", "DI_PCT"), "IPCA": ("IPCA", "IGPM")}


def reconstruir(runs: dict, M) -> dict | None:
    """Gera os pregoes que faltam depois da ultima ANBIMA. Devolve o snapshot
    mais recente gerado (ja gravado em dados/ultimo.json) ou None."""
    anb = json.loads((DADOS / "ultimo.json").read_text(encoding="utf-8"))
    if anb.get("fonte", {}).get("tipo") == "run_ativa":
        # rodada repetida no mesmo diretorio: parte sempre da ANBIMA original
        anb = json.loads((DADOS / "ultimo_anbima.json").read_text(encoding="utf-8"))
    else:
        (DADOS / "ultimo_anbima.json").write_text(json.dumps(anb, ensure_ascii=False), encoding="utf-8")
    r0 = anb["data_referencia"]

    datas_email = sorted({d for (t, d) in runs if t in FAMS_DO_RUN})
    alvos = []  # (R, E)
    for e in datas_email:
        r = dia_util_anterior(dt.date.fromisoformat(e)).isoformat()
        if r > r0 and r not in [x for x, _ in alvos]:
            alvos.append((r, e))
    if not alvos:
        print(f"[run] ANBIMA em dia ({r0}); nenhum pregao a reconstruir")
        return None

    bruto = DADOS / "bruto" / f"{r0}_debentures.txt"
    tp = DADOS / "bruto" / f"{r0}_titulos_publicos.txt"
    if not bruto.exists():
        raise SystemExit(f"falta {bruto} — baixe dados/bruto/{r0}_debentures.txt do repositorio")
    modelo = {d.codigo: d for d in M.parse_debentures(bruto.read_text(encoding="utf-8"))}
    curva = M.curva_ntnb(M.parse_titulos_publicos(tp.read_text(encoding="utf-8"))) if tp.exists() else {}
    cdi = anb.get("cdi_aa")

    M.DADOS = DADOS
    serie = M.carregar_serie()
    ultima_taxa = {c: next((p["taxa"] for p in reversed(pts) if p.get("taxa") is not None), None)
                   for c, pts in serie.items()}

    snap = None
    for r, e in alvos:
        base = {}      # cod -> (taxa, fonte, dur_anos)
        fams_ok, fams_repetidas, fams_sem = [], [], []
        for tipo, fams in FAMS_DO_RUN.items():
            rows = runs.get((tipo, e))
            if not rows:
                fams_sem.extend(fams)
                continue
            # coluna "Anbima" repetida (o run de 23/09 veio com a de 21/09):
            # se >= 90% dos papeis tem a ultima taxa conhecida (tolerancia de
            # meio centesimo, porque o corpo do e-mail vem com 2 casas), ela nao
            # e de D-1 e o dia nao e reconstruido por esta coluna. Num dia
            # normal ~50% dos papeis repetem a taxa; repetido de verdade, 100%.
            iguais = [abs(x["anbima"] - ultima_taxa[x["cod"]]) < 0.006 for x in rows
                      if x.get("anbima") is not None and ultima_taxa.get(x["cod"]) is not None]
            repetida = len(iguais) >= 50 and sum(iguais) / len(iguais) >= 0.9
            (fams_repetidas if repetida else fams_ok).extend(fams)
            for x in rows:
                if not repetida and x.get("anbima") is not None:
                    base[x["cod"]] = (x["anbima"], "anbima_run", x.get("dur"))
                elif x.get("mid") is not None:
                    base[x["cod"]] = (x["mid"], "mid_run", x.get("dur"))
        if not fams_ok:
            print(f"[run] {r}: run de {e} sem coluna ANBIMA nova — pregao nao reconstruido")
            continue

        debs, fonte_papel = [], {}
        for cod, d in modelo.items():
            d = M.Debenture(**{k: getattr(d, k) for k in d.__dataclass_fields__})
            if cod in base and d.familia in fams_ok + fams_repetidas:
                tx, fonte, dur = base[cod]
                if dur and dur > 0:
                    d.duration_anos, d.duration_du = round(dur, 3), round(dur * 252)
            else:
                tx, fonte = ultima_taxa.get(cod), "repetida"
            if tx is None:
                continue
            d.taxa_indicativa = tx
            fonte_papel[cod] = fonte
            debs.append(d)

        # NTN-B do dia: a ANBIMA de titulos publicos nao chega por aqui e o run
        # nao traz a curva. Congelar a de r0 jogaria o movimento da NTN-B
        # inteiro no spread de todo IPCA+. Estimativa: por vertice de
        # referencia, a curva anda a variacao MEDIANA da taxa das debentures
        # IPCA+ daquele vertice desde r0 (pela coluna ANBIMA do run). Preserva
        # o movimento de cada papel contra os pares; a mediana de spread IPCA+
        # desses dias fica estavel por construcao — e o painel avisa.
        desl = {}
        for d in debs:
            m0 = modelo.get(d.codigo)
            if (d.familia == "IPCA" and fonte_papel[d.codigo] == "anbima_run" and m0
                    and m0.taxa_indicativa is not None and d.ref_ntnb):
                desl.setdefault(d.ref_ntnb.isoformat(), []).append(d.taxa_indicativa - m0.taxa_indicativa)
        todos = [x for xs in desl.values() for x in xs]
        glob = statistics.median(todos) if len(todos) >= 20 else 0.0
        curva_r = {v: round(t + (statistics.median(desl[v]) if len(desl.get(v, [])) >= 5 else glob), 4)
                   for v, t in curva.items()}

        data_r = dt.date.fromisoformat(r)
        linhas = M.enriquecer(debs, curva_r, cdi)
        linhas = M.comparar_historico(linhas, serie, data_r)
        for l in linhas:
            l["fonte_taxa"] = fonte_papel[l["codigo"]]
            if l["fonte_taxa"] != "anbima_run":
                for k in ("d_spread_1d", "d_spread_5d", "d_spread_21d", "d_pu_1d_pct", "zscore_spread"):
                    l[k] = None
        linhas = M.gerar_sinais(linhas)

        # serie: so o que veio da coluna ANBIMA do run
        boas = [l for l in linhas if l["fonte_taxa"] == "anbima_run"]
        for l in boas:
            pts = serie.setdefault(l["codigo"], [])
            pts[:] = [p for p in pts if p["data"] != r]
            pts.append({"data": r, "spread_bps": l["spread_bps"], "spread_bruto_bps": l["spread_bruto_bps"],
                        "pu": l["pu"], "taxa": l["taxa_indicativa"]})
            ultima_taxa[l["codigo"]] = l["taxa_indicativa"]
        M.gravar_dia(data_r, boas)

        n = lambda f: sum(1 for l in linhas if l["fonte_taxa"] == f)
        com_spread = [l for l in linhas if l["spread_bps"] is not None]
        snap = {
            "data_referencia": r,
            "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
            "cdi_aa": cdi,
            "cobertura": {"papeis": len(linhas), "com_spread": len(com_spread),
                          "curva_ntnb_vertices": len(curva),
                          "por_familia": {f: sum(1 for l in linhas if l["familia"] == f)
                                          for f in sorted({l["familia"] for l in linhas})}},
            "agregados": M.agregados(linhas),
            "destaques": M.destaques(linhas),
            "papeis": linhas,
            "fonte": {"tipo": "run_ativa", "email": e, "anbima_ate": r0,
                      "n_anbima_run": n("anbima_run"), "n_mid": n("mid_run"), "n_repetida": n("repetida"),
                      "familias_run": sorted(set(fams_ok)), "familias_sem_run": sorted(set(fams_sem)),
                      "familias_coluna_repetida": sorted(set(fams_repetidas)),
                      "ntnb": "estimada", "ntnb_desloc_mediano_bps": round(glob * 100, 1),
                      "ntnb_base": r0, "cdi_de": r0, "pu_de": r0},
        }
        print(f"[run] {r} reconstruido do run de {e}: {n('anbima_run')} pela coluna ANBIMA, "
              f"{n('mid_run')} pelo mid, {n('repetida')} com a ultima taxa"
              + (f"; sem run: {','.join(sorted(set(fams_sem)))}" if fams_sem else ""))

    if snap:
        (DADOS / "ultimo.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return snap


# ── nota de credito do run ─────────────────────────────────────────────────
# O run traz a nota vigente por papel ("AA- FITCH", "AAA S&P", "A+ MOODYS").
# Escala nacional, do melhor ao pior; F1+ e afins (curto prazo) ficam de fora.
ESCALA = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
          "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "RD", "SD", "D"]
AGENCIAS = {"FITCH": "Fitch", "S&P": "S&P Global", "MOODYS": "Moody's", "MOODY'S": "Moody's",
            "MOODY´S": "Moody's", "MOODY`S": "Moody's"}
NOTA_CORTE = ESCALA.index("A+")      # A+ ou pior = nota baixa para o painel


def nota(rt):
    """'AA- FITCH' -> {'grau': 'AA-', 'ag': 'Fitch', 'nivel': 3}; None se nao for escala de longo prazo."""
    if not rt:
        return None
    partes = rt.strip().upper().split()
    if len(partes) < 2 or partes[0] not in ESCALA:
        return None
    ag = AGENCIAS.get(" ".join(partes[1:]).replace("’", "'"))
    return {"grau": partes[0], "ag": ag or " ".join(partes[1:]).title(), "nivel": ESCALA.index(partes[0])}


def mudancas_de_nota(runs: dict) -> list[dict]:
    """Compara a nota de cada papel entre runs consecutivos (mesma agencia).
    A data e a do run em que a mudanca apareceu; a acao da agencia caiu entre
    o run anterior e esse."""
    hist = {}
    for (t, d) in sorted(runs, key=lambda k: k[1]):
        if t not in FAMS_DO_RUN:
            continue
        for x in runs[(t, d)]:
            n = nota(x.get("rating"))
            if n:
                hist.setdefault(x["cod"], []).append((d, n, x.get("emissor")))
    out = []
    for cod, pts in hist.items():
        for (d0, n0, _), (d1, n1, emi) in zip(pts, pts[1:]):
            if n0["ag"] == n1["ag"] and n0["grau"] != n1["grau"]:
                out.append({"cod": cod, "emissor": emi, "agencia": n1["ag"], "de": n0["grau"],
                            "para": n1["grau"], "data": d1, "run_anterior": d0,
                            "acao": "rebaixamento" if n1["nivel"] > n0["nivel"] else "elevacao"})
    return out


def mesclar_ratings(mud: list[dict]) -> bool:
    """Acrescenta as mudancas de nota a dados/ratings.json, agrupadas por
    emissor (nome da ANBIMA quando o codigo esta no painel). Idempotente."""
    f = DADOS / "ratings.json"
    if not mud or not f.exists():
        return False
    R = json.loads(f.read_text(encoding="utf-8"))
    ult = json.loads((DADOS / "ultimo.json").read_text(encoding="utf-8"))
    emi_anb = {p["codigo"]: p.get("emissor") for p in ult.get("papeis", [])}
    idx = {e["emissor"].upper(): e for e in R.get("emissores", [])}
    por = {}
    for m in mud:
        por.setdefault((emi_anb.get(m["cod"]) or m["emissor"] or m["cod"], m["agencia"], m["de"],
                        m["para"], m["data"], m["run_anterior"], m["acao"]), []).append(m["cod"])
    mexeu = False
    for (emi, ag, de, para, data, d0, acao), cods in por.items():
        e = idx.get(emi.upper())
        if e is None:
            e = {"emissor": emi, "papeis": [], "d_spread_21d": None, "achados": [],
                 "nota_selecao": "mudanca de nota vista no run da Ativa"}
            R.setdefault("emissores", []).append(e)
            idx[emi.upper()] = e
        resumo = (f"Nota {ag} passou de {de} para {para} no run da Ativa "
                  f"(entre os runs de {d0} e {data}; papeis {', '.join(sorted(cods))})")
        if any(a.get("resumo") == resumo for a in e.get("achados", [])):
            continue
        e.setdefault("achados", []).append({"agencia": ag, "acao": acao, "resumo": resumo,
                                            "data": data, "fonte": "Run da Ativa (e-mail)", "url": None})
        e["papeis"] = sorted(set(e.get("papeis") or []) | set(cods))
        mexeu = True
    if mexeu:
        R["pesquisado_em"] = max(R.get("pesquisado_em") or "", max(m["data"] for m in mud))
        f.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    return mexeu


# ── payload da secao nova ──────────────────────────────────────────────────
def payload_secao(runs: dict) -> dict | None:
    ult = {}
    for (t, d) in runs:
        if d > ult.get(t, ""):
            ult[t] = d
    if not ult:
        return None
    out = {"datas": ult, "rows": []}
    for t, d in ult.items():
        for x in runs[(t, d)]:
            y = {k: v for k, v in x.items() if v not in (None, False, "")}
            y["run"] = t
            n = nota(x.get("rating"))
            if n:
                y["nota"] = n
            out["rows"].append(y)
    return out


def main() -> int:
    runs = carregar_runs(RUN)
    print(f"[run] {len(runs)} planilhas: " + ", ".join(f"{t} {d}" for t, d in sorted(runs, key=lambda k: (k[1], k[0]))))
    sys.path.insert(0, str(AQUI))
    import monitor as M
    snap = reconstruir(runs, M)
    sec = payload_secao(runs)
    mud = mudancas_de_nota(runs)
    if sec is not None:
        sec["mudancas_nota"] = mud
    for m in mud:
        print(f"[run] nota {m['cod']}: {m['agencia']} {m['de']} -> {m['para']} (run de {m['data']})")
    if mesclar_ratings(mud):
        print("RATINGS_ALTERADOS=1  (dados/ratings.json mudou: suba para claude/ratings-credito.json)")
    (DADOS / "run").mkdir(parents=True, exist_ok=True)
    (DADOS / "run" / "ultimo.json").write_text(json.dumps(sec or {}, ensure_ascii=False), encoding="utf-8")
    if snap:
        print(f"[run] dados/ultimo.json agora e {snap['data_referencia']} (fonte: run da Ativa)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
