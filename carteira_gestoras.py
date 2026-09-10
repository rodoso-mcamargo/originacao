#!/usr/bin/env python3
"""
Carteira 100% das principais gestoras, a partir da CDA/CVM.

Diferente do cda.py (que filtra pelos papeis do radar e so guarda debenture),
aqui o filtro e por FUNDO: casa a marca da gestora no nome do fundo (DENOM_SOCIAL)
e mantem TODOS os tipos de ativo - debenture liquida e iliquida, CRI, CRA, nota
comercial, letra financeira, titulo publico, cota de fundo, etc. Assim da para
ver 100% da carteira de credito das casas, inclusive o que nao tem preco no radar.

Roda no GitHub Actions (o runner alcanca dados.cvm.gov.br; o sandbox nao).

    python carteira_gestoras.py                 # mes mais recente disponivel
    python carteira_gestoras.py --mes 2026-02
    python carteira_gestoras.py --top 60        # posicoes por gestora na saida

Saida: dados/gestoras/ultimo.json
    - gestoras: total, quebra por tipo de ativo, nº de posicoes
    - posicoes: top N por gestora (tipo, emissor, codigo, valor, nº series)
O join com credito/acao/rating e feito depois, no sandbox, ao montar a tabela.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import os
import pathlib
import re
import socket
import sys
import tempfile
import traceback
import zipfile
from collections import defaultdict

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("carteira")
csv.field_size_limit(1 << 30)

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
DIR_OUT = DADOS / "gestoras"

HOST = "dados.cvm.gov.br"
URL = f"https://{HOST}/dados/FI/DOC/CDA/DADOS/cda_fi_{{aaaamm}}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0"}
TIMEOUT = 300

try:
    import urllib3.util.connection as _u3conn
    _u3conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

ALIASES = {
    "codigo":   ["CD_ATIVO", "CD_ATIVO_NEGOC", "CODIGO_ATIVO"],
    "tipo":     ["TP_ATIVO", "TIPO_ATIVO"],
    "fundo":    ["DENOM_SOCIAL", "NM_FUNDO", "DENOM_SOCIAL_CLASSE"],
    "emissor":  ["EMISSOR", "NM_EMISSOR"],
    "vl_pos":   ["VL_MERC_POS_FINAL", "MERC_POS_FIM", "VL_POS_FINAL"],
}

# marca da gestora -> regex no nome do fundo (boutiques antes de bancos)
BRANDS = [
 ("Itaú", r"ITA[ÚU]"), ("BB", r"\bBB\b|BRASILPREV|BANCO DO BRASIL"), ("Bradesco", r"BRADESCO|\bBRAM\b"),
 ("Santander", r"SANTANDER"), ("Safra", r"\bSAFRA\b"), ("Kinea", r"KINEA"), ("Caixa", r"\bCAIXA\b"),
 ("BTG", r"\bBTG\b"), ("Absolute", r"ABSOLUTE"), ("XP", r"\bXPA?\b"), ("ARX", r"\bARX\b"), ("JGP", r"\bJGP\b"),
 ("Sparta", r"SPARTA"), ("Icatu", r"ICATU"), ("AZ Quest", r"AZ ?QUEST"), ("SulAmérica", r"SULAM[ÉE]RICA"),
 ("Legacy", r"LEGACY"), ("SPX", r"\bSPX\b"),
]
PATS = [(n, re.compile(p, re.I)) for n, p in BRANDS]


def marca(fundo: str):
    for n, p in PATS:
        if p.search(fundo):
            return n
    return None


def resolver(cab):
    presentes = {c.strip().upper(): c for c in cab}
    fora = {}
    for logico, cands in ALIASES.items():
        for c in cands:
            if c in presentes:
                fora[logico] = presentes[c]; break
    return fora


def num(v):
    if v is None: return None
    v = str(v).strip()
    if v in ("", "-", "NA", "N/A"): return None
    try:
        return float(v.replace(".", "").replace(",", ".")) if "," in v else float(v)
    except ValueError:
        return None


def meses(ate, quantos):
    out, a, m = [], ate.year, ate.month
    for _ in range(quantos):
        out.append(f"{a}{m:02d}"); m -= 1
        if m == 0: a, m = a - 1, 12
    return out


def baixar_zip(aaaamm):
    u = URL.format(aaaamm=aaaamm)
    r = requests.get(u, headers=HEADERS, timeout=TIMEOUT, stream=True)
    with r:
        if r.status_code == 404:
            log.info("%s ainda nao publicado", aaaamm); return None
        r.raise_for_status()
        destino = pathlib.Path(tempfile.gettempdir()) / f"cda_{aaaamm}.zip"
        n = 0
        with destino.open("wb") as fh:
            for pedaco in r.iter_content(chunk_size=1 << 20):
                fh.write(pedaco); n += len(pedaco)
    log.info("%s: %.1f MB baixados", aaaamm, n / 1e6)
    if not zipfile.is_zipfile(destino):
        raise RuntimeError(f"{u} nao devolveu zip valido")
    return destino


def membros_csv(caminho):
    with zipfile.ZipFile(caminho) as z:
        nomes = sorted(z.namelist())
        log.info("zip: %d membros: %s", len(nomes), ", ".join(nomes[:15]))
        for nome in nomes:
            b = nome.lower()
            if b.endswith(".csv"):
                yield nome, z.open(nome)
            elif b.endswith(".zip"):
                interno = io.BytesIO(z.read(nome))
                with zipfile.ZipFile(interno) as z2:
                    for n2 in sorted(z2.namelist()):
                        if n2.lower().endswith(".csv"):
                            yield f"{nome}!{n2}", z2.open(n2)


def limpa_emissor(s):
    s = (s or "").strip()
    # alguns arquivos trazem CNPJ no lugar do nome; deixa como veio
    return s


def processar(caminho, aaaamm, topn):
    # agg[gestora][(tipo, chave)] = [vl, n_linhas, {codigos}, emissor_repr]
    agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0, set(), ""]))
    portipo = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    tipos_vistos = defaultdict(float)
    diag = {"arquivos": [], "ignorados": [], "linhas": 0, "linhas_gestora": 0}

    for nome, fh in membros_csv(caminho):
        try:
            txt = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            leitor = csv.reader(txt, delimiter=";")
            try:
                cab = next(leitor)
            except StopIteration:
                continue
            m = resolver(cab)
            if "fundo" not in m or "vl_pos" not in m:
                diag["ignorados"].append(nome); continue
            idx = {k: cab.index(v) for k, v in m.items()}
            diag["arquivos"].append({"arquivo": nome, "mapa": m})
            log.info("lendo %s", nome)

            def pega(l, k):
                i = idx.get(k)
                return l[i].strip() if i is not None and len(l) > i else ""

            for l in leitor:
                diag["linhas"] += 1
                fundo = pega(l, "fundo")
                if not fundo:
                    continue
                g = marca(fundo)
                if not g:
                    continue
                diag["linhas_gestora"] += 1
                vl = num(pega(l, "vl_pos")) or 0.0
                tipo = pega(l, "tipo") or "(sem tipo)"
                cod = pega(l, "codigo").upper()
                emis = limpa_emissor(pega(l, "emissor"))
                chave = emis.upper() if emis else (cod or tipo)
                a = agg[g][(tipo, chave)]
                a[0] += vl; a[1] += 1
                if cod: a[2].add(cod)
                if not a[3]: a[3] = emis or cod or tipo
                portipo[g][tipo] += vl
                total[g] += vl
                tipos_vistos[tipo] += vl
        except Exception as e:
            log.error("falha %s: %s: %s", nome, type(e).__name__, e)
            diag["ignorados"].append(f"{nome} (erro)")
        finally:
            try: fh.close()
            except Exception: pass

    gestoras = []
    posicoes = []
    for g, _ in BRANDS:
        if g not in total:
            continue
        pt = {t: round(v/1e6, 1) for t, v in sorted(portipo[g].items(), key=lambda x:-x[1])}
        gestoras.append({"n": g, "total": round(total[g]/1e9, 3),
                         "por_tipo": pt, "n_pos": len(agg[g])})
        top = sorted(agg[g].items(), key=lambda kv:-kv[1][0])[:topn]
        for (tipo, chave), (vl, nl, cods, rep) in top:
            posicoes.append({"g": g, "tipo": tipo, "emissor": rep,
                             "cod": sorted(cods)[0] if cods else "",
                             "n_cods": len(cods), "vl": round(vl/1e6, 1)})

    ref = f"{aaaamm[:4]}-{aaaamm[4:]}"
    doc = {"mes_referencia": ref,
           "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
           "fonte": "CDA/CVM - carteira completa por gestora (todos os tipos de ativo)",
           "gestoras": gestoras, "posicoes": posicoes,
           "tipos_ativo": {t: round(v/1e9, 3) for t, v in sorted(tipos_vistos.items(), key=lambda x:-x[1])},
           "diag": {"linhas": diag["linhas"], "linhas_gestora": diag["linhas_gestora"],
                    "arquivos_lidos": [a["arquivo"] for a in diag["arquivos"]],
                    "arquivos_ignorados": diag["ignorados"]}}
    DIR_OUT.mkdir(parents=True, exist_ok=True)
    (DIR_OUT / "ultimo.json").write_text(
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # resumo p/ o job
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    linhas_md = [f"## Carteira das gestoras - {ref}\n",
                 f"- linhas varridas: {diag['linhas']:,} | de gestora-alvo: {diag['linhas_gestora']:,}\n",
                 "\n### Tipos de ativo (R$ bi, todas as gestoras-alvo)\n"]
    for t, v in sorted(tipos_vistos.items(), key=lambda x:-x[1])[:20]:
        linhas_md.append(f"- `{t}`: {v/1e9:.2f}\n")
    linhas_md.append("\n### Total por gestora (R$ bi)\n")
    for gz in gestoras:
        linhas_md.append(f"- {gz['n']}: {gz['total']:.2f} ({gz['n_pos']} posicoes)\n")
    if resumo:
        with open(resumo, "a", encoding="utf-8") as fh:
            fh.write("".join(linhas_md))
    log.info("gestoras=%d posicoes=%d tipos=%d", len(gestoras), len(posicoes), len(tipos_vistos))
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mes", help="AAAA-MM")
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args(argv)
    alvos = [a.mes.replace("-", "")] if a.mes else meses(dt.date.today(), 8)
    for mm in alvos:
        caminho = baixar_zip(mm)
        if caminho is None:
            continue
        processar(caminho, mm, a.top)
        return 0
    log.error("nenhum mes disponivel")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log.error("FALHA\n%s", tb)
        r = os.environ.get("GITHUB_STEP_SUMMARY")
        if r:
            with open(r, "a", encoding="utf-8") as fh:
                fh.write("## Falha\n\n```\n" + tb + "\n```\n")
        sys.exit(1)
