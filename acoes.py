#!/usr/bin/env python3
"""
Coleta a variacao da acao dos emissores de debenture - eixo de alerta do radar.

Mesmo proposito dos outros coletores deste repositorio: rodar no GitHub Actions
sem instalar nada alem de `requests`. Le dados/acoes/mapa.json (emissor da
ANBIMA -> ticker da B3, curado a mao) e grava a variacao de 3, 6 e 12 meses
mais a queda desde a maxima de 52 semanas.

    python acoes.py
    python acoes.py --so CSNA3,HAPV3     # depurar um punhado de tickers
    python acoes.py --saida /tmp/x.json  # sem escrever em dados/

Saida:
    dados/acoes/ultimo.json   <- o dashboard le este

Como o dashboard usa: exatamente como usa acao de agencia de rating - eixo de
10% na triagem, chip de alerta, selo na tabela. Nunca como causa do movimento
de spread. Emissor ausente deste arquivo entra com ZERO no eixo, nao com
penalidade: boa parte do universo e SPE de saneamento e concessao, que nao tem
equity negociado, e penalizar por isso seria medir quem e listado, nao quem
esta em stress.
"""

from __future__ import annotations

import argparse
import datetime as dt
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

CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{simbolo}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Pregoes por janela - aproximacao usual da B3 (21 por mes). A janela e contada
# em pregoes da PROPRIA serie, nao em data de calendario: papel que nao negociou
# em alguns dias encurta a janela em vez de desloca-la.
JANELAS = {"var_3m": 63, "var_6m": 126, "var_12m": 252}
JANELA_MAX = 252

# Serie mais curta que isto nao sustenta nem a janela de 3 meses.
MIN_PREGOES = 40

# Abaixo disto a coleta degradou e o painel publicaria meio eixo de alerta sem
# ninguem perceber. Melhor o Actions acusar.
FRACAO_MINIMA = 0.80


def variacao(fech: list[float], n: int) -> float | None:
    """Variacao percentual contra o fechamento de n pregoes atras."""
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


def serie_yahoo(sessao: requests.Session, ticker: str) -> tuple[list[float], str] | None:
    """13 meses de fechamento ajustado. Ajustado, e nao o fechamento cru, para
    que provento e desdobramento nao virem 'queda' no eixo de alerta."""
    r = sessao.get(
        CHART.format(simbolo=f"{ticker}.SA"),
        params={"range": "13mo", "interval": "1d"},
        timeout=20,
    )
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


def coletar(tickers: list[str], pausa: float = 0.4) -> dict[str, dict]:
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": UA, "Accept": "application/json"})

    saida: dict[str, dict] = {}
    for i, t in enumerate(tickers):
        for tentativa in (1, 2, 3):
            try:
                s = serie_yahoo(sessao, t)
                break
            except Exception as e:                       # rede, 429, JSON torto
                if tentativa == 3:
                    log.warning("%s: %s", t, e)
                    s = None
                else:
                    time.sleep(2 * tentativa)
        if not s:
            continue
        fech, ultimo = s
        if len(fech) < MIN_PREGOES:
            log.warning("%s: so %d pregoes, ignorando", t, len(fech))
            continue
        saida[t] = {**metricas(fech), "atualizado_em": ultimo}
        if i % 20 == 19:
            log.info("%d/%d tickers", i + 1, len(tickers))
        time.sleep(pausa)
    return saida


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapa", default=str(DADOS / "mapa.json"))
    ap.add_argument("--saida", default=str(DADOS / "ultimo.json"))
    ap.add_argument("--so", help="lista de tickers separada por virgula, para depurar")
    a = ap.parse_args(argv)

    mapa = json.loads(pathlib.Path(a.mapa).read_text(encoding="utf-8"))["mapa"]
    tickers = sorted({x["ticker"] for x in mapa})
    if a.so:
        alvo = {t.strip().upper() for t in a.so.split(",")}
        tickers = [t for t in tickers if t in alvo]

    log.info("coletando %d tickers", len(tickers))
    cot = coletar(tickers)

    # Emissor que falhou na coleta e OMITIDO, nunca gravado com zero: no painel
    # a ausencia vale zero no eixo, mas um zero gravado seria indistinguivel de
    # "acao estavel", que e afirmacao diferente.
    emissores = [
        {"emissor": x["emissor"], "ticker": x["ticker"], "vinculo": x["vinculo"],
         **cot[x["ticker"]]}
        for x in mapa if x["ticker"] in cot
    ]
    ref = max((e["atualizado_em"] for e in emissores), default=None)

    doc = {
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_referencia": ref,
        "fonte": "Yahoo Finance - fechamento ajustado, tickers .SA",
        "tickers_pedidos": len(tickers),
        "tickers_obtidos": len(cot),
        "emissores": emissores,
    }
    saida = pathlib.Path(a.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")

    faltando = sorted(set(tickers) - set(cot))
    log.info("%d emissores, %d/%d tickers, referencia %s",
             len(emissores), len(cot), len(tickers), ref)
    if faltando:
        log.warning("sem cotacao: %s", " ".join(faltando))
    if len(cot) < FRACAO_MINIMA * len(tickers):
        log.error("menos de %.0f%% dos tickers retornaram", FRACAO_MINIMA * 100)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
