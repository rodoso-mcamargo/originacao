# -*- coding: utf-8 -*-
"""Coletor diario de acoes de rating (Moody's Local, S&P Brasil, Fitch).

Le o que cada agencia publica de forma aberta, classifica a acao (rebaixamento,
elevacao, perspectiva, revisao, afirmacao...) e casa a entidade com os
emissores/devedores do painel (dados/ultimo.json e dados/cri_cra/ultimo.json).

Fontes, na ordem (feed/JSON antes de HTML; nunca contorna login, paywall,
captcha ou desafio anti-robo - quando bloqueia, registra no diagnostico):
  1. Moody's Local Brasil  moodyslocal.com.br/reporte/rating-action/
       tenta /feed/ (RSS), /wp-json/wp/v2/ e o HTML, nessa ordem.
  2. S&P Global Ratings Brasil  brazil.ratings.spglobal.com
       le o que a pagina /ratings/pt/regulatory/ratings-actions entrega no HTML
       (janela de 1 dia renderizada no servidor). A lista completa vem de uma
       API com token Bearer; esse token NAO e reaproveitado.
  3. Fitch  fitchratings.com
       sitemap publico de research (sitemap-research.xml, listado no
       robots.txt). Os comunicados sao renderizados no navegador e /page-data/
       e vedado pelo robots.txt; so a URL e lida (titulo, data e perspectiva
       saem do slug; a nota sai sem +/-, por isso fica null).

    python ratings_agencias.py                      # coleta do dia
    python ratings_agencias.py --desde 2026-09-29   # backfill (ate 12 meses)
    python ratings_agencias.py --inspecionar        # descreve as fontes, nao grava

Saidas (dados/ratings_agencias/):
    AAAA-MM-DD.json   acoes publicadas no dia (data da publicacao)
    base.csv          base acumulada, chave agencia+url
    universo.csv      so as acoes casadas com o painel
    grupos.csv        controladora;subsidiaria (editavel, comeca vazia)
    ultimo.json       ultimo run + diagnostico por agencia
"""
from __future__ import annotations

import socket

_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import logging
import os
import pathlib
import re
import sys
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET

log = logging.getLogger("ratings_agencias")

# ---------------------------------------------------------------------------
# Classificador. Ordem importa: a primeira regra que casar vence.
# Roda sobre titulo + primeiro paragrafo (sem acento, minusculas; hifens e
# underscores viram espaco, para o slug da Fitch). Na duvida: relatorio.
# ---------------------------------------------------------------------------
CLASSIFICADOR: list[tuple[str, str]] = [
    ("em_revisao", r"creditwatch|credit watch|rating watch|em revisao|sob revisao|"
                   r"observacao (negativa|positiva)|review for (downgrade|upgrade)|"
                   r"places? .{0,60} on (rating )?watch|coloca .{0,60} em (observacao|revisao)"),
    ("rebaixamento", r"\brebaix|\bdowngrad|\bcorta (o |os )?rating"),
    # "eleva" so perto de rating/nota: "custos elevados" num comentario setorial nao e elevacao
    ("elevacao", r"\beleva\w* .{0,50}\b(ratings?|notas?|classificac)|\b(ratings?|notas?) .{0,30}\belevad|"
                 r"\bupgrad|\bmelhora (o |os )?rating"),
    ("perspectiva_negativa", r"(revis|alter|mud)\w* .{0,60}perspectiva .{0,20}negativ|"
                             r"perspectiva .{0,30}para negativ|outlook .{0,20}(to|para) negativ|"
                             r"revises? .{0,40}outlook .{0,20}negativ"),
    ("perspectiva_positiva", r"(revis|alter|mud)\w* .{0,60}perspectiva .{0,20}positiv|"
                             r"perspectiva .{0,30}para positiv|outlook .{0,20}(to|para) positiv|"
                             r"revises? .{0,40}outlook .{0,20}positiv"),
    ("retirada", r"\bretira|\bwithdraw|\bencerra (o |os )?rating"),
    ("atribuicao", r"\batribui|\bassign|\bfitch rates\b|\bpublica rating|\bclassifica\b|"
                   r"\brating inicial|\bnovo rating|\bexpected rating"),
    ("afirmacao", r"\bafirma|\breafirma|\baffirm|\bmantem (o |os )?rating"),
]
ACOES = [a for a, _ in CLASSIFICADOR] + ["relatorio"]
_CLASS_RX = [(a, re.compile(rx)) for a, rx in CLASSIFICADOR]

# Notas por agencia (escala nacional primeiro).
RX_NOTA = {
    "moodys_local": re.compile(r"\b(?:ML\s)?(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|D|A-[1-4])\.br\b"),
    "sp": re.compile(r"\bbr(?:AAA|AA[+-]?|A-[1-3]\+?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|D|SD)(?:\s?\(sf\))?"),
    "fitch": re.compile(r"\b(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|RD|D|F[1-3]\+?)(?:sf)?\(bra\)"),
}
RX_PERSPECTIVA = [
    ("em_revisao_negativa", r"(creditwatch|credit watch|rating watch|observacao|revisao) .{0,25}negativ|review for downgrade|revisao para rebaix"),
    ("em_revisao_positiva", r"(creditwatch|credit watch|rating watch|observacao|revisao) .{0,25}positiv|review for upgrade|revisao para eleva"),
    ("negativa", r"(perspectiva|outlooks?) .{0,25}negativ"),
    ("positiva", r"(perspectiva|outlooks?) .{0,25}positiv"),
    ("em_desenvolvimento", r"(perspectiva|outlooks?) .{0,25}(em desenvolvimento|developing|evolving)"),
    ("estavel", r"(perspectiva|outlooks?) .{0,25}(estavel|stable)"),
]
RX_INSTRUMENTO = [
    ("debentures", r"debentur"),
    ("cri", r"\bcri\b|certificados? de recebiveis imobiliarios"),
    ("cra", r"\bcra\b|certificados? de recebiveis do agronegocio"),
    ("fidc", r"\bfidc\b|direitos creditorios|senior quotas|cotas seniores|quotas"),
    ("notas_comerciais", r"notas? comercia|notas? promissoria|commercial notes"),
    ("letras_financeiras", r"letras? financeira"),
    ("bonds", r"\bbonds?\b|\bnotes\b|senior unsecured"),
]

