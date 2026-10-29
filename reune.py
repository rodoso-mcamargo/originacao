#!/usr/bin/env python3
"""
Coletor REUNE/ANBIMA - negocios registrados de debentures (previas publicas).

ABORDAGEM: navegador headless (Playwright). A pagina
`data.anbima.com.br/reune/previas/debentures` e um app que se autentica sozinho
(token do Firebase gerado pelo proprio JS da pagina). Em vez de reproduzir esse
token - que nao foi possivel e nao deve ser extraido da aplicacao - abrimos a
pagina de verdade num Chromium headless, deixamos o site fazer o login dele, e
CAPTURAMOS a resposta JSON que a propria pagina busca para montar a tabela.
E leitura do que o site publicamente exibe, feita como um visitante.

  monitor.py -> TAXA INDICATIVA, marcacao de consenso, 1x/dia, D-1
  reune.py   -> NEGOCIOS REGISTRADOS: taxa min/med/max, PU min/med/max e FAIXA
                de volume; previas 11h/13h/16h/18h + acumulado; desde 2018

    python reune.py                    # ultimo dia util, acumulado
    python reune.py --data 2026-09-02
    python reune.py --periodo 16H00
    python reune.py --inspecionar      # descreve a captura, nao grava

RESSALVA DE USO (aviso da propria pagina da ANBIMA, registrado de proposito):
  "As informacoes divulgadas pelo SISTEMA REUNE devem ser utilizadas como mera
   referencia de mercado (...) e, ainda, nao devem ser utilizadas na composicao
   de indices de mercado ou distribuidas para qualquer finalidade comercial,
   pelo usuario."

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
import re
import sys
import traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("reune")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
DIR_REUNE = DADOS / "reune"

PAGINA = "https://data.anbima.com.br/reune/previas/debentures"
# Toda resposta cuja URL casa com isto e candidata a carregar os negocios.
PADRAO_API = re.compile(r"/reune/previas", re.I)

PERIODOS = ("ACUMULADO", "11H00", "13H00", "16H00", "18H00")
ROTULO_ABA = {"ACUMULADO": "Acumulado", "11H00": "11h00", "13H00": "13h00",
              "16H00": "16h00", "18H00": "18h00"}
# Valor de periodo-previa na URL da API (o acumulado sai como 24H00).
PERIODO_API = {"ACUMULADO": "24H00"}
# Nomes como aparecem no aria-label dos dias do calendario (react-datepicker).
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro")

# A resposta nunca foi vista de dentro do Python; o parser aceita varios nomes.
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


def achar_registros(payload):
    """Maior lista de dicts em qualquer nivel do JSON."""
    if isinstance(payload, list):
        return payload if payload and isinstance(payload[0], dict) else []
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


def capturar(data: dt.date, periodo: str, inspecionar: bool = False):
    """
    Abre a pagina num Chromium headless, seleciona data e periodo, e devolve
    (payload_json, diagnostico). payload pode ser None se nada foi capturado.
    """
    from playwright.sync_api import sync_playwright

    capturas: list[dict] = []
    diag = {"respostas_reune": 0, "urls": [], "erro": None}
    alvo_data = data.isoformat()
    alvo_periodo = PERIODO_API.get(periodo, periodo)

    with sync_playwright() as p:
        navegador = p.chromium.launch(args=["--no-sandbox"])
        ctx = navegador.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 900},
        )
        pagina = ctx.new_page()

        def ao_responder(resp):
            try:
                if not PADRAO_API.search(resp.url):
                    return
                diag["respostas_reune"] += 1
                if resp.url not in diag["urls"]:
                    diag["urls"].append(resp.url)
                if resp.status != 200:
                    return
                # A pagina sempre carrega primeiro a data de hoje; so vale a
                # resposta da data e do periodo pedidos.
                if f"data-previa={alvo_data}" not in resp.url or \
                        f"periodo-previa={alvo_periodo}" not in resp.url:
                    return
                corpo = resp.json()
                regs = achar_registros(corpo)
                if regs:
                    capturas.append({"url": resp.url, "n": len(regs), "payload": corpo})
            except Exception as e:  # resposta nao-JSON, corpo ja consumido, etc.
                log.debug("resposta ignorada (%s): %s", resp.url, e)

        pagina.on("response", ao_responder)

        log.info("abrindo %s", PAGINA)
        pagina.goto(PAGINA, wait_until="domcontentloaded", timeout=90_000)
        pagina.wait_for_timeout(4000)

        _selecionar_data(pagina, data)
        pagina.wait_for_timeout(3000)
        _selecionar_periodo(pagina, periodo)

        # Espera a resposta chegar (ou o "sem previas" aparecer).
        for _ in range(20):
            pagina.wait_for_timeout(1000)
            if capturas:
                break

        ctx.close()
        navegador.close()

    if not capturas:
        # Fallback: raspar a tabela renderizada (caso a resposta nao seja JSON
        # ou venha por outro caminho). Retorna lista de dicts "brutos".
        return None, diag

    # Escolhe a captura com mais registros (a que carregou a grade cheia).
    melhor = max(capturas, key=lambda c: c["n"])
    diag["capturada"] = melhor["url"]
    diag["registros_capturados"] = melhor["n"]
    return melhor["payload"], diag


def _selecionar_data(pagina, data: dt.date) -> None:
    """
    Escolhe a data de referencia no calendario da pagina (react-datepicker).
    O campo nao aceita digitacao: clicar nele abre o calendario e redesenha o
    input. Cada dia tem aria-label "Choose quarta-feira, 28 de outubro de 2026";
    volta mes a mes ate o dia aparecer.
    """
    rotulo = f", {data.day} de {MESES[data.month - 1]} de {data.year}"
    dia = pagina.locator(
        f".react-datepicker__day[aria-label$='{rotulo}']"
        ":not(.react-datepicker__day--outside-month)")
    try:
        pagina.locator("input[type='text']").first.click()
        pagina.locator(".react-datepicker").first.wait_for(timeout=10_000)
        for _ in range(13):
            if dia.count():
                break
            pagina.locator(".react-datepicker__navigation--previous").first.click()
            pagina.wait_for_timeout(300)
        dia.first.click()
        log.info("data selecionada: %s", data.strftime("%d/%m/%Y"))
    except Exception as e:
        log.warning("nao consegui escolher a data (%s): sigo com a padrao", e)


def _selecionar_periodo(pagina, periodo: str) -> None:
    rot = ROTULO_ABA.get(periodo, "Acumulado")
    try:
        aba = pagina.locator("button, a, [role='tab']").filter(
            has_text=re.compile(rf"^\s*{rot}\s*$", re.I))
        if aba.count():
            aba.first.click()
            log.info("aba de periodo selecionada: %s", rot)
        else:
            log.info("aba '%s' nao encontrada; usando o padrao da pagina", rot)
    except Exception as e:
        log.warning("nao consegui clicar no periodo (%s)", e)


def processar(payload, data: dt.date, periodo: str, diag: dict) -> dict:
    regs = achar_registros(payload) if payload is not None else []
    linhas, mapa = [], {}
    for r in regs:
        if not isinstance(r, dict):
            continue
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

    saida = {
        "data_referencia": data.isoformat(),
        "periodo": periodo,
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "diagnostico": {
            "respostas_reune_vistas": diag.get("respostas_reune", 0),
            "urls_reune": diag.get("urls", []),
            "registros_capturados": diag.get("registros_capturados", 0),
            "linhas_gravadas": len(linhas),
            "mapa_campos": mapa,
            "campos_originais": list(regs[0].keys()) if regs else [],
        },
        "negocios": linhas,
    }
    if linhas or payload is not None:
        (DIR_REUNE / "ultimo.json").write_text(
            json.dumps(saida, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
    return saida


def inspecionar(payload, diag) -> None:
    print("## Captura do REUNE (navegador headless)\n")
    print(f"- Respostas /reune/previas vistas: {diag.get('respostas_reune', 0)}")
    for u in diag.get("urls", [])[:6]:
        print(f"  - `{u}`")
    if payload is None:
        print("\n**Nenhum JSON com registros foi capturado.**")
        if diag.get("erro"):
            print(f"\nErro: {diag['erro']}")
        return
    regs = achar_registros(payload)
    print(f"\nRegistros no payload: **{len(regs)}**\n")
    if regs:
        print("Campos do primeiro registro: `" + "`, `".join(regs[0].keys()) + "`\n")
        print("Aliases resolvidos: `" + json.dumps(resolver(regs[0])) + "`\n")
        print("```json\n" + json.dumps(regs[0], ensure_ascii=False, indent=1)[:1200] + "\n```")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD")
    ap.add_argument("--periodo", default="ACUMULADO", choices=PERIODOS)
    ap.add_argument("--inspecionar", action="store_true")
    a = ap.parse_args(argv)

    d = dt.date.fromisoformat(a.data) if a.data else ultimo_dia_util()
    payload, diag = capturar(d, a.periodo, a.inspecionar)

    if a.inspecionar:
        inspecionar(payload, diag)
        return 0 if payload is not None else 1

    saida = processar(payload, d, a.periodo, diag)
    n = saida["diagnostico"]["linhas_gravadas"]
    if n == 0:
        log.warning("%s %s: nada capturado (previa nao publicada, feriado, "
                    "ou o layout da pagina mudou)", d, a.periodo)
        return 1
    return 0


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
