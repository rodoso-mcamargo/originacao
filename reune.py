#!/usr/bin/env python3
"""
Coletor REUNE/ANBIMA - negocios registrados de debentures (previas publicas).

Complementa o monitor.py. Sao dados diferentes:
  monitor.py -> TAXA INDICATIVA, marcacao de consenso, 1x/dia, D-1
  reune.py   -> NEGOCIOS REGISTRADOS: taxa minima/media/maxima, PU min/med/max
                e FAIXA de volume negociado; 4 previas por dia (11h,13h,16h,18h)
                mais o acumulado; historico da fonte desde 01/01/2018

    python reune.py                    # ultimo dia util, acumulado
    python reune.py --data 2026-09-02
    python reune.py --periodo 16H00
    python reune.py --backfill 30
    python reune.py --inspecionar      # so descreve a resposta da API, nao grava

RESSALVA DE USO (aviso da propria pagina da ANBIMA, registrado aqui de proposito):
  "As informacoes divulgadas pelo SISTEMA REUNE devem ser utilizadas como mera
   referencia de mercado, nao constituindo indicacao ou recomendacao para tomada
   de decisoes e, ainda, nao devem ser utilizadas na composicao de indices de
   mercado ou distribuidas para qualquer finalidade comercial, pelo usuario."

Saidas:
    dados/reune/AAAA-MM-DD.csv   negocios do dia
    dados/reune/ultimo.json      ultimo dia + diagnostico
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import pathlib
import socket
import sys
import traceback

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("reune")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
DIR_REUNE = DADOS / "reune"

# identitytoolkit.googleapis.com publica AAAA e o runner do GitHub nao tem rota
# IPv6 - a chamada morreria com ENETUNREACH. Mesma armadilha que derrubou o
# coletor da CVM. A API da ANBIMA e IPv4-only, mas forcar aqui cobre as duas.
try:
    import urllib3.util.connection as _u3conn
    _u3conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

# Configuracao publica do Firebase, lida do env-config.js da propria pagina.
FIREBASE_KEY = "AIzaSyArrpcrNGQ9fa7xbJXlJeReBkr0dGng7FY"
URL_TOKEN = ("https://identitytoolkit.googleapis.com/v1/accounts:signUp"
             f"?key={FIREBASE_KEY}")
URL_API = "https://data-api.prd.anbima.com.br/web-bff/v1/reune/previas"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")
TIMEOUT = 60

PERIODOS = ("ACUMULADO", "11H00", "13H00", "16H00", "18H00")

# A resposta nunca foi inspecionada de dentro do Python - o levantamento foi
# feito pelo navegador e so revelou o endpoint. Por isso cada campo aceita
# varios nomes e existe o modo --inspecionar.
ALIASES = {
    "codigo":      ["codigo", "codigoAtivo", "cetip", "codigoCetip", "ativo"],
    "bvmf":        ["bvmf", "codigoBvmf"],
    "isin":        ["isin", "codigoIsin"],
    "agrupamento": ["agrupamento", "agrupamentoOperacoes", "tipoAgrupamento"],
    "taxa_min":    ["taxaMinima", "taxa_minima", "txMinima", "minimaTaxa"],
    "taxa_med":    ["taxaMedia", "taxa_media", "txMedia", "mediaTaxa"],
    "taxa_max":    ["taxaMaxima", "taxa_maxima", "txMaxima", "maximaTaxa"],
    "pu_min":      ["puMinimo", "pu_minimo", "precoUnitarioMinimo", "minimaPu"],
    "pu_med":      ["puMedio", "pu_medio", "precoUnitarioMedio", "mediaPu"],
    "pu_max":      ["puMaximo", "pu_maximo", "precoUnitarioMaximo", "maximaPu"],
    "faixa_vol":   ["faixaVolume", "faixaVolumeNegociado", "volumeNegociado",
                    "faixa_volume_negociado"],
}

COLS = ["data", "periodo", "codigo", "bvmf", "isin", "agrupamento",
        "taxa_min", "taxa_med", "taxa_max", "pu_min", "pu_med", "pu_max",
        "faixa_vol"]


def ultimo_dia_util(ref: dt.date | None = None) -> dt.date:
    d = (ref or dt.date.today()) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def token_anonimo(sessao: requests.Session) -> str:
    """Autenticacao anonima no Firebase - o mesmo que o navegador faz ao abrir a pagina."""
    r = sessao.post(URL_TOKEN, json={"returnSecureToken": True}, timeout=TIMEOUT)
    r.raise_for_status()
    tk = r.json().get("idToken")
    if not tk:
        raise RuntimeError(f"Firebase nao devolveu idToken: {r.text[:200]}")
    log.info("token anonimo obtido (%d caracteres)", len(tk))
    return tk


def buscar(sessao: requests.Session, token: str, data: dt.date, periodo: str):
    """Devolve (payload, formato_de_cabecalho_que_funcionou) ou (None, motivo)."""
    params = {
        "data-previa": data.isoformat(),
        "periodo-previa": periodo,
        "codigo-ativo": "DEB",
    }
    # O navegador manda 'g-google-authorization'; nao ficou claro se com prefixo
    # Bearer. Tenta as duas formas e para na primeira que responder 200.
    tentativas = [
        ("g-google-authorization", token),
        ("g-google-authorization", f"Bearer {token}"),
        ("Authorization", f"Bearer {token}"),
    ]
    ultimo = None
    for nome, valor in tentativas:
        h = {"accept": "application/json", "User-Agent": UA, nome: valor}
        r = sessao.get(URL_API, params=params, headers=h, timeout=TIMEOUT)
        ultimo = f"HTTP {r.status_code} com {nome}"
        if r.status_code == 200:
            log.info("%s %s: 200 usando cabecalho %s", data, periodo, nome)
            try:
                return r.json(), nome
            except ValueError:
                return None, f"200 mas resposta nao e JSON: {r.text[:150]}"
        if r.status_code == 404:
            return None, "404 - sem previa publicada para esta data/periodo"
    return None, ultimo or "sem resposta"


def achar_registros(payload):
    """A lista de negocios pode vir na raiz ou aninhada; procura a maior lista de dicts."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    melhor = []
    pilha = [payload]
    while pilha:
        n = pilha.pop()
        if isinstance(n, dict):
            pilha.extend(n.values())
        elif isinstance(n, list):
            if n and isinstance(n[0], dict) and len(n) > len(melhor):
                melhor = n
            pilha.extend(x for x in n if isinstance(x, (dict, list)))
    return melhor