# Normalizacao de nomes (regra 11).
SUFIXOS = {"S", "A", "SA", "LTDA", "CIA", "COMPANHIA", "PARTICIPACOES", "PARTICIPACAO",
           "EIRELI", "ME", "EPP", "SPE"}
SUFIXOS_FRASE = ["EM RECUPERACAO JUDICIAL", "S A", "S/A"]
GENERICAS = {
    "ENERGIA", "SANEAMENTO", "TRANSMISSORA", "TRANSMISSAO", "GERACAO", "DISTRIBUICAO",
    "DISTRIBUIDORA", "BRASIL", "BRASILEIRA", "BANCO", "HOLDING", "HOLDINGS", "GRUPO",
    "INVESTIMENTOS", "INVESTIMENTO", "SECURITIZADORA", "CONCESSIONARIA", "RODOVIAS",
    "RODOVIA", "AGUAS", "AGUA", "SERVICOS", "INDUSTRIA", "COMERCIO", "FUNDO", "CREDITO",
    "FINANCEIRA", "EMPREENDIMENTOS", "IMOBILIARIOS", "IMOBILIARIA", "LOGISTICA",
    "ENERGETICA", "ELETRICA", "SOLAR", "EOLICA", "TELECOM", "AGRO", "AGRICOLA",
    "ALIMENTOS", "SAUDE", "EDUCACAO", "SEGUROS", "CAPITAL", "GESTAO", "INFRAESTRUTURA",
    "PORTOS", "TERMINAIS", "MINERACAO", "SIDERURGICA", "PETROLEO", "GAS", "OLEO",
    "NORTE", "SUL", "NORDESTE", "CENTRO", "OESTE", "LESTE", "SAO", "PAULO", "RIO",
    "JANEIRO", "MINAS", "GERAIS", "DO", "DA", "DE", "DOS", "DAS", "E", "THE", "OF",
    "AND", "I", "II", "III", "IV", "1", "2", "3", "4", "5", "COOPERATIVA", "CENTRAL",
    "SECURITIZACAO", "FIDC", "CRI", "CRA", "DEBENTURES", "NACIONAL", "INTERNACIONAL",
}
LIMIAR_FUZZY = 92
GEOGRAFICAS = {"BRASIL", "BRASILEIRA", "BRASILEIRO", "BRAZIL", "BRAZILIAN", "BR", "INTERNATIONAL",
               "INTERNACIONAL", "GLOBAL", "LATAM", "AMERICA", "USA", "US", "MEXICO", "CHILE", "PERU",
               "COLOMBIA", "ARGENTINA", "EUROPE", "SPAIN", "ESPANHA", "PORTUGAL"}

UA = "RadarSecundario-coletor/1.0 (+https://github.com/rodoso-mcamargo/originacao)"
TIMEOUT = 45
INTERVALO = 1.1  # <= 1 req/s
FUSO = dt.timezone(dt.timedelta(hours=-3))
MAX_BACKFILL_DIAS = 366

RAIZ = pathlib.Path(__file__).resolve().parent
DIR_OUT = RAIZ / "dados" / "ratings_agencias"
UNIVERSO_PAINEL = RAIZ / "dados" / "ultimo.json"
UNIVERSO_CRI = RAIZ / "dados" / "cri_cra" / "ultimo.json"

CAMPOS = ["agencia", "data", "entidade", "escala", "acao", "nota_anterior", "nota_nova",
          "perspectiva", "instrumento", "titulo", "url"]
CAMPOS_BASE = CAMPOS + ["coletado_em"]
CAMPOS_UNIVERSO = ["agencia", "data", "entidade", "acao", "nota_anterior", "nota_nova",
                   "perspectiva", "codigos", "emissor_painel", "tipo_match", "score", "url"]

