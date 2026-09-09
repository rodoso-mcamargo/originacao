#!/usr/bin/env python3
"""
Coleta preco e variacao de TODAS as empresas listadas na B3.

Roda no GitHub Actions so com `requests`. Le dados/acoes/universo.json (todas as
listadas: ticker + empresa) e cota cada uma. Alem disso cruza com
dados/acoes/mapa.json (emissor de debenture -> ticker) para marcar quais empresas
sao emissoras de debentura no radar de credito - mas a coleta cobre o mercado
inteiro, nao so quem tem debenture.

    python acoes.py
    python acoes.py --so CSNA3,HAPV3     # depurar um punhado de tickers
    python acoes.py --saida /tmp/x.json  # sem escrever em dados/

Saida: dados/acoes/ultimo.json
    - "acoes": todas as listadas com preco e variacao de 3/6/12m e queda 52s
    - "emissores": o subconjunto que e emissor de debenture (para o eixo de credito)

FONTE DUPLA: o Yahoo passou a devolver 0 do IP do runner (exige cookie/crumb ou
bloqueia datacenter). Primaria = STOOQ (CSV diario, sem login, amigavel a CI),
Yahoo = fallback ja com handshake de cookie. Serie ajustada nas duas: provento e
desdobramento nao viram 'queda'. Ponto de troca: serie_stooq / serie_yahoo.
"""

from __future__ import annotations

import argparse
import csv as csvmod
import datetime as dt
import io
import json
import logging
import pathlib
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("acoes")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados" / "acoes"

STOOQ = "https://stooq.com/q/d/l/"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"
CRUMB = "https://query1.finance.yahoo.com/v1/test/getcrumb"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

JANELAS = {"var_3m": 63, "var_6m": 126, "var_12m": 252}
JANELA_MAX = 252
MIN_PREGOES = 40
FRACAO_MINIMA = 0.70   # universo grande tem small caps ilíquidas; 70% ja e saudavel


def variacao(fech: list[float], n: int) -> float | None:
    if len(fech) <= n:
        return None
    base = fech[-1 - n]
    if not base:
        return None
    return round((fech[-1] / base - 1) * 100, 1)


def metricas(fech: list[float]) -> dict:
    saida = {k: variacao(fech, n) for k, n in JANELAS.items()}
    janela = fech[-JANELA_MAX:]
    topo = max(janela) if janela else None
    saida["preco"] = round(fech[-1], 2)
    saida["queda_max_52s"] = round((fech[-1] / topo - 1) * 100, 1) if topo else None
    return saida


# ---- Fonte 1 (primaria): STOOQ. CSV diario ajustado, sem login. ----
def serie_stooq(sessao: requests.Session, ticker: str) -> tuple[list[float], str] | None:
    r = sessao.get(STOOQ, params={"s": f"{ticker.lower()}.sa", "i": "d"}, timeout=25)
    r.raise_for_status()
    texto = r.text.strip()
    if not texto or texto.upper().startswith("N/D") or "<html" in texto[:200].lower():
        return None
    linhas = list(csvmod.reader(io.StringIO(texto)))
    if len(linhas) < 2 or "Close" not in linhas[0]:
        return None
    icol = linhas[0].index("Close")
    idata = linhas[0].index("Date")
    fech: list[float] = []
    ultima_data = None
    for ln in linhas[1:]:
        if len(ln) <= icol:
            continue
        v = ln[icol].strip()
        if not v or v in ("N/D", "null"):
            continue
        try:
            fech.append(float(v))
            ultima_data = ln[idata].strip()
        except ValueError:
            continue
    if not fech or not ultima_data:
        return None
    return fech, ultima_data


# ---- Fonte 2 (fallback): YAHOO. Fechamento ajustado, com handshake de cookie. ----
def aquecer_yahoo(sessao: requests.Session) -> None:
    for url in ("https://fc.yahoo.com", "https://finance.yahoo.com"):
        try:
            sessao.get(url, timeout=15)
        except Exception:
            pass
    try:
        c = sessao.get(CRUMB, timeout=15)
        sessao.headers["x-yahoo-crumb"] = c.text.strip()
    except Exception:
        pass


def serie_yahoo(sessao: requests.Session, ticker: str) -> tuple[list[float], str] | None:
    r = sessao.get(CHART.format(simbolo=f"{ticker}.SA"),
                   params={"range": "13mo", "interval": "1d"}, timeout=20)
    r.raise_for_status()
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        return None
    bloco = res[0]
    ind = bloco.get("indicators") or {}
    aj = (ind.get("adjclose") or [{}])[0].get("adjclose")
    cru = (ind.get("quote") or [{}])[0].get("close")
    valores = aj if aj else cru
    carimbos = bloco.get("timestamp") or []
    if not valores or not carimbos:
        return None
    pares = [(t, v) for t, v in zip(carimbos, valores) if v is not None]
    if not pares:
        return None
    fech = [float(v) for _, v in pares]
    ultimo = dt.datetime.fromtimestamp(pares[-1][0], dt.timezone.utc).date().isoformat()
    return fech, ultimo


