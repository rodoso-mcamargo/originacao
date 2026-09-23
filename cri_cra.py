# -*- coding: utf-8 -*-
"""Coletor das Taxas de CRI e CRA da ANBIMA.

Fonte: pagina publica, server-rendered (a tabela vem no HTML):
  https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm

Roda no GitHub Actions (o container do Claude NAO alcanca anbima.com.br).
Escrito DEFENSIVO: a estrutura exata da tabela nunca foi vista de dentro do
Python. `--inspecionar` descreve a resposta sem gravar; rodar isso primeiro.

Saidas:
  dados/cri_cra/AAAA-MM-DD.csv   uma linha por ativo
  dados/cri_cra/ultimo.json      ultimo dia + diagnostico do parsing
"""
from __future__ import annotations
import argparse, datetime as dt, io, json, logging, os, pathlib, re, sys, traceback

import requests

log = logging.getLogger("cri_cra")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

URL = ("https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/"
       "taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
TIMEOUT = 60
DIR_OUT = pathlib.Path("dados/cri_cra")

# nome normalizado -> variantes de cabecalho aceitas (comparacao sem acento/caixa)
ALIASES = {
    "emissor":        ["risco de credito", "devedor", "originador", "risco credito"],
    "securitizadora": ["emissor", "securitizadora", "emissora", "companhia securitizadora"],
    "serie":          ["serie", "serie/emissao"],
    "codigo":         ["codigo", "codigo do ativo", "codigo ativo", "cod"],
    "vencimento":     ["vencimento", "data de vencimento"],
    "indexador":      ["indice/correcao", "indice", "indexador", "correcao", "remuneracao"],
    "taxa_compra":    ["taxa compra", "taxa de compra", "tx compra"],
    "taxa_venda":     ["taxa venda", "taxa de venda", "tx venda"],
    "taxa_indicativa":["taxa indicativa", "tx indicativa", "taxa ind"],
    "desvio":         ["desvio padrao", "desvio-padrao", "desvio"],
    "pu":             ["pu", "preco unitario", "pu (r$)"],
    "pct_pu_par":     ["% pu par", "%pu par", "% pu de par", "pu par", "% pu"],
    "duration":       ["duration", "duration (anos)", "duracao"],
    "referencia":     ["referencia", "data", "data de referencia"],
}


def _sem_acento(s: str) -> str:
    tab = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
                        "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC")
    return s.translate(tab)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", _sem_acento(str(s)).strip().lower())


def _num(v):
    """Numero em pt-BR: ponto = milhar, virgula = decimal. '' / '--' -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "nan", "None", "n/d", "N/D"):
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if s in ("", "-", ".", ","):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _data_iso(v):
    s = str(v).strip()
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def baixar() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "latin-1"
    html = r.text
    if "taxa" not in _norm(html) and "cri" not in _norm(html):
        raise RuntimeError("conteudo inesperado (pagina de erro?)")
    return html


def _mapa_colunas(cols):
    """De cada coluna do DataFrame para o nome normalizado, via ALIASES."""
    out = {}
    for c in cols:
        cn = _norm(c)
        for chave, variantes in ALIASES.items():
            if chave in out.values():
                continue
            if any(cn == v or cn.startswith(v) or v in cn for v in variantes):
                out[c] = chave
                break
    return out


def _infere_tipo(codigo: str, indexador: str = "") -> str:
    c = (codigo or "").upper()
    if c.startswith("CRA"):
        return "CRA"
    if c.startswith("CRI"):
        return "CRI"
    # CRA de agro costuma ter 'CRA' no codigo; CRI (imobiliario) usa codigo
    # numerico tipo 25B0013406 / 24D2765715. Sem prefixo -> provavel CRI.
    return "CRI"


def _celulas(tr):
    return [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
            for c in tr.find_all(["td", "th"])]


def _tabelas_html(html):
    """Cada <table> vira (header:list[str], linhas:list[list[str]]). Header é a
    linha (thead/1ª tr) cujas células casam mais aliases; texto CRU (pt-BR)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    saida = []
    for tab in soup.find_all("table"):
        linhas = [_celulas(tr) for tr in tab.find_all("tr")]
        linhas = [l for l in linhas if l]
        if len(linhas) < 2:
            continue
        # escolhe a linha de cabecalho: a que mapeia mais colunas conhecidas
        idx_hdr, melhor_n = 0, -1
        for i, l in enumerate(linhas[:5]):
            n = len(set(_mapa_colunas(l).values()))
            if n > melhor_n:
                idx_hdr, melhor_n = i, n
        header = linhas[idx_hdr]
        dados = linhas[idx_hdr + 1:]
        saida.append((header, dados))
    return saida