MOODYS_BASE = "https://moodyslocal.com.br"
MOODYS_URLS = [
    ("rss", MOODYS_BASE + "/reporte/rating-action/feed/"),
    ("wp-json", MOODYS_BASE + "/wp-json/wp/v2/"),
    ("html", MOODYS_BASE + "/reporte/rating-action/"),
]
SP_BASE = "https://brazil.ratings.spglobal.com"
SP_URLS = [
    ("html-ssr", SP_BASE + "/ratings/pt/regulatory/ratings-actions"),
    ("html-ssr-home", SP_BASE + "/pt"),
]
FITCH_SITEMAP = "https://www.fitchratings.com/sitemap-research.xml"


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def texto_classif(s: str) -> str:
    s = sem_acento(html.unescape(s or "")).lower()
    s = re.sub(r"[-_/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalizar_nome(nome: str | None) -> str:
    """Regra 11: sem acento, maiusculas, sem pontuacao, sem sufixos societarios."""
    if not nome:
        return ""
    s = sem_acento(html.unescape(str(nome))).upper()
    s = s.replace("S/A", " SA ").replace("S.A.", " SA ").replace("S.A", " SA ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for f in SUFIXOS_FRASE:
        s = re.sub(rf"\b{f}\b", " ", s)
    toks = s.split()
    while toks and toks[-1] in SUFIXOS:
        toks.pop()
    toks = [t for t in toks if t not in ("CIA", "COMPANHIA", "LTDA", "PARTICIPACOES")]
    return " ".join(toks)


def tokens_distintivos(nome_norm: str) -> set[str]:
    return {t for t in nome_norm.split() if t not in GENERICAS and len(t) >= 3}


# ---------------------------------------------------------------------------
# Extracao de campos
# ---------------------------------------------------------------------------
def classificar(titulo: str, paragrafo: str = "") -> str:
    t = texto_classif(f"{titulo} {paragrafo}")
    for acao, rx in _CLASS_RX:
        if rx.search(t):
            return acao
    return "relatorio"


def extrair_perspectiva(texto: str) -> str | None:
    t = texto_classif(texto)
    for nome, rx in RX_PERSPECTIVA:
        if re.search(rx, t):
            return nome
    return None


def extrair_instrumento(texto: str) -> str | None:
    t = texto_classif(texto)
    for nome, rx in RX_INSTRUMENTO:
        if re.search(rx, t):
            return nome
    return None


def extrair_notas(agencia: str, texto: str, acao: str) -> tuple[str | None, str | None]:
    """(nota_anterior, nota_nova). Com duas notas numa mudanca, 'de X para Y'."""
    rx = RX_NOTA.get(agencia)
    if not rx or not texto:
        return None, None
    notas = [m.group(0).strip() for m in rx.finditer(html.unescape(texto))]
    vistas: list[str] = []
    for n in notas:
        if n not in vistas:
            vistas.append(n)
    if not vistas:
        return None, None
    if acao in ("rebaixamento", "elevacao") and len(vistas) >= 2:
        return vistas[0], vistas[1]
    return None, vistas[0]


def escala_de(agencia: str, texto: str, url: str = "") -> str | None:
    if RX_NOTA.get(agencia) and RX_NOTA[agencia].search(texto or ""):
        return "nacional"
    t = texto_classif(texto) + " " + url.lower()
    if re.search(r"\bbra\b|\.br\b|escala nacional|national scale|/pt/", t):
        return "nacional"
    if agencia == "fitch":
        return "internacional"
    return None


RX_ENTIDADE_PT = re.compile(
    r"(?i:ratings?|notas?|classifica\w+|perspectiva)\b.{0,60}?\b(?i:da|do|das|dos|de|a|ao|à)\s+"
    r"(?P<ent>[A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ][^;:,()]{2,90}?)"
    r"(?=\s*(?:[;:,(]|\se\s(?=[a-z])|\s(?:para|com|em|apos|após|de\s+[A-Z]{1,3}[+-]?\.br)\s|$))")


def extrair_entidade_pt(titulo: str) -> str | None:
    """Entidade num titulo em portugues ('... afirma os ratings da XYZ S.A.; ...')."""
    if not titulo:
        return None
    t = html.unescape(titulo)
    m = RX_ENTIDADE_PT.search(t)
    if m:
        ent = m.group("ent").strip(" -")
        ent = re.sub(r"\s+(?:em|para)\s+.*$", "", ent)
        # "5a emissao de debentures da Rumo S.A." -> "Rumo S.A."
        ent = re.sub(r"^.*\b(?:emiss\w+|s[eé]ries?|deb[eê]ntures?|cotas|quotas|CRIs?|CRAs?|notas comerciais)\b.*?\b(?:da|do|das|dos)\s+", "", ent, flags=re.I)
        return ent or None
    return None


# ---------------------------------------------------------------------------
# Fitch: slug da URL
# ---------------------------------------------------------------------------
FITCH_VERBOS = (r"affirms|upgrades|downgrades|assigns|rates|withdraws|places|revises|"
                r"maintains|resolves|removes|publishes|takes|extends|downgrade|upgrade|"
                r"affirm|assign|withdraw")
RX_FITCH_SLUG = re.compile(r"^(?P<corpo>.+?)-(?P<d>\d{1,2})-(?P<m>\d{1,2})-(?P<y>20\d\d)$")
RX_FITCH_NOTA_SLUG = r"(?:aaa|aa|a|bbb|bb|b|ccc|cc|c|rd|d|f[1-3])(?:sf)?(?:-bra)?"
RX_FITCH_LIXO_FIM = re.compile(
    r"(?:-(?:s|idrs?|long-term|short-term|national|natl|scale|ratings?|ifs|vr|debentures?|"
    r"issuances?|issues?|notes|programme|program|cri|cra|bonds?|and|the|proposed|senior|"
    r"unsecured|quotas|series|outlooks?|stable|negative|positive|final|expected))+$")


def fitch_slug(url: str) -> dict | None:
    caminho = url.rstrip("/").split("?")[0]
    slug = caminho.rsplit("/", 1)[-1]
    m = RX_FITCH_SLUG.match(slug)
    if not m:
        return None
    try:
        data = dt.date(int(m["y"]), int(m["m"]), int(m["d"])).isoformat()
    except ValueError:
        data = None
    corpo = m["corpo"]
    entidade = None
    mv = re.match(rf"^fitch-(?:{FITCH_VERBOS})-(?P<resto>.+)$", corpo)
    if mv:
        resto = mv["resto"]
        # B: fitch-assigns-aaasf-bra-rating-to-<entidade> / fitch-affirms-aa-bra-from-<entidade>
        mb = re.match(rf"^(?:{RX_FITCH_NOTA_SLUG})(?:-ratings?)?-(?:to|from|of|for|on)-(?P<e>.+)$", resto)
        if mb:
            ent = mb["e"]
            ent = re.split(r"-(?:outlook|rating-watch|ratings?-)", ent)[0]
            ent = re.sub(r"^(?:\w+-)?(?:series|issues?|issuance|tranche)-.*?-of-(?:\w+-)*?(?:quotas|debentures|notes|cri|cra)-of-", "", ent)
            ent = re.sub(r"^.*?-(?:quotas|debentures)-of-", "", ent)
            entidade = ent
        else:
            ma = re.match(r"^(?P<e>.+?)-(?:idrs?-|ifs-rating-|long-term-idr-|national-ratings?-|ratings?-)?(?:at|to)-", resto)
            mc = re.match(r"^(?P<e>.+?)-(?:ratings?|outlooks?|national-scale)(?:-|$)", resto)
            mfinal = re.match(r"^(?:final-|expected-)?ratings?-to-(?P<e>.+)$", resto)
            if mfinal:
                entidade = mfinal["e"]
            elif ma:
                entidade = ma["e"]
            elif mc:
                entidade = mc["e"]
            else:
                entidade = resto
    else:
        entidade = corpo  # sem verbo: relatorio sobre a entidade
    if entidade:
        entidade = RX_FITCH_LIXO_FIM.sub("", entidade)
        entidade = re.sub(r"-(?:outlook|rating).*$", "", entidade)
        entidade = entidade.replace("-", " ").strip().upper() or None
    titulo = corpo.replace("-", " ")
    return {"data": data, "entidade": entidade, "titulo": titulo, "slug": slug}


def parse_fitch_sitemap(xml_texto: str) -> list[dict]:
    """Entradas de research do sitemap: [{url, lastmod}]."""
    try:
        raiz = ET.fromstring(xml_texto.encode("utf-8") if isinstance(xml_texto, str) else xml_texto)
    except ET.ParseError:
        return []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out = []
    for u in raiz.findall("s:url", ns):
        loc = (u.findtext("s:loc", default="", namespaces=ns) or "").strip()
        lm = (u.findtext("s:lastmod", default="", namespaces=ns) or "").strip() or None
        if "/research/" in loc:
            out.append({"url": loc, "lastmod": lm})
    return out


def fitch_e_brasil(url: str, slug: str) -> bool:
    return "/research/pt/" in url or bool(re.search(r"(?:^|-)bra(?:-|$)", slug))


def registro_fitch(url: str) -> dict | None:
    info = fitch_slug(url)
    if not info:
        return None
    titulo = info["titulo"]
    acao = classificar(titulo)
    return {
        "agencia": "fitch",
        "data": info["data"],
        "entidade": info["entidade"],
        "escala": "nacional" if fitch_e_brasil(url, info["slug"]) else "internacional",
        "acao": acao,
        "nota_anterior": None,   # o slug perde +/-: nao da para afirmar a nota
        "nota_nova": None,
        "perspectiva": extrair_perspectiva(titulo),
        "instrumento": extrair_instrumento(titulo),
        "titulo": titulo,
        "url": url,
    }


# ---------------------------------------------------------------------------
# Moody's Local: RSS do WordPress
# ---------------------------------------------------------------------------
def primeiro_paragrafo(html_texto: str) -> str:
    if not html_texto:
        return ""
    t = html.unescape(html_texto)
    m = re.search(r"<p[^>]*>(.*?)</p>", t, re.S | re.I)
    t = m.group(1) if m else t
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:600]


def data_rss(s: str | None) -> str | None:
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s.strip())
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        return d.date().isoformat()
    return d.astimezone(FUSO).date().isoformat()