def resolver(registro: dict) -> dict:
    presentes = {k.lower(): k for k in registro}
    mapa = {}
    for logico, cands in ALIASES.items():
        for c in cands:
            if c.lower() in presentes:
                mapa[logico] = presentes[c.lower()]
                break
    return mapa


def inspecionar(payload) -> None:
    print("## Estrutura da resposta do REUNE\n")
    if isinstance(payload, dict):
        print("Chaves da raiz: `" + "`, `".join(payload.keys()) + "`\n")
    regs = achar_registros(payload)
    print(f"Registros encontrados: **{len(regs)}**\n")
    if not regs:
        print("```\n" + json.dumps(payload, ensure_ascii=False)[:1500] + "\n```\n")
        return
    print("Campos do primeiro registro: `" + "`, `".join(regs[0].keys()) + "`\n")
    print("Aliases resolvidos: `" + json.dumps(resolver(regs[0])) + "`\n")
    print("Primeiro registro completo:\n")
    print("```json\n" + json.dumps(regs[0], ensure_ascii=False, indent=1)[:1200] + "\n```\n")


def processar(payload, data: dt.date, periodo: str) -> dict:
    regs = achar_registros(payload)
    linhas, mapa = [], {}
    for r in regs:
        if not mapa:
            mapa = resolver(r)
        linha = {"data": data.isoformat(), "periodo": periodo}
        for logico in ALIASES:
            k = mapa.get(logico)
            v = r.get(k) if k else None
            linha[logico] = "" if v is None else str(v).strip()
        if linha["codigo"]:
            linhas.append(linha)

    DIR_REUNE.mkdir(parents=True, exist_ok=True)
    if linhas:
        arq = DIR_REUNE / f"{data.isoformat()}.csv"
        with arq.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(linhas)
        log.info("%s %s: %d negocios -> %s", data, periodo, len(linhas), arq.name)

    return {
        "data_referencia": data.isoformat(),
        "periodo": periodo,
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "diagnostico": {
            "registros_brutos": len(regs),
            "linhas_gravadas": len(linhas),
            "mapa_campos": mapa,
            "campos_originais": list(regs[0].keys()) if regs else [],
        },
        "negocios": linhas,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD")
    ap.add_argument("--periodo", default="ACUMULADO", choices=PERIODOS)
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--inspecionar", action="store_true",
                    help="descreve a resposta da API e sai, sem gravar")
    a = ap.parse_args(argv)

    s = requests.Session()
    token = token_anonimo(s)

    if a.inspecionar:
        d = dt.date.fromisoformat(a.data) if a.data else ultimo_dia_util()
        payload, como = buscar(s, token, d, a.periodo)
        if payload is None:
            print(f"## REUNE {d} {a.periodo}\n\nNao foi possivel obter: **{como}**\n")
            return 1
        print(f"**{d} - {a.periodo} - cabecalho `{como}`**\n")
        inspecionar(payload)
        return 0

    if a.backfill:
        datas, d = [], dt.date.today()
        for _ in range(a.backfill):
            d = ultimo_dia_util(d)
            datas.append(d)
        datas.reverse()
    else:
        datas = [dt.date.fromisoformat(a.data) if a.data else ultimo_dia_util()]

    ok, ultimo = 0, None
    for d in datas:
        payload, como = buscar(s, token, d, a.periodo)
        if payload is None:
            log.warning("%s %s pulado: %s", d, a.periodo, como)
            continue
        ultimo = processar(payload, d, a.periodo)
        ok += 1

    if ultimo:
        (DIR_REUNE / "ultimo.json").write_text(
            json.dumps(ultimo, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
    log.info("concluido: %d de %d dias", ok, len(datas))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log.error("FALHA NAO TRATADA\n%s", tb)
        resumo = os.environ.get("GITHUB_STEP_SUMMARY")
        if resumo:
            with open(resumo, "a", encoding="utf-8") as fh:
                fh.write("## Falha na coleta do REUNE\n\n```\n" + tb + "\n```\n")
        sys.exit(1)
