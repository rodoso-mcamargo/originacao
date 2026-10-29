# -*- coding: utf-8 -*-
"""Coletor de caracteristicas e agenda de eventos das debentures (SND / debentures.com.br).

Fonte: www.debentures.com.br (ANBIMA/B3). A propria pagina de consulta oferece
exportacao em lote ("texto delimitado por Tab"), usada aqui em vez de pagina por codigo:
  caracteristicas: exploreosnd/consultaadados/emissoesdedebentures/caracteristicas_e.asp?Ativo=
  agenda:          exploreosnd/consultaadados/eventosfinanceiros/agenda_e.asp?...&dt_ini=..&dt_fim=..
Como o lote traz todos os codigos de uma vez, nao ha cache por codigo (codigo novo no
universo entra no mesmo dia).

Universo: emissores (CNPJ) dos papeis de dados/ultimo.json. Entram TODAS as debentures
desses CNPJs registradas no SND (para o muro de vencimentos do emissor), com no_universo
marcando as que estao no painel.

Saidas (dados/snd/):
  caracteristicas.csv      um por codigo
  agenda.csv               eventos futuros (juros, amortizacao, vencimento, resgate...)
  vencimentos_emissor.csv  principal a vencer por CNPJ em 0-6, 6-12, 12-18 e 18-24 meses
  AAAA-MM-DD.csv           foto do dia de vencimentos_emissor
  ultimo.json              resumo + diagnostico
"""
from __future__ import annotations

import socket
_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse, csv, datetime as dt, io, json, os, pathlib, re, sys, time, traceback, unicodedata

import requests

UA = "originacao-radar/1.0 (+https://github.com/rodoso-mcamargo/originacao)"
BASE = "https://www.debentures.com.br/exploreosnd/consultaadados"
URL_CARAC = BASE + "/emissoesdedebentures/caracteristicas_e.asp?Ativo="
URL_AGENDA = (BASE + "/eventosfinanceiros/agenda_e.asp?emissor=&ativo=&evento="
              "&dt_ini={ini}&dt_fim={fim}&dt_pgto_ini=&dt_pgto_fim=")
TIMEOUT = 180
RAIZ = pathlib.Path(__file__).resolve().parent
DIR_OUT = RAIZ / "dados" / "snd"
UNIVERSO = RAIZ / "dados" / "ultimo.json"

COLS_CARAC = ["codigo", "isin", "emissor", "cnpj_emissor", "numero_emissao", "serie",
              "data_emissao", "vencimento", "volume_emissao", "quantidade", "valor_nominal",
              "indexador", "remuneracao", "garantia", "incentivada", "agente_fiduciario",
              "coordenador_lider", "situacao", "atualizado_em",
              # extras (da mesma fonte)
              "no_universo", "quantidade_mercado", "valor_nominal_atual", "data_vna",
              "saldo_atual", "percentual_indexador", "taxa_juros", "familia"]
COLS_AGENDA = ["codigo", "data_evento", "tipo", "percentual_ou_valor",
               "data_pagamento", "evento_fonte", "base_fonte", "liquidacao"]
COLS_VENC = ["cnpj_emissor", "emissor", "no_universo", "n_papeis", "saldo_atual",
             "venc_0_6m", "venc_6_12m", "venc_12_18m", "venc_18_24m", "venc_0_24m",
             "pct_saldo_0_24m", "saldo_estimado", "data_referencia"]
INDICES_PRECO = {"IPCA", "IGP-M", "IGP-DI", "INPC"}

sessao = requests.Session()
sessao.headers.update({"User-Agent": UA})
_ultimo_req = [0.0]