def parse(html: str):
    tabelas = _tabelas_html(html)
    diag = {"n_tabelas": len(tabelas), "tabelas": []}
    melhor = None  # (header, dados, mapa)
    for i, (header, dados) in enumerate(tabelas):
        mp = _mapa_colunas(header)  # {texto_col: chave}
        diag["tabelas"].append({"i": i, "linhas": len(dados),
                                "colunas": header[:20],
                                "mapeadas": mp})
        tem = set(mp.values())
        if {"codigo", "taxa_indicativa"} <= tem:
            if melhor is None or len(dados) > len(melhor[1]):
                melhor = (header, dados, mp, i)
    if melhor is None:
        return None, diag
    header, dados, mp, mi = melhor
    # indice de cada chave na lista de celulas
    pos = {}
    for j, col in enumerate(header):
        if col in mp and mp[col] not in pos:
            pos[mp[col]] = j

    def g(cels, k):
        j = pos.get(k)
        if j is None or j >= len(cels):
            return None
        v = cels[j].strip()
        return v if v not in ("", "-", "--") else None

    registros = []
    for cels in dados:
        cod = g(cels, "codigo")
        if not cod or _norm(cod) in ("nan", "codigo", "total"):
            continue
        idx = g(cels, "indexador")
        registros.append({
            "codigo": cod,
            "tipo": _infere_tipo(cod, idx or ""),
            "emissor": g(cels, "emissor"),
            "securitizadora": g(cels, "securitizadora"),
            "serie": g(cels, "serie"),
            "vencimento": _data_iso(g(cels, "vencimento")) if g(cels, "vencimento") else None,
            "indexador": idx,
            "taxa_indicativa": _num(g(cels, "taxa_indicativa")),
            "taxa_compra": _num(g(cels, "taxa_compra")),
            "taxa_venda": _num(g(cels, "taxa_venda")),
            "desvio": _num(g(cels, "desvio")),
            "pu": _num(g(cels, "pu")),
            "pct_pu_par": _num(g(cels, "pct_pu_par")),
            "duration": _num(g(cels, "duration")),
            "referencia": _data_iso(g(cels, "referencia")) if g(cels, "referencia") else None,
        })
    diag["tabela_escolhida"] = mi
    diag["colunas_escolhida"] = mp
    return registros, diag


def _resumo(txt: str):
    r = os.environ.get("GITHUB_STEP_SUMMARY")
    if r:
        with open(r, "a", encoding="utf-8") as fh:
            fh.write(txt + "\n")
    print(txt)


def inspecionar():
    html = baixar()
    _resumo(f"## Inspeção Taxas CRI/CRA\n\n- bytes HTML: {len(html):,}\n")
    tabelas = _tabelas_html(html)
    _resumo(f"- nº de tabelas úteis no HTML: {len(tabelas)}\n")
    for i, (header, dados) in enumerate(tabelas):
        mp = _mapa_colunas(header)
        _resumo(f"### tabela {i} — {len(dados)} linhas × {len(header)} colunas\n")
        _resumo("colunas: `" + " | ".join(map(str, header))[:400] + "`\n")
        _resumo("mapeadas: `" + json.dumps(mp, ensure_ascii=False)[:400] + "`\n")
        for l in dados[:3]:
            _resumo("linha: `" + " | ".join(l)[:400] + "`\n")
    # tenta o parse completo p/ ver a contagem final
    regs, diag = parse(html)
    if regs is not None:
        n_cri = sum(1 for r in regs if r["tipo"] == "CRI")
        n_cra = sum(1 for r in regs if r["tipo"] == "CRA")
        _resumo(f"\n**parse OK**: {len(regs)} ativos (CRI {n_cri} · CRA {n_cra}). "
                f"Amostra:\n```\n" +
                "\n".join(json.dumps(r, ensure_ascii=False)[:300] for r in regs[:4]) + "\n```")
    else:
        _resumo("\n**parse não encontrou tabela com codigo+taxa_indicativa.**")
    return 0


def coletar():
    html = baixar()
    registros, diag = parse(html)
    if not registros:
        _resumo("## CRI/CRA — FALHA no parse\n\n```\n" +
                json.dumps(diag, ensure_ascii=False, indent=2)[:2000] + "\n```")
        raise RuntimeError("nao consegui parsear a tabela — ver diagnostico")
    refs = [r["referencia"] for r in registros if r["referencia"]]
    ref = max(refs) if refs else dt.date.today().isoformat()
    n_cri = sum(1 for r in registros if r["tipo"] == "CRI")
    n_cra = sum(1 for r in registros if r["tipo"] == "CRA")
    com_taxa = sum(1 for r in registros if r["taxa_indicativa"] is not None)
    doc = {"fonte": "ANBIMA — Taxas de CRI e CRA (secundário)",
           "url": URL,
           "data_referencia": ref,
           "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
           "n_ativos": len(registros), "n_cri": n_cri, "n_cra": n_cra,
           "com_taxa_indicativa": com_taxa,
           "ativos": registros,
           "diag": {k: v for k, v in diag.items() if k != "tabelas"}}
    DIR_OUT.mkdir(parents=True, exist_ok=True)
    (DIR_OUT / "ultimo.json").write_text(
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    # csv do dia
    import csv
    campos = ["codigo", "tipo", "emissor", "securitizadora", "serie", "vencimento",
              "indexador", "taxa_indicativa", "taxa_compra", "taxa_venda", "desvio",
              "pu", "pct_pu_par", "duration", "referencia"]
    with open(DIR_OUT / f"{ref}.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        for r in registros:
            w.writerow(r)
    _resumo(f"## CRI/CRA — {ref}\n\n"
            f"- ativos: **{len(registros)}** (CRI {n_cri} · CRA {n_cra})\n"
            f"- com taxa indicativa: {com_taxa}\n"
            f"- tabela escolhida: {diag.get('tabela_escolhida')}\n")
    log.info("cri/cra=%d ref=%s", len(registros), ref)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspecionar", action="store_true")
    a = ap.parse_args(argv)
    return inspecionar() if a.inspecionar else coletar()


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
