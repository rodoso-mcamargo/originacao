#!/usr/bin/env python3
"""
Coletor CDA/CVM - quem comprou e quem vendeu cada debenture do radar.

A CDA (Composicao e Diversificacao de Aplicacoes) e entregue mensalmente pelos
fundos. Cada linha traz, alem da posicao final, o FLUXO DO MES: valor comprado e
valor vendido. Nao e preciso inferir venda por diferenca de posicao.

    python cda.py                  # mes mais recente disponivel
    python cda.py --mes 2026-06
    python cda.py --backfill 6     # ultimos 6 meses
    python cda.py --inspecionar    # so descreve a estrutura do zip, nao processa

Saidas:
    dados/cda/AAAA-MM.csv    linhas filtradas pelos papeis do radar
    dados/cda/ultimo.json    agregado por papel: detentores, compradores, vendedores

O universo de papeis vem de dados/ultimo.json (o radar diario). Sem ele, o script
para: filtrar a CDA inteira sem recorte geraria centenas de MB.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import pathlib
import sys
import zipfile
from collections import defaultdict

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("cda")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
DIR_CDA = DADOS / "cda"

URL = "https://dados.cvm.gov.br/dados/FI/DOC/CDA/DADOS/cda_fi_{aaaamm}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0"}
TIMEOUT = 300

# A CVM ja renomeou colunas (RCVM 175 trouxe "classe"), entao cada campo aceita
# varios nomes. A resolucao e feita contra o cabecalho real do arquivo.
ALIASES = {
    "codigo":   ["CD_ATIVO", "CD_ATIVO_NEGOC", "CODIGO_ATIVO"],
    "tipo":     ["TP_ATIVO", "TIPO_ATIVO"],
    "cnpj":     ["CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO", "CNPJ_FUNDO_COTA", "CD_CNPJ_FUNDO"],
    "fundo":    ["DENOM_SOCIAL", "NM_FUNDO", "DENOM_SOCIAL_CLASSE"],
    "data":     ["DT_COMPTC", "DT_COMPETENCIA"],
    "emissor":  ["EMISSOR", "NM_EMISSOR", "CNPJ_EMISSOR"],
    "qt_pos":   ["QT_POS_FINAL", "QTDE_POS_FIM"],
    "vl_pos":   ["VL_MERC_POS_FINAL", "MERC_POS_FIM", "VL_POS_FINAL"],
    "vl_compra": ["VL_AQUIS_NEGOC", "VL_AQUIS", "VL_AQUISICAO_NEGOC"],
    "vl_venda":  ["VL_VENDA_NEGOC", "VL_VENDAS_NEGOC", "VL_VENDA"],
    "qt_compra": ["QT_AQUIS_NEGOC", "QTDE_AQUIS_NEGOC"],
    "qt_venda":  ["QT_VENDA_NEGOC", "QTDE_VENDAS_NEGOC"],
}

TOPO = 12  # quantos fundos guardar em cada lista por papel


def resolver(cabecalho: list[str]) -> dict[str, str]:
    """Mapeia nome logico -> nome real da coluna neste arquivo."""
    presentes = {c.strip().upper(): c for c in cabecalho}
    fora = {}
    for logico, candidatos in ALIASES.items():
        for c in candidatos:
            if c in presentes:
                fora[logico] = presentes[c]
                break
    return fora


def num(v) -> float | None:
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "-", "NA", "N/A", "0,00"):
        return 0.0 if v == "0,00" else None
    try:
        return float(v.replace(".", "").replace(",", ".")) if "," in v else float(v)
    except ValueError:
        return None


def meses_disponiveis(ate: dt.date, quantos: int) -> list[str]:
    out, a, m = [], ate.year, ate.month
    for _ in range(quantos):
        out.append(f"{a}{m:02d}")
        m -= 1
        if m == 0:
            a, m = a - 1, 12
    return out


def baixar_zip(aaaamm: str) -> bytes | None:
    u = URL.format(aaaamm=aaaamm)
    r = requests.get(u, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        log.info("%s ainda nao publicado", aaaamm)
        return None
    r.raise_for_status()
    log.info("%s: %.1f MB baixados", aaaamm, len(r.content) / 1e6)
    return r.content


def universo() -> dict[str, dict]:
    f = DADOS / "ultimo.json"
    if not f.exists():
        raise SystemExit(
            "dados/ultimo.json nao encontrado. Rode o monitor.py antes: a CDA e "
            "filtrada pelos papeis do radar."
        )
    s = json.loads(f.read_text(encoding="utf-8"))
    return {p["codigo"].strip().upper(): p for p in s["papeis"]}


def inspecionar(conteudo: bytes) -> None:
    """Descreve o zip sem processar: quais arquivos, quais colunas, que tipos de ativo."""
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        print("## Estrutura do zip da CDA\n")
        for nome in sorted(z.namelist()):
            info = z.getinfo(nome)
            print(f"### `{nome}` — {info.file_size/1e6:.1f} MB\n")
            with z.open(nome) as fh:
                txt = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                leitor = csv.reader(txt, delimiter=";")
                try:
                    cab = next(leitor)
                except StopIteration:
                    print("_vazio_\n")
                    continue
                print("Colunas: `" + "`, `".join(c.strip() for c in cab) + "`\n")
                mapa = resolver(cab)
                print(f"Resolvidas: `{mapa}`\n")
                if "tipo" in mapa:
                    i = cab.index(mapa["tipo"])
                    tipos = defaultdict(int)
                    for n, linha in enumerate(leitor):
                        if n > 200000:
                            break
                        if len(linha) > i:
                            tipos[linha[i].strip()] += 1
                    top = sorted(tipos.items(), key=lambda x: -x[1])[:12]
                    print("Tipos de ativo (amostra): "
                          + ", ".join(f"`{t}` ({n})" for t, n in top) + "\n")


def processar_mes(conteudo: bytes, aaaamm: str, papeis: dict[str, dict]) -> dict:
    linhas, diag = [], {"arquivos_lidos": [], "linhas_varridas": 0,
                        "linhas_com_codigo": 0, "casadas": 0}

    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        for nome in sorted(z.namelist()):
            if not nome.lower().endswith(".csv"):
                continue
            with z.open(nome) as fh:
                txt = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                leitor = csv.reader(txt, delimiter=";")
                try:
                    cab = next(leitor)
                except StopIteration:
                    continue
                m = resolver(cab)
                # so interessa arquivo que tenha codigo de ativo e fluxo
                if "codigo" not in m or "fundo" not in m:
                    continue
                idx = {k: cab.index(v) for k, v in m.items()}
                diag["arquivos_lidos"].append(nome)

                def pega(linha, chave):
                    i = idx.get(chave)
                    return linha[i].strip() if i is not None and len(linha) > i else ""

                for linha in leitor:
                    diag["linhas_varridas"] += 1
                    cod = pega(linha, "codigo").upper()
                    if not cod:
                        continue
                    diag["linhas_com_codigo"] += 1
                    if cod not in papeis:
                        continue
                    diag["casadas"] += 1
                    linhas.append({
                        "codigo": cod,
                        "cnpj": pega(linha, "cnpj"),
                        "fundo": pega(linha, "fundo"),
                        "tipo": pega(linha, "tipo"),
                        "vl_pos": num(pega(linha, "vl_pos")) or 0.0,
                        "vl_compra": num(pega(linha, "vl_compra")) or 0.0,
                        "vl_venda": num(pega(linha, "vl_venda")) or 0.0,
                    })

    log.info("%s: %d linhas varridas, %d com codigo, %d casadas com o radar",
             aaaamm, diag["linhas_varridas"], diag["linhas_com_codigo"], diag["casadas"])

    # CSV filtrado do mes
    DIR_CDA.mkdir(parents=True, exist_ok=True)
    ref = f"{aaaamm[:4]}-{aaaamm[4:]}"
    with (DIR_CDA / f"{ref}.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["codigo", "cnpj", "fundo", "tipo",
                                           "vl_pos", "vl_compra", "vl_venda"])
        w.writeheader()
        w.writerows(linhas)

    # agregado por papel
    por_papel = defaultdict(list)
    for r in linhas:
        por_papel[r["codigo"]].append(r)

    agregado = {}
    for cod, rs in por_papel.items():
        det = sorted([r for r in rs if r["vl_pos"] > 0], key=lambda r: -r["vl_pos"])
        comp = sorted([r for r in rs if r["vl_compra"] > 0], key=lambda r: -r["vl_compra"])
        vend = sorted([r for r in rs if r["vl_venda"] > 0], key=lambda r: -r["vl_venda"])
        total = sum(r["vl_pos"] for r in det)
        agregado[cod] = {
            "n_fundos": len(det),
            "vl_total": round(total, 2),
            "top5_pct": round(100 * sum(r["vl_pos"] for r in det[:5]) / total, 1) if total else None,
            "vl_comprado": round(sum(r["vl_compra"] for r in comp), 2),
            "vl_vendido": round(sum(r["vl_venda"] for r in vend), 2),
            "detentores": [{"fundo": r["fundo"], "vl": round(r["vl_pos"], 2)} for r in det[:TOPO]],
            "compradores": [{"fundo": r["fundo"], "vl": round(r["vl_compra"], 2)} for r in comp[:TOPO]],
            "vendedores": [{"fundo": r["fundo"], "vl": round(r["vl_venda"], 2)} for r in vend[:TOPO]],
        }

    saida = {
        "mes_referencia": ref,
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "diagnostico": diag,
        "papeis_no_radar": len(papeis),
        "papeis_com_dado": len(agregado),
        "por_papel": agregado,
    }
    (DIR_CDA / "ultimo.json").write_text(
        json.dumps(saida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return saida


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mes", help="AAAA-MM")
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--inspecionar", action="store_true",
                    help="so descreve a estrutura do zip mais recente")
    ap.add_argument("--forcar", action="store_true",
                    help="reprocessa o mes mais recente mesmo se ja estiver salvo")
    a = ap.parse_args(argv)

    if a.inspecionar:
        for mm in meses_disponiveis(dt.date.today(), 8):
            c = baixar_zip(mm)
            if c:
                print(f"\n**Mes mais recente disponivel: {mm}**\n")
                inspecionar(c)
                return 0
        log.error("nenhum mes encontrado nos ultimos 8")
        return 1

    papeis = universo()
    log.info("universo do radar: %d papeis", len(papeis))

    if a.mes:
        alvos = [a.mes.replace("-", "")]
    else:
        alvos = meses_disponiveis(dt.date.today(), 8)

    # O zip da CDA tem centenas de MB. Se o mes mais recente ja foi processado,
    # nem baixa: a rotina pode rodar semanalmente sem desperdicio, so fazendo
    # trabalho de verdade quando a CVM publica um mes novo.
    ja = None
    f_ult = DIR_CDA / "ultimo.json"
    if f_ult.exists() and not a.mes and not a.backfill and not a.forcar:
        ja = json.loads(f_ult.read_text(encoding="utf-8")).get("mes_referencia")

    processados, ok = 0, None
    for mm in alvos:
        ref = f"{mm[:4]}-{mm[4:]}"
        if ja and ref == ja:
            log.info("%s ja processado (use --forcar para refazer); nada a fazer", ref)
            return 0
        if ja and ref < ja:
            log.info("nenhum mes novo depois de %s", ja)
            return 0
        conteudo = baixar_zip(mm)
        if conteudo is None:
            continue
        ok = processar_mes(conteudo, mm, papeis)
        processados += 1
        if processados >= max(1, a.backfill):
            break

    if not ok:
        log.error("nenhum mes da CDA pode ser processado")
        return 1
    log.info("mes %s: %d de %d papeis do radar com dado de carteira",
             ok["mes_referencia"], ok["papeis_com_dado"], ok["papeis_no_radar"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