def parse_rss(xml_texto: str) -> list[dict]:
    """Itens de um RSS 2.0: [{titulo, url, data, paragrafo}]."""
    try:
        raiz = ET.fromstring(xml_texto.encode("utf-8") if isinstance(xml_texto, str) else xml_texto)
    except ET.ParseError:
        return []
    out = []
    for it in raiz.iter("item"):
        titulo = html.unescape((it.findtext("title") or "").strip())
        url = (it.findtext("link") or "").strip()
        desc = it.findtext("description") or ""
        conteudo = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        out.append({"titulo": titulo, "url": url, "data": data_rss(it.findtext("pubDate")),
                    "paragrafo": primeiro_paragrafo(desc or conteudo)})
    return out


def registro_texto(agencia: str, titulo: str, paragrafo: str, url: str, data: str | None,
                   entidade: str | None = None) -> dict:
    texto = f"{titulo} {paragrafo}"
    acao = classificar(titulo, paragrafo)
    ant, nova = extrair_notas(agencia, texto, acao)
    return {
        "agencia": agencia,
        "data": data,
        "entidade": entidade or extrair_entidade_pt(titulo),
        "escala": escala_de(agencia, texto, url),
        "acao": acao,
        "nota_anterior": ant,
        "nota_nova": nova,
        "perspectiva": extrair_perspectiva(texto),
        "instrumento": extrair_instrumento(texto),
        "titulo": titulo,
        "url": url,
    }


def e_desafio_antirobo(resp) -> bool:
    if resp is None:
        return False
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    corpo = resp.text[:3000] if resp.text else ""
    return resp.status_code in (403, 429, 503) and (
        "Just a moment" in corpo or "challenge-platform" in corpo or "_cf_chl_opt" in corpo
        or "captcha" in corpo.lower())


