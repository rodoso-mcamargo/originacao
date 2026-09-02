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
import os
import pathlib
import socket
import sys
import tempfile
import traceback
import zipfile
from collections import defaultdict

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("cda")

# Alguns CSVs da CVM trazem campos muito longos; o limite padrao do modulo csv
# (128 KB) estoura com "_csv.Error: field larger than field limit".
csv.field_size_limit(1 << 30)

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
DIR_CDA = DADOS / "cda"

HOST = "dados.cvm.gov.br"
URL = f"https://{HOST}/dados/FI/DOC/CDA/DADOS/cda_fi_{{aaaamm}}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0"}
TIMEOUT = 300

# O host da CVM publica A e AAAA; o runner do GitHub nao tem rota IPv6 e a
# tentativa morre com ENETUNREACH. Forcar IPv4 elimina essa metade do problema.
# (A ANBIMA so tem IPv4, por isso o coletor diario nunca esbarrou nisso.)
try:
    import urllib3.util.connection as _u3conn
    _u3conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass


def diagnostico_rede() -> str:
    """Prova de conectividade com o host da CVM, em markdown para o resumo do job."""
    L = ["## Diagnostico de rede\n"]

    familias = {}
    for fam, rot in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            infos = socket.getaddrinfo(HOST, 443, fam, socket.SOCK_STREAM)
            familias[rot] = sorted({i[4][0] for i in infos})
        except Exception as e:
            familias[rot] = f"sem registro ({type(e).__name__})"
    L.append(f"- DNS de `{HOST}`: {familias}\n")

    for rot, enderecos in familias.items():
        if not isinstance(enderecos, list):
            continue
        for ip in enderecos[:2]:
            fam = socket.AF_INET if rot == "IPv4" else socket.AF_INET6
            s = None
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.settimeout(15)
                s.connect((ip, 443))
                L.append(f"- TCP 443 em `{ip}` ({rot}): **conectou**\n")
            except Exception as e:
                L.append(f"- TCP 443 em `{ip}` ({rot}): **falhou** — "
                         f"{type(e).__name__}: {e}\n")
            finally:
                if s is not None:
                    s.close()

    # de onde este runner sai para a internet
    for servico in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = requests.get(servico, timeout=15)
            L.append(f"- IP publico do runner (`{servico}`): `{r.text.strip()[:60]}`\n")
            break
        except Exception as e:
            L.append(f"- {servico}: falhou ({type(e).__name__})\n")

    # um GET de verdade, curto
    try:
        r = requests.get(f"https://{HOST}/", headers=HEADERS, timeout=30)
        L.append(f"- GET `https://{HOST}/`: **HTTP {r.status_code}**, "
                 f"{len(r.content)} bytes\n")
    except Exception as e:
        L.append(f"- GET `https://{HOST}/`: **falhou** — {type(e).__name__}: {e}\n")

    L.append(
        "\n_Se o IPv4 tambem nao conecta, o host esta recusando a rede do GitHub "
        "(bloqueio de IP estrangeiro e comum em site do governo brasileiro) e a "
        "coleta precisa sair de outro lugar._\n")
    return "".join(L)

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


def baixar_zip(aaaamm: str) -> pathlib.Path | None:
    """Baixa em streaming para arquivo temporario: o zip da CDA passa de 100 MB
    e segurar tudo em memoria e desnecessario."""
    u = URL.format(aaaamm=aaaamm)
    try:
        resposta = requests.get(u, headers=HEADERS, timeout=TIMEOUT, stream=True)
    except requests.exceptions.ConnectionError as e:
        # Falha de rede nao diz nada sozinha: anexa a prova de conectividade.
        raise RuntimeError(
            f"nao foi possivel alcancar {HOST}.\n\n{diagnostico_rede()}\n"
            f"Erro original: {type(e).__name__}: {e}") from e

    with resposta as r:
        if r.status_code == 404:
            log.info("%s ainda nao publicado", aaaamm)
            return None
        r.raise_for_status()
        destino = pathlib.Path(tempfile.gettempdir()) / f"cda_{aaaamm}.zip"
        n = 0
        with destino.open("wb") as fh:
            for pedaco in r.iter_content(chunk_size=1 << 20):
                fh.write(pedaco)
                n += len(pedaco)
    log.info("%s: %.1f MB baixados -> %s", aaaamm, n / 1e6, destino)
    if not zipfile.is_zipfile(destino):
        cabeca = destino.read_bytes()[:200]
        raise RuntimeError(
            f"{u} nao devolveu um zip valido. Primeiros bytes: {cabeca!r}")
    return destino


def membros_csv(caminho: pathlib.Path):
    """Percorre os CSVs do zip, entrando em zips aninhados se houver."""
    with zipfile.ZipFile(caminho) as z:
        nomes = sorted(z.namelist())
        log.info("zip contem %d membros: %s", len(nomes),
                 ", ".join(nomes[:12]) + (" ..." if len(nomes) > 12 else ""))
        for nome in nomes:
            baixo = nome.lower()
            if baixo.endswith(".csv"):
                yield nome, z.open(nome)
            elif baixo.endswith(".zip"):
                log.info("zip aninhado: %s", nome)
                interno = io.BytesIO(z.read(nome))
                with zipfile.ZipFile(interno) as z2:
                    for n2 in sorted(z2.namelist()):
                        if n2.lower().endswith(".csv"):
                            yield f"{nome}!{n2}", z2.open(n2)