# ----------------------------------------------------------------- utilidades
def baixar(url: str) -> str:
    espera = 1.0 - (time.time() - _ultimo_req[0])  # <= 1 req/s
    if espera > 0:
        time.sleep(espera)
    r = sessao.get(url, timeout=TIMEOUT)
    _ultimo_req[0] = time.time()
    r.raise_for_status()
    return r.content.decode("cp1252", errors="replace")


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def data_iso(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    return f"{m[3]}-{m[2]}-{m[1]}" if m else None


def numero(s):
    """'1.000,50' / '1,6' / '000001000000' / '1000' -> float; '-', '' -> None."""
    s = (s or "").strip()
    if not s or s in {"-", "--"}:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def inteiro(s):
    v = numero(s)
    return int(v) if v is not None else None


def texto(s):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return None if s in {"", "-", "--"} else s


def tabela_tab(txt: str, inicio_cabecalho: str) -> tuple[list[str], list[list[str]], str | None]:
    """Acha o cabecalho e devolve (colunas unicas, linhas, 'gerado em' ISO)."""
    linhas = txt.splitlines()
    gerado = None
    m = re.search(r"Gerado em (\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}:\d{2})", txt)
    if m:
        gerado = f"{data_iso(m[1])}T{m[2]}"
    idx = next((i for i, l in enumerate(linhas) if l.strip().startswith(inicio_cabecalho)), None)
    if idx is None:
        raise ValueError(f"cabecalho '{inicio_cabecalho}' nao encontrado na resposta")
    cols, vistos = [], {}
    for c in linhas[idx].split("\t"):
        c = re.sub(r"\s+", " ", c).strip()
        n = vistos.get(c, 0)
        vistos[c] = n + 1
        cols.append(c if n == 0 else f"{c}.{n}")
    dados = [l.split("\t") for l in linhas[idx + 1:] if l.strip()]
    return cols, dados, gerado


# ----------------------------------------------------------------- parsing
def familia_de(indice, percentual, taxa):
    ind = (indice or "").upper()
    if ind == "DI":
        if percentual is not None and abs(percentual - 100) > 1e-9:
            return "DI_PCT"
        return "DI_SPREAD"
    if ind == "IPCA":
        return "IPCA"
    if ind in {"IGP-M"}:
        return "IGPM"
    if ind in {"PRÉ", "PRE", "PREFIXADO"}:
        return "PRE"
    return ind or None


def remuneracao_de(indice, percentual, taxa):
    """Monta o texto da remuneracao com os campos da fonte (nada calculado)."""
    if not indice:
        return None
    ind = indice.upper()
    tx = f"{taxa:g}".replace(".", ",") if taxa else None
    if ind == "DI":
        if percentual is not None and abs(percentual - 100) > 1e-9:
            s = f"{percentual:g}".replace(".", ",") + "% DI"
            return s + (f" + {tx}%" if tx else "")
        return f"DI + {tx}%" if tx else "100% DI"
    if ind in {"PRÉ", "PRE", "PREFIXADO"}:
        return f"{tx}% a.a." if tx else "PRÉ"
    return f"{indice} + {tx}%" if tx else indice


def parse_caracteristicas(txt: str):
    cols, linhas, gerado = tabela_tab(txt, "Codigo do Ativo")
    saida = []
    for v in linhas:
        r = dict(zip(cols, v + [""] * (len(cols) - len(v))))
        codigo = texto(r.get("Codigo do Ativo"))
        if not codigo:
            continue
        qtd = inteiro(r.get("Quantidade Emitida"))
        vne = numero(r.get("Valor Nominal na Emissao"))
        qtd_merc = inteiro(r.get("Quantidade em Mercado"))
        vna = numero(r.get("Valor Nominal Atual"))
        indice = texto(r.get("indice"))
        pct = numero(r.get("Percentual Multiplicador/Rentabilidade"))
        taxa = numero(r.get("Juros Criterio Novo - Taxa"))
        cnpj = re.sub(r"\D", "", r.get("CNPJ") or "") or None
        saida.append({
            "codigo": codigo,
            "isin": texto(r.get("ISIN")),
            "emissor": texto(r.get("Empresa")),
            "cnpj_emissor": cnpj.zfill(14) if cnpj else None,
            "numero_emissao": inteiro(r.get("Emissao")),
            "serie": texto(r.get("Serie")),
            "data_emissao": data_iso(r.get("Data de Emissao")),
            "vencimento": data_iso(r.get("Data de Saida / Novo Vencimento")) or data_iso(r.get("Data de Vencimento")),
            "volume_emissao": round(qtd * vne, 2) if qtd is not None and vne is not None else None,
            "quantidade": qtd,
            "valor_nominal": vne,
            "indexador": indice,
            "remuneracao": remuneracao_de(indice, pct, taxa),
            "garantia": texto(r.get("Garantia/Especie")),
            "incentivada": {"S": True, "N": False}.get((r.get("Deb. Incent. (Lei 12.431)") or "").strip()),
            "agente_fiduciario": texto(r.get("Agente Fiduciario")),
            "coordenador_lider": texto(r.get("Coordenador Lider")),
            "situacao": texto(r.get("Situacao")),
            "atualizado_em": gerado,
            "quantidade_mercado": qtd_merc,
            "valor_nominal_atual": vna,
            "data_vna": data_iso(r.get("Data Ult. VNA")),
            "saldo_atual": round(qtd_merc * vna, 2) if qtd_merc is not None and vna is not None else None,
            "percentual_indexador": pct,
            "taxa_juros": taxa,
            "familia": familia_de(indice, pct, taxa),
        })
    return saida, cols, gerado


def tipo_evento(ev: str) -> str:
    e = sem_acento(ev or "").lower().strip()
    if "juros" in e:
        return "juros"
    if e.startswith("amortizacao"):
        return "amortizacao"
    if e.startswith("vencimento"):
        return "vencimento"
    if e.startswith("resgate"):
        return "resgate"
    if e.startswith("premio"):
        return "premio"
    if e.startswith("participacao"):
        return "participacao"
    return "outro"


def base_fonte(tp: str):
    """A coluna Tipo vem duplicada ('VNA VAR/UNIFVNA VAR/UNIF'): fica a primeira metade."""
    tp = (tp or "").strip()
    n = len(tp)
    if n and n % 2 == 0 and tp[: n // 2] == tp[n // 2:]:
        tp = tp[: n // 2]
    return texto(tp)


def parse_agenda(txt: str):
    cols, linhas, gerado = tabela_tab(txt, "Data do Evento")
    saida = []
    for v in linhas:
        r = dict(zip(cols, v + [""] * (len(cols) - len(v))))
        codigo = texto(r.get("Ativo"))
        de = data_iso(r.get("Data do Evento"))
        if not codigo or not de:
            continue
        ev = texto(r.get("Evento"))
        saida.append({
            "codigo": codigo,
            "data_evento": de,
            "tipo": tipo_evento(ev),
            "percentual_ou_valor": numero(r.get("Taxa/Percentual")),
            "data_pagamento": data_iso(r.get("Data do Pagamento")),
            "evento_fonte": ev,
            "base_fonte": base_fonte(r.get("Tipo")),
            "liquidacao": texto(r.get("Liquidação") or r.get("Liquidacao")),
            "_emissor": texto(r.get("Emissor")),
        })
    return saida, cols, gerado


# ----------------------------------------------------------------- vencimentos
def somar_meses(d: dt.date, n: int) -> dt.date:
    a, m = divmod(d.month - 1 + n, 12)
    y, mm = d.year + a, m + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return dt.date(y, mm, dia)
        except ValueError:
            continue


def fluxo_principal(car: dict, eventos: list[dict]):
    """Principal pago em cada evento (R$), a partir do saldo atual.

    Regras (base_fonte do SND):
      VNA VAR ...  -> % sobre o saldo remanescente
      VNA FIX / VNE ... -> % sobre o valor nominal de emissao (x quantidade em mercado)
      vencimento / resgate total -> todo o saldo remanescente
    Devolve (lista de (data, valor)), estimado(bool).
    """
    estimado = False
    saldo = car.get("saldo_atual")
    if car.get("quantidade_mercado") == 0:
        return [], False  # tudo resgatado/em tesouraria segundo a fonte: saldo zero de fato
    if not saldo:  # sem quantidade em mercado ou sem VNA atual na fonte
        saldo = car.get("volume_emissao")
        estimado = True
    if not saldo:
        return [], True
    qtd = car.get("quantidade_mercado") or car.get("quantidade")
    vne = car.get("valor_nominal")
    preco = (car.get("indexador") or "").upper() in INDICES_PRECO
    rem = float(saldo)
    fluxo = []
    for e in sorted(eventos, key=lambda x: x["data_evento"]):
        if rem <= 0.005:
            break
        tp, p = e["tipo"], e.get("percentual_ou_valor")
        data = e.get("data_pagamento") or e["data_evento"]
        ev = sem_acento(e.get("evento_fonte") or "").lower()
        base = (e.get("base_fonte") or "").upper()
        if tp == "vencimento" or (tp == "resgate" and "total" in ev):
            valor = rem
        elif tp == "amortizacao" and p:
            if base.startswith("VNA VAR") or not base or base.startswith("-"):
                valor = rem * p / 100
            elif qtd and vne:
                valor = min(rem, qtd * vne * p / 100)
                if preco:
                    estimado = True  # % sobre VNE sem a correcao monetaria acumulada
            else:
                valor = rem * p / 100
                estimado = True
        else:
            continue
        rem -= valor
        fluxo.append((data, valor))
    return fluxo, estimado


def vencimentos_por_emissor(caracs: list[dict], agenda: list[dict], hoje: dt.date):
    lim = [somar_meses(hoje, m).isoformat() for m in (6, 12, 18, 24)]
    por_cod = {}
    for e in agenda:
        por_cod.setdefault(e["codigo"], []).append(e)
    out = {}
    for c in caracs:
        if (c.get("situacao") or "").lower() != "registrado" or not c.get("cnpj_emissor"):
            continue
        k = c["cnpj_emissor"]
        o = out.setdefault(k, {"cnpj_emissor": k, "emissor": c["emissor"], "no_universo": False,
                               "n_papeis": 0, "saldo_atual": 0.0, "venc_0_6m": 0.0,
                               "venc_6_12m": 0.0, "venc_12_18m": 0.0, "venc_18_24m": 0.0,
                               "saldo_estimado": False, "data_referencia": hoje.isoformat()})
        o["n_papeis"] += 1
        o["no_universo"] = o["no_universo"] or bool(c.get("no_universo"))
        fluxo, est = fluxo_principal(c, por_cod.get(c["codigo"], []))
        if c.get("quantidade_mercado") == 0:
            pass
        elif c.get("saldo_atual"):
            o["saldo_atual"] += c["saldo_atual"]
        else:
            o["saldo_atual"] += c.get("volume_emissao") or 0
        o["saldo_estimado"] = o["saldo_estimado"] or est
        for data, v in fluxo:
            if data < hoje.isoformat():
                continue
            if data <= lim[0]:
                o["venc_0_6m"] += v
            elif data <= lim[1]:
                o["venc_6_12m"] += v
            elif data <= lim[2]:
                o["venc_12_18m"] += v
            elif data <= lim[3]:
                o["venc_18_24m"] += v
    linhas = []
    for o in out.values():
        for k in ("saldo_atual", "venc_0_6m", "venc_6_12m", "venc_12_18m", "venc_18_24m"):
            o[k] = round(o[k], 2)
        o["venc_0_24m"] = round(o["venc_0_6m"] + o["venc_6_12m"] + o["venc_12_18m"] + o["venc_18_24m"], 2)
        o["pct_saldo_0_24m"] = round(100 * o["venc_0_24m"] / o["saldo_atual"], 1) if o["saldo_atual"] else None
        linhas.append(o)
    linhas.sort(key=lambda o: -o["venc_0_24m"])
    return linhas


# ----------------------------------------------------------------- execucao
def universo_codigos() -> set[str]:
    try:
        s = json.loads(UNIVERSO.read_text(encoding="utf-8"))
        return {p["codigo"].strip().upper() for p in s.get("papeis", []) if p.get("codigo")}
    except FileNotFoundError:
        return set()


def gravar_csv(caminho: pathlib.Path, linhas: list[dict], cols: list[str]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for l in linhas:
            w.writerow({k: ("" if l.get(k) is None else l.get(k)) for k in cols})


def url_agenda(hoje: dt.date) -> str:
    return URL_AGENDA.format(ini=hoje.strftime("%d/%m/%Y"), fim="31/12/2070")


def inspecionar(hoje: dt.date):
    print("## SND (debentures.com.br) - inspecao\n")
    for nome, url, cab in (("Caracteristicas (lote)", URL_CARAC, "Codigo do Ativo"),
                           ("Agenda de eventos (lote)", url_agenda(hoje), "Data do Evento")):
        try:
            txt = baixar(url)
            cols, linhas, gerado = tabela_tab(txt, cab)
            print(f"### {nome}\n\n- URL: `{url}`\n- status: 200, {len(txt):,} caracteres, "
                  f"gerado em {gerado}, {len(linhas):,} linhas\n- colunas ({len(cols)}): `{cols}`\n")
            for l in linhas[:3]:
                print("    " + " | ".join(x.strip() for x in l[:20]))
            print()
        except Exception as e:  # inspecionar nunca quebra
            print(f"### {nome}\n\n- ERRO: `{type(e).__name__}: {e}`\n")


def rodar(hoje: dt.date):
    diag = {"lidos": {}, "gravados": {}, "descartados": {}, "erros": {}}
    txt_c = baixar(URL_CARAC)
    caracs_todas, cols_c, gerado_c = parse_caracteristicas(txt_c)
    diag["lidos"]["caracteristicas"] = len(caracs_todas)
    txt_a = baixar(url_agenda(hoje))
    agenda_toda, cols_a, gerado_a = parse_agenda(txt_a)
    diag["lidos"]["agenda"] = len(agenda_toda)

    univ = universo_codigos()
    cnpj_univ = {c["cnpj_emissor"] for c in caracs_todas if c["codigo"] in univ and c["cnpj_emissor"]}
    sem_snd = sorted(univ - {c["codigo"] for c in caracs_todas})
    caracs = []
    for c in caracs_todas:
        c["no_universo"] = c["codigo"] in univ
        # papeis ja excluidos do SND so entram se ainda estiverem no painel
        if c["cnpj_emissor"] in cnpj_univ and (
                (c.get("situacao") or "").lower() == "registrado" or c["no_universo"]):
            caracs.append(c)
    caracs.sort(key=lambda c: c["codigo"])
    cods = {c["codigo"] for c in caracs}
    agenda = [e for e in agenda_toda if e["codigo"] in cods]
    agenda.sort(key=lambda e: (e["codigo"], e["data_evento"], e["tipo"]))
    diag["descartados"] = {
        "caracteristicas_fora_do_universo": len(caracs_todas) - len(caracs),
        "agenda_fora_do_universo": len(agenda_toda) - len(agenda),
        "motivo": "CNPJ do emissor sem papel em dados/ultimo.json, ou papel excluido do SND fora do painel",
        "codigos_do_universo_sem_snd": sem_snd,
    }
    venc = vencimentos_por_emissor(caracs, agenda, hoje)

    DIR_OUT.mkdir(parents=True, exist_ok=True)
    gravar_csv(DIR_OUT / "caracteristicas.csv", caracs, COLS_CARAC)
    gravar_csv(DIR_OUT / "agenda.csv", agenda, COLS_AGENDA)
    gravar_csv(DIR_OUT / "vencimentos_emissor.csv", venc, COLS_VENC)
    gravar_csv(DIR_OUT / f"{hoje.isoformat()}.csv", venc, COLS_VENC)
    diag["gravados"] = {"caracteristicas": len(caracs), "agenda": len(agenda),
                        "vencimentos_emissor": len(venc)}
    resumo = {
        "fonte": "SND - debentures.com.br (ANBIMA/B3)",
        "urls": {"caracteristicas": URL_CARAC, "agenda": url_agenda(hoje)},
        "data_referencia": hoje.isoformat(),
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fonte_gerado_em": {"caracteristicas": gerado_c, "agenda": gerado_a},
        "universo_codigos": len(univ),
        "universo_cnpjs": len(cnpj_univ),
        "emissores_com_vencimento_24m": sum(1 for v in venc if v["venc_0_24m"] > 0),
        "top10_vencimento_24m": [{k: v[k] for k in ("cnpj_emissor", "emissor", "venc_0_24m",
                                                     "pct_saldo_0_24m", "saldo_estimado")}
                                 for v in venc[:10]],
        "diagnostico": diag,
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8")
    return resumo


def resumo_md(s: dict) -> str:
    d = s["diagnostico"]
    out = [f"## SND {s['data_referencia']}\n",
           f"- Lidos: caracteristicas {d['lidos']['caracteristicas']:,}, agenda {d['lidos']['agenda']:,}",
           f"- Gravados: `{d['gravados']}`",
           f"- Universo: {s['universo_codigos']} codigos, {s['universo_cnpjs']} CNPJs; "
           f"sem SND: {d['descartados']['codigos_do_universo_sem_snd']}",
           f"- Emissores com principal a vencer em 24m: **{s['emissores_com_vencimento_24m']}**\n",
           "| Emissor | 0-24m (R$ mi) | % do saldo | estimado |", "|---|---:|---:|---|"]
    for t in s["top10_vencimento_24m"]:
        out.append(f"| {t['emissor']} | {t['venc_0_24m'] / 1e6:,.1f} | {t['pct_saldo_0_24m']} | {t['saldo_estimado']} |")
    return "\n".join(out) + "\n"


def no_summary(txt: str):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(txt + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inspecionar", action="store_true", help="so descreve a fonte, nao grava")
    ap.add_argument("--data", help="data de referencia AAAA-MM-DD (padrao: hoje)")
    a = ap.parse_args(argv)
    hoje = dt.date.fromisoformat(a.data) if a.data else dt.date.today()
    try:
        if a.inspecionar:
            inspecionar(hoje)
            return 0
        s = rodar(hoje)
        md = resumo_md(s)
        print(md)
        no_summary(md)
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        no_summary("## SND: erro\n\n```\n" + tb + "\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