# ---------------------------------------------------------------------------
# S&P: dados que a pagina entrega renderizados no servidor (__NEXT_DATA__)
# ---------------------------------------------------------------------------
def next_data(html_texto: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                  html_texto or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _pega(d: dict, *padroes: str, exato: bool = False):
    """Primeiro valor nao vazio cuja chave (minusculas) contem (ou e, com exato) um padrao."""
    for p in padroes:
        for k, v in d.items():
            if (k.lower() == p if exato else p in k.lower()) and v not in (None, "", []):
                return v
    return None


def data_sp(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    meses = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
             "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)          # 20-Aug-2026 11:25 BRT
    if m and m.group(2).lower() in meses:
        return dt.date(int(m.group(3)), meses[m.group(2).lower()], int(m.group(1))).isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)                      # ISO
    if m:
        return m.group(1)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)                  # dd/mm/aaaa
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def registros_sp(nd: dict) -> tuple[list[dict], dict]:
    """Extrai acoes do pageProps (ratingAction) e artigos da home (articles)."""
    pp = (nd or {}).get("props", {}).get("pageProps", {}) or {}
    info = {"chaves_pageProps": sorted(k for k in pp if k not in ("translations", "headerData", "footerData")),
            "totalNumberOfRecords": (pp.get("RatingActionsAPIData") or {}).get("totalNumberOfRecords")}
    brutos = []
    for chave in ("ratingAction", "RatingAction", "articles"):
        v = pp.get(chave)
        if isinstance(v, list):
            brutos += [(chave, x) for x in v if isinstance(x, dict)]
    info["chaves_registro"] = sorted({k for _, x in brutos for k in x})[:40]
    out = []
    for origem, x in brutos:
        titulo = _pega(x, "articletitle", "title", "headline") or ""
        paragrafo = _pega(x, "articledescription", "description", "summary") or ""
        sid = _pega(x, "sourceobjectid", "sourceid", "articleid")
        url = _pega(x, "url", "link")
        if not url and sid:
            url = f"{SP_BASE}/ratings/pt/regulatory/article/-/view/sourceId/{sid}"
        if not url:
            continue
        entidade = _pega(x, "entityname", "issuername", "organizationname", "entity")
        data = data_sp(_pega(x, "ratingactiondate", "actiondate", "articledate", "date"))
        r = registro_texto("sp", str(titulo), str(paragrafo), str(url), data,
                           entidade=str(entidade) if entidade else None)
        nova = _pega(x, "currentrating", "newrating", "rating", "torating", exato=True)
        ant = _pega(x, "previousrating", "priorrating", "fromrating", exato=True)
        persp = _pega(x, "creditwatch", "outlook", "creditwatchoutlook", exato=True)
        if isinstance(nova, str) and nova.strip():
            r["nota_nova"] = nova.strip()
        if isinstance(ant, str) and ant.strip():
            r["nota_anterior"] = ant.strip()
        if isinstance(persp, str) and persp.strip() and not r["perspectiva"]:
            r["perspectiva"] = extrair_perspectiva("perspectiva " + persp) or persp.strip().lower()
        out.append(r)
    return out, info


