#!/usr/bin/env python3
"""
Coleta preco e variacao de TODAS as empresas listadas na B3.

Roda no GitHub Actions so com `requests`. Le dados/acoes/universo.json (todas as
listadas: ticker + empresa) e cota cada uma. Cruza com dados/acoes/mapa.json
(emissor de debenture -> ticker) para marcar quem emite debenture - mas a coleta
cobre o mercado inteiro, nao so quem tem debenture.

    python acoes.py
    python acoes.py --so PETR4,VALE3,MGLU3,ITUB4   # ativos de teste (sem token)
    python acoes.py --saida /tmp/x.json            # sem escrever em dados/

Saida: dados/acoes/ultimo.json
    - "acoes": todas as listadas com preco e variacao de 3/6/12m e queda 52s
    - "emissores": o subconjunto emissor de debenture (para o eixo de credito)

FONTE: brapi.dev (primaria) + Yahoo (fallback). Yahoo e Stooq bloqueiam o IP de
datacenter do GitHub (429 / vazio), entao a fonte confiavel e a brapi, uma API
brasileira feita pra rodar de servidor. O token vem da variavel de ambiente
BRAPI_TOKEN (secret do repo); PETR4/VALE3/MGLU3/ITUB4 respondem sem token, o que
permite testar o codigo. Serie ajustada (adjustedClose): provento e desdobramento
nao viram 'queda'. Ponto de troca: serie_brapi / serie_yahoo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("acoes")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados" / "acoes"

BRAPI = "https://brapi.dev/api/quote/{simbolo}"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"
TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Plano GRATUITO do brapi limita o historico a 3 meses (range 3mo; permitidos
# 1d/5d/1mo/3mo). 6m/12m e 1 ano exigem plano pago (Startup). Entao aqui saem
# var_1m e var_3m (janela inteira); var_6m/var_12m/queda_52s ficam None.
JANELA_1M = 21
MIN_PREGOES = 20
FRACAO_MINIMA = 0.70


def variacao(fech: list[float], n: int) -> float | None:
    if len(fech) <= n:
        return None
    base = fech[-1 - n]
    if not base:
        return None
    return round((fech[-1] / base - 1) * 100, 1)


def metricas(fech: list[float]) -> dict:
    saida = {}
    saida["var_1m"] = variacao(fech, JANELA_1M)
    # var_3m = primeiro vs ultimo da janela (o range 3mo E ~3 meses)
    saida["var_3m"] = round((fech[-1] / fech[0] - 1) * 100, 1) if fech and fech[0] else None
    saida["var_6m"] = None   # requer plano pago
    saida["var_12m"] = None  # requer plano pago
    saida["preco"] = round(fech[-1], 2)
    topo = max(fech) if fech else None
    # queda desde a maxima do periodo disponivel (~3 meses), nao 52 semanas
    saida["queda_max_3m"] = round((fech[-1] / topo - 1) * 100, 1) if topo else None
    saida["queda_max_52s"] = None  # requer plano pago (1 ano)
    return saida


# ---- Fonte 1 (primaria): brapi.dev. Serie ajustada. Token via env. ----
def serie_brapi(sessao: requests.Session, ticker: str) -> tuple[list[float], str] | None:
    params = {"range": "3mo", "interval": "1d"}
    if TOKEN:
        params["token"] = TOKEN
    r = sessao.get(BRAPI.format(simbolo=ticker), params=params, timeout=25)
    if r.status_code in (401, 402, 403):
        # sem permissao (token ausente/expirado ou ativo fora do plano)
        raise PermissionError(f"brapi {r.status_code}: {r.text[:120]}")
    r.raise_for_status()
    res = (r.json() or {}).get("results") or []
    if not res:
        return None
    hist = res[0].get("historicalDataPrice") or []
    if not hist:
        return None
    pts = []
    for h in hist:
        v = h.get("adjustedClose")
        if v is None:
            v = h.get("close")
        if v is None or not h.get("date"):
            continue
        pts.append((h["date"], float(v)))
    if not pts:
        return None
    pts.sort(key=lambda x: x[0])
    fech = [v for _, v in pts]
    ultima = dt.datetime.fromtimestamp(pts[-1][0], dt.timezone.utc).date().isoformat()
    return fech, ultima


# ---- Fonte 2 (fallback): Yahoo. So funciona fora do IP do runner. ----
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
    for nome, fn in (("brapi", serie_brapi), ("yahoo", serie_yahoo)):
        for tentativa in (1, 2, 3):
            try:
                s = fn(sessao, ticker)
                break
            except PermissionError as e:
                log.warning("%s/%s: %s", ticker, nome, e)
                s = None
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


def coletar(tickers: list[str], pausa: float = 0.2) -> tuple[dict[str, dict], dict[str, int]]:
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": UA, "Accept": "application/json"})
    saida: dict[str, dict] = {}
    fontes = {"brapi": 0, "yahoo": 0}
    for i, t in enumerate(tickers):
        r = buscar(sessao, t)
        if not r:
            continue
        fech, ultimo, fonte = r
        saida[t] = {**metricas(fech), "atualizado_em": ultimo, "origem": fonte}
        fontes[fonte] += 1
        if i % 25 == 24:
            log.info("%d/%d tickers (brapi %d, yahoo %d)", i + 1, len(tickers),
                     fontes["brapi"], fontes["yahoo"])
        time.sleep(pausa)
    return saida, fontes


def carregar_universo(caminho: pathlib.Path, mapa: list) -> list[dict]:
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

    if not TOKEN:
        log.warning("BRAPI_TOKEN ausente: so os ativos de teste vao cotar.")

    mapa = json.loads(pathlib.Path(a.mapa).read_text(encoding="utf-8"))["mapa"]
    universo = carregar_universo(pathlib.Path(a.universo), mapa)

    emissor_por_ticker: dict[str, list[dict]] = {}
    for x in mapa:
        emissor_por_ticker.setdefault(x["ticker"], []).append(
            {"emissor": x["emissor"], "vinculo": x["vinculo"]})

    uni_tickers = sorted({u["ticker"] for u in universo})
    map_tickers = sorted({x["ticker"] for x in mapa})
    map_roots = {t[:4] for t in map_tickers}
    # cota a UNIAO: linha representativa do universo + linhas especificas do mapa
    # (o mapa usa PN/unit, ex. CMIG4; o universo usa a ON, CMIG3). Sem isso o
    # subconjunto de credito perde emissores cujo ticker do mapa nao foi cotado.
    tickers = sorted(set(uni_tickers) | set(map_tickers))
    if a.so:
        alvo = {t.strip().upper() for t in a.so.split(",")}
        tickers = [t for t in tickers if t in alvo] or sorted(alvo)

    log.info("coletando %d tickers (universo inteiro da B3)", len(tickers))
    cot, fontes = coletar(tickers)

    nome_por_ticker = {u["ticker"]: u["empresa"] for u in universo}
    # lista de mercado: uma linha por empresa (linha representativa do universo);
    # emite_debenture casa por RAIZ (CMIG3 do universo == CMIG4 do mapa).
    acoes = []
    for t in uni_tickers:
        if t not in cot:
            continue
        acoes.append({
            "ticker": t,
            "empresa": nome_por_ticker.get(t, t),
            "emite_debenture": t[:4] in map_roots,
            **cot[t],
        })

    emissores = [
        {"emissor": x["emissor"], "ticker": x["ticker"], "vinculo": x["vinculo"],
         **{k: cot[x["ticker"]][k] for k in
            ("preco", "var_1m", "var_3m", "var_6m", "var_12m",
             "queda_max_3m", "queda_max_52s", "atualizado_em")}}
        for x in mapa if x["ticker"] in cot
    ]
    ref = max((v["atualizado_em"] for v in cot.values()), default=None)

    doc = {
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_referencia": ref,
        "fonte": "brapi.dev plano gratuito (historico 3 meses) - fechamento ajustado",
        "limite_plano": "brapi free: range max 3mo; 6m/12m/52s exigem plano pago",
        "universo_pedido": len(tickers),
        "obtidos": len(cot),
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
    log.info("%d/%d cotados (brapi %d, yahoo %d) | %d emissoras | ref %s",
             len(cot), len(tickers), fontes["brapi"], fontes["yahoo"], len(emissores), ref)
    if faltando:
        log.warning("sem cotacao (%d): %s", len(faltando), " ".join(faltando[:60]))
    if len(cot) < FRACAO_MINIMA * len(tickers):
        log.error("menos de %.0f%% cotaram", FRACAO_MINIMA * 100)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