def buscar(sessao: requests.Session, ticker: str) -> tuple[list[float], str, str] | None:
    for nome, fn in (("stooq", serie_stooq), ("yahoo", serie_yahoo)):
        for tentativa in (1, 2, 3):
            try:
                s = fn(sessao, ticker)
                break
            except Exception as e:
                if tentativa == 3:
                    log.warning("%s/%s: %s", ticker, nome, e)
                    s = None
                else:
                    time.sleep(1.5 * tentativa)
        if s and len(s[0]) >= MIN_PREGOES:
            return s[0], s[1], nome
    return None


def coletar(tickers: list[str], pausa: float = 0.25) -> tuple[dict[str, dict], dict[str, int]]:
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": UA, "Accept": "text/csv, application/json, */*"})
    aquecer_yahoo(sessao)
    saida: dict[str, dict] = {}
    fontes = {"stooq": 0, "yahoo": 0}
    for i, t in enumerate(tickers):
        r = buscar(sessao, t)
        if not r:
            continue
        fech, ultimo, fonte = r
        saida[t] = {**metricas(fech), "atualizado_em": ultimo, "origem": fonte}
        fontes[fonte] += 1
        if i % 25 == 24:
            log.info("%d/%d tickers", i + 1, len(tickers))
        time.sleep(pausa)
    return saida, fontes


def carregar_universo(caminho: pathlib.Path, mapa: list) -> list[dict]:
    """Universo = todas as listadas (universo.json). Fallback: tickers do mapa."""
    if caminho.exists():
        emp = json.loads(caminho.read_text(encoding="utf-8"))["empresas"]
        return [{"ticker": e["ticker"], "empresa": e["empresa"]} for e in emp]
    log.warning("universo.json ausente; usando so os tickers do mapa")
    vistos, out = set(), []
    for x in mapa:
        if x["ticker"] not in vistos:
            vistos.add(x["ticker"])
            out.append({"ticker": x["ticker"], "empresa": x["emissor"]})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapa", default=str(DADOS / "mapa.json"))
    ap.add_argument("--universo", default=str(DADOS / "universo.json"))
    ap.add_argument("--saida", default=str(DADOS / "ultimo.json"))
    ap.add_argument("--so", help="lista de tickers separada por virgula, para depurar")
    a = ap.parse_args(argv)

    mapa = json.loads(pathlib.Path(a.mapa).read_text(encoding="utf-8"))["mapa"]
    universo = carregar_universo(pathlib.Path(a.universo), mapa)

    # emissor de debenture por ticker (para marcar no resultado)
    emissor_por_ticker: dict[str, list[dict]] = {}
    for x in mapa:
        emissor_por_ticker.setdefault(x["ticker"], []).append(
            {"emissor": x["emissor"], "vinculo": x["vinculo"]})

    tickers = sorted({u["ticker"] for u in universo})
    if a.so:
        alvo = {t.strip().upper() for t in a.so.split(",")}
        tickers = [t for t in tickers if t in alvo]

    log.info("coletando %d tickers (universo inteiro da B3)", len(tickers))
    cot, fontes = coletar(tickers)

    nome_por_ticker = {u["ticker"]: u["empresa"] for u in universo}
    acoes = []
    for t in sorted(cot):
        emissoras = emissor_por_ticker.get(t, [])
        acoes.append({
            "ticker": t,
            "empresa": nome_por_ticker.get(t, t),
            "emite_debenture": bool(emissoras),
            **cot[t],
        })

    # subconjunto emissor-por-emissor, para o eixo de credito (contrato antigo)
    emissores = [
        {"emissor": x["emissor"], "ticker": x["ticker"], "vinculo": x["vinculo"],
         **{k: cot[x["ticker"]][k] for k in
            ("preco", "var_3m", "var_6m", "var_12m", "queda_max_52s", "atualizado_em")}}
        for x in mapa if x["ticker"] in cot
    ]
    ref = max((v["atualizado_em"] for v in cot.values()), default=None)

    doc = {
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_referencia": ref,
        "fonte": "Stooq (primaria) + Yahoo (fallback) - fechamento ajustado, tickers .SA",
        "universo_pedido": len(tickers),
        "obtidos": len(cot),
        # aliases p/ compatibilidade com o passo Resumo do workflow
        "tickers_pedidos": len(tickers),
        "tickers_obtidos": len(cot),
        "por_fonte": fontes,
        "acoes": acoes,
        "emissores": emissores,
    }
    saida = pathlib.Path(a.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")

    faltando = sorted(set(tickers) - set(cot))
    log.info("%d/%d cotados (stooq %d, yahoo %d) | %d emissoras de debenture | ref %s",
             len(cot), len(tickers), fontes["stooq"], fontes["yahoo"], len(emissores), ref)
    if faltando:
        log.warning("sem cotacao (%d): %s", len(faltando), " ".join(faltando))
    if len(cot) < FRACAO_MINIMA * len(tickers):
        log.error("menos de %.0f%% cotaram", FRACAO_MINIMA * 100)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