# ---------------------------------------------------------------------------
# Universo do painel e casamento (regra 11)
# ---------------------------------------------------------------------------
def carregar_universo(painel=UNIVERSO_PAINEL, cri=UNIVERSO_CRI) -> dict[str, dict]:
    """nome normalizado -> {'emissor': nome original, 'codigos': set()}"""
    uni: dict[str, dict] = {}

    def add(nome, codigo):
        n = normalizar_nome(nome)
        if not n or not tokens_distintivos(n):
            return
        e = uni.setdefault(n, {"emissor": nome, "codigos": set()})
        if codigo:
            e["codigos"].add(codigo)

    for arq, lista, campos in ((painel, "papeis", ("emissor",)),
                               (cri, "ativos", ("devedor", "emissor"))):
        try:
            d = json.loads(pathlib.Path(arq).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("universo %s indisponivel: %s", arq, e)
            continue
        for p in d.get(lista, []) or []:
            nome = next((p.get(c) for c in campos if p.get(c)), None)
            if nome:
                add(nome, p.get("codigo"))
    return uni


def carregar_grupos(caminho: pathlib.Path) -> dict[str, list[str]]:
    grupos: dict[str, list[str]] = {}
    if not caminho.exists():
        return grupos
    with caminho.open(encoding="utf-8") as f:
        for row in csv.DictReader((l for l in f if l.strip() and not l.startswith("#")), delimiter=";"):
            c = normalizar_nome(row.get("controladora"))
            s = normalizar_nome(row.get("subsidiaria"))
            if c and s:
                grupos.setdefault(c, []).append(s)
    return grupos


def casar(entidade: str | None, universo: dict[str, dict], grupos: dict | None = None) -> list[dict]:
    """[{emissor_painel, codigos, tipo_match, score}] - exato, depois fuzzy, depois grupo."""
    n = normalizar_nome(entidade)
    if not n or not tokens_distintivos(n):
        return []  # nunca casar so por palavra generica
    achados: list[dict] = []
    if n in universo:
        achados.append({"chave": n, "tipo_match": "exato", "score": 100})
    else:
        try:
            from rapidfuzz import fuzz
        except ImportError:  # pragma: no cover
            fuzz = None
        if fuzz:
            dn = tokens_distintivos(n)
            melhor = None
            for cand in universo:
                dc = tokens_distintivos(cand)
                comuns = dn & dc
                if not comuns:
                    continue
                menor = dn if len(dn) <= len(dc) else dc
                if not menor <= comuns:   # todo token distintivo do nome menor tem de estar no outro
                    continue
                # "TELEFONICA SA" (Espanha) x "TELEFONICA BRASIL": a palavra de pais que sobra
                # separa as entidades; controle/grupo so pela tabela grupos.csv
                if (set(n.split()) ^ set(cand.split())) & GEOGRAFICAS:
                    continue
                sc = fuzz.token_set_ratio(n, cand)
                if sc >= LIMIAR_FUZZY and (melhor is None or sc > melhor[1]):
                    melhor = (cand, sc)
            if melhor:
                achados.append({"chave": melhor[0], "tipo_match": "fuzzy", "score": round(melhor[1], 1)})
    for sub in (grupos or {}).get(n, []):
        if sub in universo and all(a["chave"] != sub for a in achados):
            achados.append({"chave": sub, "tipo_match": f"grupo:{n}", "score": 100})
    out = []
    for a in achados:
        u = universo[a["chave"]]
        out.append({"emissor_painel": u["emissor"], "codigos": ";".join(sorted(u["codigos"])),
                    "tipo_match": a["tipo_match"], "score": a["score"]})
    return out


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------
class Cliente:
    def __init__(self):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5"})
        self._ultimo = 0.0

    def get(self, url, **kw):
        espera = INTERVALO - (time.monotonic() - self._ultimo)
        if espera > 0:
            time.sleep(espera)
        try:
            return self.s.get(url, timeout=TIMEOUT, **kw)
        finally:
            self._ultimo = time.monotonic()


def novo_diag(fonte: str) -> dict:
    return {"status": "nao_rodou", "fonte_usada": None, "tentativas": [], "lidos": 0,
            "validos": 0, "novos_na_base": 0, "descartados": {}, "erros": [], "fonte": fonte}


def _desc(diag, motivo, n=1):
    diag["descartados"][motivo] = diag["descartados"].get(motivo, 0) + n


def coletar_moodys(cli: Cliente, desde: str | None, diag: dict, amostra: dict) -> list[dict]:
    regs: list[dict] = []
    for tipo, url in MOODYS_URLS:
        try:
            r = cli.get(url)
        except Exception as e:  # rede
            diag["tentativas"].append({"url": url, "erro": f"{type(e).__name__}: {e}"[:200]})
            diag["erros"].append(f"{tipo}: {type(e).__name__}")
            continue
        t = {"url": url, "http": r.status_code, "content_type": r.headers.get("content-type")}
        if e_desafio_antirobo(r):
            t["bloqueio"] = "desafio anti-robo (Cloudflare); nao contornado"
            diag["tentativas"].append(t)
            continue
        diag["tentativas"].append(t)
        if r.status_code != 200:
            continue
        if tipo == "rss":
            itens = parse_rss(r.text)
            amostra["moodys_local"] = {"url": url, "campos": ["titulo", "url", "data", "paragrafo"],
                                       "registros": itens[:3]}
            pagina = 1
            while itens:
                for it in itens:
                    regs.append(registro_texto("moodys_local", it["titulo"], it["paragrafo"],
                                               it["url"], it["data"]))
                datas = [it["data"] for it in itens if it["data"]]
                if not desde or not datas or min(datas) < desde or pagina >= 60:
                    break
                pagina += 1
                rp = cli.get(f"{url}?paged={pagina}")
                if rp.status_code != 200 or e_desafio_antirobo(rp):
                    diag["tentativas"].append({"url": f"{url}?paged={pagina}", "http": rp.status_code})
                    break
                itens = parse_rss(rp.text)
            diag["fonte_usada"] = "rss"
            break
        if tipo == "wp-json":
            # Indice da API: so registra os tipos de post disponiveis (a rota exata
            # dos rating actions so pode ser confirmada com o site acessivel).
            try:
                rotas = sorted(k for k in (r.json().get("routes") or {}) if "rating" in k.lower())
            except ValueError:
                rotas = []
            t["rotas_rating"] = rotas[:20]
            continue
        if tipo == "html":
            links = re.findall(r'<a[^>]+href="(https://(?:www\.)?moodyslocal\.com\.br/[^"]+)"[^>]*>([^<]{15,300})</a>', r.text)
            vistos = set()
            for href, txt in links:
                if href in vistos or "/reporte/" in href.rstrip("/").rsplit("/", 1)[-1]:
                    continue
                vistos.add(href)
                regs.append(registro_texto("moodys_local", txt.strip(), "", href, None))
            amostra["moodys_local"] = {"url": url, "campos": ["titulo", "url"],
                                       "registros": [{"titulo": t2, "url": h2} for h2, t2 in links[:3]]}
            diag["fonte_usada"] = "html"
            break
    if diag["fonte_usada"] is None:
        diag["status"] = "bloqueado" if any(t.get("bloqueio") for t in diag["tentativas"]) else "erro"
    return regs


def coletar_sp(cli: Cliente, desde: str | None, diag: dict, amostra: dict) -> list[dict]:
    regs: list[dict] = []
    for tipo, url in SP_URLS:
        try:
            r = cli.get(url)
        except Exception as e:
            diag["tentativas"].append({"url": url, "erro": f"{type(e).__name__}: {e}"[:200]})
            diag["erros"].append(f"{tipo}: {type(e).__name__}")
            continue
        t = {"url": url, "http": r.status_code}
        if e_desafio_antirobo(r):
            t["bloqueio"] = "desafio anti-robo; nao contornado"
        diag["tentativas"].append(t)
        if r.status_code != 200:
            continue
        nd = next_data(r.text)
        if not nd:
            t["obs"] = "sem __NEXT_DATA__"
            continue
        rs, info = registros_sp(nd)
        t.update(info)
        regs += rs
        diag["fonte_usada"] = (diag["fonte_usada"] + "+" if diag["fonte_usada"] else "") + tipo
        amostra.setdefault("sp", {"url": url, "campos": info.get("chaves_registro"), "registros": []})
        amostra["sp"]["registros"] = (amostra["sp"]["registros"] + rs)[:3]
    if diag["fonte_usada"] is None:
        diag["status"] = "bloqueado" if any(t.get("bloqueio") for t in diag["tentativas"]) else "erro"
    return regs


def coletar_fitch(cli: Cliente, desde: str | None, diag: dict, amostra: dict,
                  universo: dict) -> list[dict]:
    regs: list[dict] = []
    try:
        r = cli.get(FITCH_SITEMAP)
    except Exception as e:
        diag["tentativas"].append({"url": FITCH_SITEMAP, "erro": f"{type(e).__name__}: {e}"[:200]})
        diag["erros"].append(type(e).__name__)
        diag["status"] = "erro"
        return regs
    t = {"url": FITCH_SITEMAP, "http": r.status_code}
    diag["tentativas"].append(t)
    if e_desafio_antirobo(r) or r.status_code != 200:
        t["bloqueio"] = "desafio anti-robo; nao contornado" if e_desafio_antirobo(r) else None
        diag["status"] = "bloqueado" if t["bloqueio"] else "erro"
        return regs
    entradas = parse_fitch_sitemap(r.text)
    t["urls_research"] = len(entradas)
    diag["fonte_usada"] = "sitemap-research"
    for e in entradas:
        rg = registro_fitch(e["url"])
        if not rg:
            _desc(diag, "slug_sem_data")
            continue
        slug = e["url"].rsplit("/", 1)[-1]
        if not fitch_e_brasil(e["url"], slug) and not any(
                m["tipo_match"] == "exato" for m in casar(rg["entidade"], universo)):
            _desc(diag, "fora_do_brasil_e_fora_do_universo")
            continue
        regs.append(rg)
    amostra["fitch"] = {"url": FITCH_SITEMAP, "campos": ["loc", "lastmod"],
                        "registros": [{"url": x["url"], "titulo": x["titulo"], "data": x["data"]} for x in regs[:3]]
                        or entradas[:3]}
    return regs


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------
def ler_base(caminho: pathlib.Path) -> dict[tuple[str, str], dict]:
    base: dict[tuple[str, str], dict] = {}
    if caminho.exists():
        with caminho.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                row = {k: (v if v != "" else None) for k, v in row.items()}
                base[(row["agencia"], row["url"])] = row
    return base


def gravar_csv(caminho: pathlib.Path, campos: list[str], linhas: list[dict]):
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for l in linhas:
            w.writerow({k: ("" if l.get(k) is None else l.get(k)) for k in campos})


def montar_universo_csv(base: dict, universo: dict, grupos: dict) -> list[dict]:
    linhas = []
    for (_, _), r in base.items():
        for m in casar(r.get("entidade"), universo, grupos):
            linhas.append({**{k: r.get(k) for k in CAMPOS_UNIVERSO if k in r}, **m})
    linhas.sort(key=lambda x: (x.get("data") or "", x["agencia"], x["url"]), reverse=True)
    return linhas


def escrever_summary(texto: str):
    r = os.environ.get("GITHUB_STEP_SUMMARY")
    if r:
        with open(r, "a", encoding="utf-8") as f:
            f.write(texto + "\n")


def resumo_md(saida: dict) -> str:
    l = [f"## Ratings das agencias - {saida['data_referencia']}", ""]
    if saida.get("desde"):
        l.append(f"Backfill desde {saida['desde']}\n")
    l.append("| Agencia | Status | Fonte | Lidos | Validos | Novos na base | Descartados |")
    l.append("|---|---|---|---:|---:|---:|---|")
    for ag, d in saida["diagnostico"]["agencias"].items():
        l.append(f"| {ag} | {d['status']} | {d.get('fonte_usada') or '-'} | {d['lidos']} | {d['validos']} | "
                 f"{d['novos_na_base']} | {d['descartados'] or '-'} |")
    l.append("")
    l.append(f"- Acoes por tipo (run): `{saida['contagens']['por_acao']}`")
    l.append(f"- Base: **{saida['contagens']['base_total']}** acoes; casadas com o painel: "
             f"**{saida['contagens']['universo_linhas']}** linhas")
    for ag, d in saida["diagnostico"]["agencias"].items():
        for t in d["tentativas"]:
            if t.get("bloqueio"):
                l.append(f"- {ag}: `{t['url']}` HTTP {t.get('http')} - {t['bloqueio']}")
    return "\n".join(l)


def inspecionar(cli: Cliente, universo: dict) -> str:
    amostra: dict = {}
    diags = {"moodys_local": novo_diag("moodyslocal.com.br"), "sp": novo_diag("brazil.ratings.spglobal.com"),
             "fitch": novo_diag("fitchratings.com")}
    coletar_moodys(cli, None, diags["moodys_local"], amostra)
    coletar_sp(cli, None, diags["sp"], amostra)
    coletar_fitch(cli, None, diags["fitch"], amostra, universo)
    l = ["## Ratings das agencias - inspecao (nada gravado)", ""]
    for ag, d in diags.items():
        l.append(f"### {ag}\n")
        for t in d["tentativas"]:
            l.append(f"- `{t.get('url')}` -> HTTP {t.get('http', '-')}"
                     + (f" | {t['bloqueio']}" if t.get("bloqueio") else "")
                     + (f" | erro {t['erro']}" if t.get("erro") else "")
                     + (f" | total SSR {t['totalNumberOfRecords']}" if "totalNumberOfRecords" in t else "")
                     + (f" | urls research {t['urls_research']}" if "urls_research" in t else ""))
        a = amostra.get(ag)
        if a:
            l.append(f"\nColunas reais: `{a.get('campos')}`\n")
            for x in a.get("registros", [])[:3]:
                l.append(f"- `{json.dumps(x, ensure_ascii=False)[:400]}`")
        else:
            l.append("\nSem registros lidos.")
        l.append("")
    return "\n".join(l)


def executar(desde: str | None, hoje: dt.date) -> dict:
    DIR_OUT.mkdir(parents=True, exist_ok=True)
    grupos_csv = DIR_OUT / "grupos.csv"
    if not grupos_csv.exists():
        grupos_csv.write_text("controladora;subsidiaria\n", encoding="utf-8")
    universo = carregar_universo()
    grupos = carregar_grupos(grupos_csv)
    cli = Cliente()
    amostra: dict = {}
    diags = {"moodys_local": novo_diag("moodyslocal.com.br"), "sp": novo_diag("brazil.ratings.spglobal.com"),
             "fitch": novo_diag("fitchratings.com")}
    brutos = {}
    for ag, fn in (("moodys_local", lambda d: coletar_moodys(cli, desde, d, amostra)),
                   ("sp", lambda d: coletar_sp(cli, desde, d, amostra)),
                   ("fitch", lambda d: coletar_fitch(cli, desde, d, amostra, universo))):
        try:
            brutos[ag] = fn(diags[ag])
        except Exception as e:
            log.exception("falha em %s", ag)
            diags[ag]["erros"].append(f"{type(e).__name__}: {e}"[:300])
            diags[ag]["status"] = "erro"
            brutos[ag] = []

    caminho_base = DIR_OUT / "base.csv"
    base = ler_base(caminho_base)
    agora = dt.datetime.now(FUSO).replace(microsecond=0).isoformat()
    validos: list[dict] = []
    for ag, regs in brutos.items():
        d = diags[ag]
        d["lidos"] += len(regs)
        vistos = set()
        for r in regs:
            if not r.get("url"):
                _desc(d, "sem_url"); continue
            if r["url"] in vistos:
                _desc(d, "duplicado_no_lote"); continue
            vistos.add(r["url"])
            if desde and r.get("data") and r["data"] < desde:
                _desc(d, "antes_de_desde"); continue
            validos.append(r)
            d["validos"] += 1
            chave = (r["agencia"], r["url"])
            if chave not in base:
                base[chave] = {**r, "coletado_em": agora}
                d["novos_na_base"] += 1
            else:  # atualiza campos vindos da fonte, mantem coletado_em
                base[chave] = {**base[chave], **{k: v for k, v in r.items() if v is not None}}
        if d["status"] == "nao_rodou":
            d["status"] = "ok" if regs else "vazio"

    # Arquivos por dia: todas as acoes publicadas no dia (sem data -> dia da coleta).
    por_dia: dict[str, list[dict]] = {}
    for r in validos:
        por_dia.setdefault(r.get("data") or hoje.isoformat(), []).append(r)
    for dia, regs in por_dia.items():
        arq = DIR_OUT / f"{dia}.json"
        existentes = {}
        if arq.exists():
            try:
                existentes = {(x["agencia"], x["url"]): x for x in json.loads(arq.read_text(encoding="utf-8"))["acoes"]}
            except (ValueError, KeyError):
                existentes = {}
        for r in regs:
            existentes[(r["agencia"], r["url"])] = {k: r.get(k) for k in CAMPOS}
        acoes = sorted(existentes.values(), key=lambda x: (x["agencia"], x["url"]))
        arq.write_text(json.dumps({"data": dia, "n": len(acoes), "acoes": acoes}, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    linhas_base = sorted(base.values(), key=lambda x: (x.get("data") or "", x["agencia"], x["url"]), reverse=True)
    gravar_csv(caminho_base, CAMPOS_BASE, linhas_base)
    uni_linhas = montar_universo_csv(base, universo, grupos)
    gravar_csv(DIR_OUT / "universo.csv", CAMPOS_UNIVERSO, uni_linhas)

    def conta(regs, campo):
        c: dict[str, int] = {}
        for r in regs:
            c[r.get(campo) or "null"] = c.get(r.get(campo) or "null", 0) + 1
        return dict(sorted(c.items()))

    saida = {
        "data_referencia": hoje.isoformat(),
        "gerado_em": agora,
        "desde": desde,
        "contagens": {
            "por_agencia": conta(validos, "agencia"),
            "por_acao": conta(validos, "acao"),
            "base_total": len(base),
            "base_por_agencia": conta(base.values(), "agencia"),
            "base_por_acao": conta(base.values(), "acao"),
            "universo_linhas": len(uni_linhas),
        },
        "diagnostico": {
            "universo_nomes": len(universo),
            "grupos": sum(len(v) for v in grupos.values()),
            "agencias": diags,
        },
        "acoes_casadas_no_run": [
            {**{k: r.get(k) for k in ("agencia", "data", "entidade", "acao", "nota_nova", "perspectiva", "url")},
             "casamentos": casar(r.get("entidade"), universo, grupos)}
            for r in validos if casar(r.get("entidade"), universo, grupos)
        ],
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    return saida


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inspecionar", action="store_true", help="descreve as fontes, nao grava")
    ap.add_argument("--desde", help="backfill a partir de AAAA-MM-DD (ate 12 meses)")
    a = ap.parse_args(argv)
    hoje = dt.datetime.now(FUSO).date()
    desde = None
    if a.desde:
        try:
            d = dt.date.fromisoformat(a.desde)
        except ValueError:
            ap.error("--desde deve ser AAAA-MM-DD")
        limite = hoje - dt.timedelta(days=MAX_BACKFILL_DIAS)
        if d < limite:
            log.warning("--desde %s alem de 12 meses; usando %s", d, limite)
            d = limite
        desde = d.isoformat()
    try:
        if a.inspecionar:
            texto = inspecionar(Cliente(), carregar_universo())
            print(texto)
            return 0
        saida = executar(desde, hoje)
        md = resumo_md(saida)
        print(md)
        escrever_summary(md)
        return 0
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        escrever_summary("## ratings_agencias: erro nao tratado\n\n```\n" + tb + "\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
