#!/usr/bin/env python3
"""
Monitor de pedidos de Recuperação Judicial, Falência e Cautelar Pré-RJ.

Duas fontes, sem lista fixa de empresas:

1. DataJud (CNJ) — API pública oficial, cobre os 27 tribunais estaduais.
   Encontra QUALQUER processo novo nas classes de Recuperação Judicial (129),
   Falência (108) ou Tutela Cautelar Antecedente (12134) com assunto
   "Recuperação judicial e Falência" (4993). LIMITAÇÃO IMPORTANTE: a API
   pública não devolve nome das partes (ver decisões-metodologia no projeto),
   só número do processo, tribunal, vara e data. Serve para saber QUANTO e
   ONDE, não QUEM — a menos que o processo depois apareça na imprensa.

2. Notícias (Google News RSS + busca) — dá o nome da empresa quando o caso
   vira notícia. Cobre só os casos relevantes o suficiente para sair na
   imprensa, mas é a fonte que efetivamente nomeia a empresa.

Roda diariamente via GitHub Actions (o container do Claude não alcança nem
api-publica.datajud.cnj.jus.br nem news.google.com diretamente — testado e
bloqueado, igual ANBIMA/B3/BCB no monitor de debêntures).

Saídas em dados/rj/:
  diario/AAAA-MM-DD.json   achados brutos da execução do dia
  vistos.json              registro do que já foi visto (dedupe + "é novo")
  ultimo.json              janela agregada dos últimos 30 dias, para o painel
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

DIR_DADOS = "dados/rj"
DIR_DIARIO = f"{DIR_DADOS}/diario"
ARQ_VISTOS = f"{DIR_DADOS}/vistos.json"
ARQ_ULTIMO = f"{DIR_DADOS}/ultimo.json"

JANELA_DASHBOARD_DIAS = 30  # quanto tempo um achado fica visível no painel
JANELA_VISTOS_DIAS = 120     # por quanto tempo guardamos o registro de dedupe
JANELA_DATAJUD_DIAS = 5      # margem de segurança para atraso de indexação

DATAJUD_URL = "https://api-publica.datajud.cnj.jus.br/{alias}/_search"
DATAJUD_KEY = "APIKey cDZHYzlZa0JadVREZDJcendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="
# Chave pública documentada pelo CNJ em https://datajud-wiki.cnj.jus.br/api-publica/acesso/
# É a mesma para todo mundo (não é segredo). Se parar de funcionar (CNJ já
# avisou que pode trocar), pegar a atual naquela página e atualizar aqui.

TRIBUNAIS = [
    "tjac", "tjal", "tjam", "tjap", "tjba", "tjce", "tjdft", "tjes", "tjgo",
    "tjma", "tjmg", "tjms", "tjmt", "tjpa", "tjpb", "tjpe", "tjpi", "tjpr",
    "tjrj", "tjrn", "tjro", "tjrr", "tjrs", "tjsc", "tjse", "tjsp", "tjto",
]

CLASSE_RJ = 129           # "Recuperação Judicial"
CLASSE_FALENCIA = 108     # "Falência de Empresários, Sociedades Empresariais..."
CLASSE_CAUTELAR = 12134   # "Tutela Cautelar Antecedente"
ASSUNTO_RJ_FALENCIA = 4993 # "Recuperação judicial e Falência"

USER_AGENT = "monitor-rj-originacao/1.0 (uso interno, contato via GitHub)"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_json(url: str, body: dict | None, headers: dict, tentativas: int = 3) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", "replace")[:300]
            ultimo_erro = f"HTTP {e.code}: {corpo}"
            if e.code in (400, 401, 403, 404):
                break  # erro de requisição, não adianta repetir
        except Exception as e:  # noqa: BLE001
            ultimo_erro = str(e)
        if tentativa < tentativas:
            time.sleep(2 * tentativa)
    log(f"  falhou ({url}): {ultimo_erro}")
    return None


# ---------------------------------------------------------------------------
# Fonte 1: DataJud
# ---------------------------------------------------------------------------

def consultar_datajud(alias: str, filtros: list[dict]) -> list[dict]:
    """Roda uma query contra um tribunal e devolve os hits (_source)."""
    corpo = {
        "size": 200,
        "sort": [{"dataAjuizamento": {"order": "desc"}}],
        "query": {"bool": {"filter": filtros}},
        "_source": [
            "numeroProcesso", "classe", "assuntos", "tribunal", "grau",
            "orgaoJulgador", "dataAjuizamento", "dataHoraUltimaAtualizacao",
        ],
    }
    headers = {
        "Authorization": DATAJUD_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    resposta = http_json(DATAJUD_URL.format(alias=alias), corpo, headers)
    if not resposta:
        return []
    hits = resposta.get("hits", {}).get("hits", [])
    return [h.get("_source", {}) for h in hits]


def buscar_datajud(desde_iso: str) -> list[dict]:
    """Varre os 27 tribunais estaduais atrás de RJ, falência e cautelar pré-RJ."""
    achados = []
    filtro_data = {"range": {"dataAjuizamento": {"gte": desde_iso}}}

    for alias in TRIBUNAIS:
        tribunal_nome = alias.upper()
        log(f"DataJud: consultando {tribunal_nome}...")

        consultas = [
            ("recuperacao_judicial", [{"term": {"classe.codigo": CLASSE_RJ}}, filtro_data]),
            ("falencia", [{"term": {"classe.codigo": CLASSE_FALENCIA}}, filtro_data]),
            ("cautelar_pre_rj", [
                {"term": {"classe.codigo": CLASSE_CAUTELAR}},
                {"term": {"assuntos.codigo": ASSUNTO_RJ_FALENCIA}},
                filtro_data,
            ]),
        ]

        for tipo, filtros in consultas:
            hits = consultar_datajud(f"api_publica_{alias}", filtros)
            for h in hits:
                numero = h.get("numeroProcesso", "").strip()
                if not numero:
                    continue
                orgao = (h.get("orgaoJulgador") or {}).get("nome", "")
                achados.append({
                    "fonte": "datajud",
                    "tipo": tipo,
                    "chave": f"datajud:{numero}",
                    "numero_processo": numero,
                    "tribunal": h.get("tribunal", tribunal_nome),
                    "vara": orgao,
                    "classe": (h.get("classe") or {}).get("nome", ""),
                    "data_ajuizamento": h.get("dataAjuizamento", ""),
                    "empresa": None,  # API pública não devolve partes — ver docstring
                    "url": None,
                })
            time.sleep(0.3)  # não martelar a API pública

    return achados


# ---------------------------------------------------------------------------
# Fonte 2: notícias (Google News RSS)
# ---------------------------------------------------------------------------

CONSULTAS_NOTICIAS = [
    '""pede recuperação judicial"',
    '"entra com pedido de recuperação judicial"',
    '""protocola pedido de recuperação judicial"',
    '"ajuíza recuperação judicial"',
    '"pedido de recuperação judicial" empresa',
    '"tutela cautelar" "recuperação judicial"',
    '"cautelar preparatória" "recuperação judicial"',
    '"cautelar pré-recuperação judicial"',
]

FONTES_RSS = [
    # (nome, função que monta a URL a partir da consulta)
    ("google_news", lambda q: "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-BR"})),
    ("bing_news", lambda q: "https://www.bing.com/news/search?" + urllib.parse.urlencode(
        {"q": q, "format": "rss", "setlang": "pt-BR", "cc": "BR"})),
]


def _extrair_manchete(titulo: str, veiculo: str) -> str:
    """Google/Bing formatam o título como 'Manchete - Veículo'; tira o veículo."""
    if veiculo and titulo.endswith(veiculo):
        return titulo[: -len(veiculo)].rstrip(" -–|")
    return titulo


def buscar_noticias() -> list[dict]:
    achados = []
    headers = {"User-Agent": USER_AGENT}

    for nome_fonte, montar_url in FONTES_RSS:
        for consulta in CONSULTAS_NOTICIAS:
            url = montar_url(consulta)
            log(f"Notícias ({nome_fonte}): buscando {consulta!r}...")

            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    xml_bruto = resp.read()
            except Exception as e:  # noqa: BLE001
                log(f"  falhou ({nome_fonte}, {consulta!r}): {e}")
                continue

            try:
                raiz = ET.fromstring(xml_bruto)
            except ET.ParseError as e:
                log(f"  XML inválido ({nome_fonte}, {consulta!r}): {e}")
                continue

            for item in raiz.findall(".//item"):
                titulo = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                fonte_el = item.find("source")
                veiculo = fonte_el.text.strip() if fonte_el is not None and fonte_el.text else ""
                if not titulo or not link:
                    continue

                achados.append({
                    "fonte": "noticia",
                    "tipo": "noticia_rj_ou_cautelar",
                    "chave": f"noticia:{link}",
                    "titulo": _extrair_manchete(titulo, veiculo),
                    "veiculo": veiculo,
                    "buscador": nome_fonte,
                    "consulta": consulta,
                    "data_publicacao": pub_date,
                    "empresa": None,  # não há extração automática — ler a manchete
                    "url": link,
                })
            time.sleep(0.5)

    return achados


# ---------------------------------------------------------------------------
# Persistência / dedupe / agregação para o painel
# ---------------------------------------------------------------------------

def carregar_json(caminho: str, padrao):
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return padrao


def salvar_json(caminho: str, obj) -> None:
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def data_ordenacao(achado: dict) -> str:
    """Melhor data disponível pra ordenar/filtrar, em ISO, string vazia se não houver."""
    if achado["fonte"] == "datajud":
        return achado.get("data_ajuizamento") or ""
    bruta = achado.get("data_publicacao") or ""
    if not bruta:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(bruta).astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def marcar_novos(achados: list[dict], vistos: dict, hoje_iso: str) -> list[dict]:
    for achado in achados:
        chave = achado["chave"]
        if chave in vistos:
            achado["novo"] = False
            achado["primeiro_visto"] = vistos[chave]
        else:
            achado["novo"] = True
            achado["primeiro_visto"] = hoje_iso
            vistos[chave] = hoje_iso
    return achados


def podar_vistos(vistos: dict, hoje: datetime) -> dict:
    limite = (hoje - timedelta(days=JANELA_VISTOS_DIAS)).date().isoformat()
    return {k: v for k, v in vistos.items() if v >= limite}


def montar_ultimo(hoje: datetime) -> dict:
    """Relê os diários dos últimos JANELA_DASHBOARD_DIAS dias e agrega, deduplicando."""
    por_chave: dict[str, dict] = {}
    for i in range(JANELA_DASHBOARD_DIAS):
        dia = (hoje - timedelta(days=i)).date().isoformat()
        caminho = f"{DIR_DIARIO}/{dia}.json"
        if not os.path.exists(caminho):
            continue
        for achado in carregar_json(caminho, []):
            chave = achado["chave"]
            existente = por_chave.get(chave)
            if existente is None or achado.get("primeiro_visto", "") < existente.get("primeiro_visto", ""):
                por_chave[chave] = achado

    achados = sorted(por_chave.values(), key=data_ordenacao, reverse=True)

    contagem_tipo: dict[str, int] = {}
    contagem_tribunal: dict[str, int] = {}
    for a in achados:
        contagem_tipo[a["tipo"]] = contagem_tipo.get(a["tipo"], 0) + 1
        if a["fonte"] == "datajud":
            contagem_tribunal[a["tribunal"]] = contagem_tribunal.get(a["tribunal"], 0) + 1

    return {
        "gerado_em": hoje.astimezone(timezone.utc).isoformat(),
        "janela_dias": JANELA_DASHBOARD_DIAS,
        "total": len(achados),
        "novos_hoje": sum(1 for a in achados if a.get("primeiro_visto", "")[:10] == hoje.date().isoformat()),
        "por_tipo": contagem_tipo,
        "por_tribunal": contagem_tribunal,
        "achados": achados,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(hoje: datetime | None = None) -> None:
    hoje = hoje or datetime.now(timezone.utc)
    hoje_iso = hoje.date().isoformat()
    desde = hoje - timedelta(days=JANELA_DATAJUD_DIAS)
    desde_iso = desde.strftime("%Y-%m-%dT00:00:00.000Z")

    log("=== Monitor RJ/Cautelar pré-RJ — início ===")

    achados_datajud = buscar_datajud(desde_iso)
    log(f"DataJud: {len(achados_datajud)} processo(s) encontrados na janela de {JANELA_DATAJUD_DIAS} dias.")

    achados_noticias = buscar_noticias()
    log(f"Notícias: {len(achados_noticias)} manchete(s) encontradas (antes de deduplicar).")

    achados_hoje = achados_datajud + achados_noticias

    vistos = carregar_json(ARQ_VISTOS, {})
    achados_hoje = marcar_novos(achados_hoje, vistos, hoje_iso)
    vistos = podar_vistos(vistos, hoje)
    salvar_json(ARQ_VISTOS, vistos)

    salvar_json(f"{DIR_DIARIO}/{hoje_iso}.json", achados_hoje)

    ultimo = montar_ultimo(hoje)
    salvar_json(ARQ_ULTIMO, ultimo)

    novos = sum(1 for a in achados_hoje if a["novo"])
    log(f"Achados novos hoje: {novos} de {len(achados_hoje)} no total da execução.")
    log(f"Painel: {ultimo['total']} achados na janela de {JANELA_DASHBOARD_DIAS} dias.")
    log("=== fim ===")


if __name__ == "__main__":
    sys.exit(main() or 0)