def universo() -> dict[str, dict]:
    f = DADOS / "ultimo.json"
    if not f.exists():
        raise SystemExit(
            "dados/ultimo.json nao encontrado. Rode o monitor.py antes: a CDA e "
            "filtrada pelos papeis do radar."
        )
    s = json.loads(f.read_text(encoding="utf-8"))
    return {p["codigo"].strip().upper(): p for p in s["papeis"]}


def inspecionar(caminho: pathlib.Path) -> None:
    """Descreve o zip sem processar: quais arquivos, quais colunas, que tipos de ativo."""
    print("## Estrutura do zip da CDA\n")
    for nome, fh in membros_csv(caminho):
        print(f"### `{nome}`\n")
        try:
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
            i_tipo = cab.index(mapa["tipo"]) if "tipo" in mapa else None
            i_cod = cab.index(mapa["codigo"]) if "codigo" in mapa else None
            tipos, exemplos = defaultdict(int), []
            for n, linha in enumerate(leitor):
                if n > 200000:
                    break
                if i_tipo is not None and len(linha) > i_tipo:
                    t = linha[i_tipo].strip()
                    tipos[t] += 1
                    if i_cod is not None and "deb" in t.lower() and len(exemplos) < 8 \
                       and len(linha) > i_cod:
                        exemplos.append(linha[i_cod].strip())
            if tipos:
                top = sorted(tipos.items(), key=lambda x: -x[1])[:12]
                print("Tipos de ativo: " + ", ".join(f"`{t}` ({n})" for t, n in top) + "\n")
            if exemplos:
                print("Exemplos de codigo em linhas de debenture: `"
                      + "`, `".join(exemplos) + "`\n")
        except Exception as e:
            print(f"_falha lendo: {type(e).__name__}: {e}_\n")
        finally:
            try:
                fh.close()
            except Exception:
                pass


def processar_mes(caminho: pathlib.Path, aaaamm: str, papeis: dict[str, dict]) -> dict:
    linhas = []
    diag = {"arquivos_lidos": [], "arquivos_ignorados": [], "arquivos_com_erro": [],
            "linhas_varridas": 0, "linhas_com_codigo": 0, "casadas": 0,
            "amostra_codigos": []}
    amostra = set()

    for nome, fh in membros_csv(caminho):
        try:
            txt = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            leitor = csv.reader(txt, delimiter=";")
            try:
                cab = next(leitor)
            except StopIteration:
                continue
            m = resolver(cab)
            # so interessa arquivo que tenha codigo de ativo e nome de fundo
            if "codigo" not in m or "fundo" not in m:
                diag["arquivos_ignorados"].append(
                    {"arquivo": nome, "colunas": [c.strip() for c in cab][:20]})
                continue
            idx = {k: cab.index(v) for k, v in m.items()}
            diag["arquivos_lidos"].append({"arquivo": nome, "mapa": m})
            log.info("lendo %s (colunas resolvidas: %s)", nome, m)

            def pega(linha, chave):
                i = idx.get(chave)
                return linha[i].strip() if i is not None and len(linha) > i else ""

            for linha in leitor:
                diag["linhas_varridas"] += 1
                cod = pega(linha, "codigo").upper()
                if not cod:
                    continue
                diag["linhas_com_codigo"] += 1
                # guarda uma amostra de codigos p/ diagnosticar join que nao casa
                if len(amostra) < 40 and "deb" in pega(linha, "tipo").lower():
                    amostra.add(cod)
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
        except Exception as e:
            # um arquivo problematico nao pode derrubar o mes inteiro
            log.error("falha lendo %s: %s: %s", nome, type(e).__name__, e)
            diag["arquivos_com_erro"].append(
                {"arquivo": nome, "erro": f"{type(e).__name__}: {e}"})
        finally:
            try:
                fh.close()
            except Exception:
                pass

    diag["amostra_codigos"] = sorted(amostra)

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
    ap.add_argument("--rede", action="store_true",
                    help="so testa a conectividade com a CVM e sai")
    a = ap.parse_args(argv)

    if a.rede:
        print(diagnostico_rede())
        return 0

    if a.inspecionar:
        for mm in ([a.mes.replace("-", "")] if a.mes
                   else meses_disponiveis(dt.date.today(), 8)):
            caminho = baixar_zip(mm)
            if caminho:
                print(f"\n**Mes mais recente disponivel: {mm}**\n")
                inspecionar(caminho)
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
        caminho = baixar_zip(mm)
        if caminho is None:
            continue
        ok = processar_mes(caminho, mm, papeis)
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
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # Nunca morrer calado: o log do Actions exige login, entao o traceback
        # tem que estar tambem no resumo do job, que da para copiar facil.
        tb = traceback.format_exc()
        log.error("FALHA NAO TRATADA\n%s", tb)
        resumo = os.environ.get("GITHUB_STEP_SUMMARY")
        if resumo:
            with open(resumo, "a", encoding="utf-8") as fh:
                fh.write("## Falha na coleta da CDA\n\n```\n" + tb + "\n```\n")
        sys.exit(1)
