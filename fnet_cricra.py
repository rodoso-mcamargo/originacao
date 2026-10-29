# -*- coding: utf-8 -*-
"""Assembleias e documentos de CRI/CRA no Fundos.NET (B3), classificados por pauta.

Fonte: pesquisa publica do Fundos.NET, aba de certificados (securitizacao):
  pagina  https://fnet.bmfbovespa.com.br/fnet/publico/abrirGerenciadorDocumentosCVM
  JSON    https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados
          ?paginaCertificados=true&idCategoriaDocumento=..&dataInicial=dd/mm/aaaa&...
  doc     https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento?id=<id>
E o mesmo endpoint que a tabela da pagina chama (DataTables, GET). Sem login nem
captcha para a listagem; o captcha da pagina so aparece no "Anexo B", que nao usamos.

A listagem nao traz o assunto da assembleia (campo `assuntos` vem sempre nulo para
certificados). Para os documentos casados com o universo do painel (dados/cri_cra),
o script baixa o documento em memoria, extrai o texto (pypdf), recorta a "Ordem do
Dia" e classifica a pauta por regex. Nenhum PDF e gravado; so um trecho de ate 300
caracteres da pauta vai para o CSV.

Saidas em dados/fnet_cricra/:
  AAAA-MM-DD.json  documentos de CRI/CRA entregues naquele dia
  base.csv         base acumulada, chave id_documento
  universo.csv     documentos casados com dados/cri_cra (um por documento x codigo)
  ultimo.json      resumo da ultima execucao + diagnostico

Uso:
  python fnet_cricra.py                  # ultimos 5 dias corridos
  python fnet_cricra.py --backfill 270   # reconstroi 270 dias
  python fnet_cricra.py --inspecionar    # so descreve a fonte, nao grava
"""
from __future__ import annotations

import socket

_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse, csv, datetime as dt, io, json, logging, os, pathlib, re, sys, time, traceback
import unicodedata
from collections import Counter

import requests

log = logging.getLogger("fnet_cricra")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # PDFs com xref torto geram avisos inofensivos
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BASE = "https://fnet.bmfbovespa.com.br/fnet/publico/"
URL_PAGINA = BASE + "abrirGerenciadorDocumentosCVM"
URL_DADOS = BASE + "pesquisarGerenciadorDocumentosDados"
URL_DOC = BASE + "exibirDocumento?id={id}"
UA = "originacao-radar/1.0 (+https://github.com/rodoso-mcamargo/originacao)"
PAUSA = 1.1          # segundos entre requisicoes (<= 1 req/s)
POR_PAGINA = 200     # o servidor devolve vazio com l=1000; 200 funciona
TIMEOUT = 60
DIR_OUT = pathlib.Path("dados/fnet_cricra")
UNIVERSO = pathlib.Path("dados/cri_cra/ultimo.json")

# Categorias pedidas (ids da pagina publica). Informes mensais ficam de fora.
CATEGORIAS = {
    2: "Assembleia",
    46: "Assembleia especial de investidores",
    1: "Fato Relevante",
    3: "Comunicado ao Mercado",
    20: "Aviso aos Investidores",
    35: "Recuperação Judicial",
    34: "Recuperação Extrajudicial",
}
# Categorias cujo texto e baixado e classificado (quando casadas com o universo)
CATEGORIAS_TEXTO = {"assembleia", "assembleia especial de investidores", "fato relevante",
                    "comunicado ao mercado", "aviso aos investidores",
                    "recuperacao judicial", "recuperacao extrajudicial"}

# --------------------------------------------------------------------------------------
# Classificacao da pauta. Aplicada ao texto sem acento e em minusculas.
# credito tem precedencia sobre estrutural; sem match nos dois = sem_pauta.
# --------------------------------------------------------------------------------------
REGEX_CREDITO = [
    r"\bwaivers?\b",
    r"\brenuncia\b",
    r"\bperdao\b",
    r"\bnao (?:declaracao|decretacao)\b[^.;]{0,60}vencimento antecipado",
    r"\b(?:declaracao|decretacao)\b[^.;]{0,40}vencimento antecipado",
    r"\bevento[s]? de (?:vencimento antecipado|inadimplemento)",
    r"\bcovenants?\b",
    r"\bindices? financeiros?\b",
    r"\b(?:alterac|prorrogac|modificac|postergac)\w*\b[^.;]{0,80}"
    r"\b(?:cronograma|prazo|data de vencimento|datas de vencimento|remuneracao|juros remuneratorios|"
    r"taxa de juros|spread|carencia|datas? de pagamento)",
    r"\brepactuac",
    r"\binadimpl",
    r"\brecuperacao (?:judicial|extrajudicial)\b",
]
REGEX_ESTRUTURAL = [
    r"\bsubstituic\w*\b[^.;]{0,60}\b(?:devedor|devedora|garantia|garantias|agente fiduciario|"
    r"securitizadora|emissora|cedente|fiador|fiadora)",
    r"\b(?:liberac|reforc|exclus|inclus|compartilhament|constituic)\w*\b[^.;]{0,40}\bgarantias?\b",
    r"\bresgate\b",
    r"\bamortizac\w* extraordinaria",
    r"\b(?:mudanca|alteracao|transferencia|cessao|alienacao) (?:do |de |indireta do |direta do )?controle",
]
RE_CREDITO = [re.compile(p) for p in REGEX_CREDITO]
RE_ESTRUTURAL = [re.compile(p) for p in REGEX_ESTRUTURAL]

RE_ORDEM_DIA = re.compile(r"ordem do dia|materias? (?:a serem )?deliberad|deliberar sobre", re.I)
RE_ABRE_LISTA = re.compile(r"^[\s\"”“)]*(?::|\.|-|–)?\s*(?:\(?(?:i|1|a)\)|deliberar|examinar|"
                           r"as seguintes|sobre as seguintes|sobre:)")
RE_FIM_ORDEM = re.compile(
    r"\b(?:instalacao|quorum|deliberacoes|presenca|informacoes gerais|"
    r"local(?:,| e) (?:data|hora)|participacao (?:na|da) assembleia|encerramento)\b")

# descricaoFundo: "OPEA CRI Emissão:376 Série(s):1 (+2) Diversificado 12/2024 BRRBRACRIS10"
RE_DESC = re.compile(
    r"^(?P<sec>.+?)\s+(?P<tipo>CRI|CRA|CR|DEB|OTS|SC|NC|CPR)\s+Emiss[aã]o:\s*(?P<emissao>\S+)\s+"
    r"S[eé]rie(?:\(s\))?:\s*(?P<serie>\S+)(?:\s+\(\+(?P<extra>\d+)\))?\s+(?P<nome>.*?)\s*"
    r"(?:(?P<mes>\d{2}/\d{4})\s+)?(?P<cod>\S+)?\s*$")
RE_ISIN = re.compile(r"^BR[A-Z0-9]{9}\d$")

GENERICAS = {"ENERGIA", "SANEAMENTO", "TRANSMISSORA", "DIVERSIFICADO", "PULVERIZADO", "AGRO",
             "AGRONEGOCIO", "IMOBILIARIO", "IMOBILIARIA", "EMPREENDIMENTOS", "PARTICIPACOES",
             "HOLDING", "INCORPORADORA", "CONSTRUTORA", "SHOPPING", "LOGISTICA", "SECURITIZADORA",
             "GRUPO", "BRASIL", "SPE", "INVESTIMENTOS", "CREDITO", "LOTEAMENTO", "LOTEAMENTOS"}
SUFIXOS = ["EM RECUPERACAO JUDICIAL", "PARTICIPACOES", "COMPANHIA", "LTDA", "CIA", "S A", "SA",
           "S/A", "EIRELI", "ME"]

# chave da securitizadora: primeira palavra significativa, com apelidos do Fundos.NET
APELIDOS_SEC = {
    "RBCAPITALRES": "RB CAPITAL", "RBCAPITAL": "RB CAPITAL", "RB": "RB CAPITAL",
    "CIAVERT": "VERT", "CIAVERT5": "VERT", "VERTCIASEC": "VERT", "CIAVERTPROVI": "PROVINCIA", "GAIA": "OPEA",
    "ISEC": "ISEC", "ECO": "ECO", "TRUE": "TRUE", "TRUESEC": "TRUE",
}
PALAVRAS_SEC_IGNORAR = {"COMPANHIA", "CIA", "DE", "DA", "DO"}


# --------------------------------------------------------------------------------------
# normalizacao
# --------------------------------------------------------------------------------------
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_texto(s: str) -> str:
    return re.sub(r"\s+", " ", sem_acento(s or "").lower()).strip()


def norm_nome(s: str | None) -> str:
    """Nome de empresa: sem acento, maiusculas, sem pontuacao, sem sufixos societarios."""
    if not s:
        return ""
    t = sem_acento(s).upper()
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    mudou = True
    while mudou and t:
        mudou = False
        for suf in SUFIXOS:
            s2 = suf.replace("/", " ")
            if t.endswith(" " + s2) or t == s2:
                t = t[: -len(s2)].strip()
                mudou = True
    t = re.sub(r"^(?:CIA|COMPANHIA) ", "", t)
    return t


def chave_sec(nome: str | None) -> str:
    t = norm_nome(nome)
    for p in t.split():
        if p in PALAVRAS_SEC_IGNORAR:
            continue
        if p == "RB" and "CAPITAL" in t:
            return "RB CAPITAL"
        return APELIDOS_SEC.get(p, p)
    return ""


def num_serie(s) -> int | None:
    m = re.search(r"\d+", str(s or ""))
    return int(m.group()) if m else None


def data_iso(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", s.strip())
    if not m:
        return None
    d, mo, a, h, mi = m.groups()
    return f"{a}-{mo}-{d}" + (f"T{h}:{mi}" if h else "")


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------
def parse_descricao(desc: str) -> dict:
    desc = re.sub(r"\s+", " ", (desc or "").strip())
    m = RE_DESC.match(desc)
    if not m:
        return {"securitizadora": None, "tipo_titulo": None, "emissao": None, "serie": None,
                "series_extra": None, "devedor_fnet": None, "mes_emissao": None, "codigo_fnet": None}
    g = m.groupdict()
    return {
        "securitizadora": g["sec"].strip(),
        "tipo_titulo": g["tipo"],
        "emissao": g["emissao"],
        "serie": g["serie"],
        "series_extra": int(g["extra"]) if g["extra"] else 0,
        "devedor_fnet": (g["nome"] or "").strip() or None,
        "mes_emissao": (g["mes"][3:] + "-" + g["mes"][:2]) if g["mes"] else None,
        "codigo_fnet": g["cod"],
    }


def status_txt(st: str | None) -> str | None:
    if not st:
        return None
    return {"C": "cancelado", "A": "ativo", "I": "inativo"}.get(st[:1].upper(), st)


def parse_registro(r: dict) -> dict:
    d = parse_descricao(r.get("descricaoFundo", ""))
    cod = d.pop("codigo_fnet")
    return {
        "id_documento": r.get("id"),
        "data_entrega": data_iso(r.get("dataEntrega")),
        "data_referencia": data_iso(r.get("dataReferencia")),
        **d,
        "isin": cod if cod and RE_ISIN.match(cod) else None,
        "codigo": cod if cod and not RE_ISIN.match(cod) else None,
        "descricao_fnet": r.get("descricaoFundo"),
        "categoria": (r.get("categoriaDocumento") or "").strip() or None,
        "tipo": (r.get("tipoDocumento") or "").strip() or None,
        "especie": (r.get("especieDocumento") or "").strip() or None,
        "assunto": None,
        "status": status_txt(r.get("status")),
        "versao": r.get("versao"),
        "url": URL_DOC.format(id=r.get("id")),
    }


# --------------------------------------------------------------------------------------
# classificacao
# --------------------------------------------------------------------------------------
RE_CODIGO_B3 = re.compile(r"\b(?:\d{2}[A-L]\d{7}|CRA0\d{2}[0-9A-Z]{5})\b")


def codigos_citados(texto: str | None) -> str | None:
    """Codigos B3 de CRI/CRA citados no texto (ate 30), separados por ';'."""
    vistos = []
    for c in RE_CODIGO_B3.findall((texto or "").upper()):
        if c not in vistos:
            vistos.append(c)
    return ";".join(vistos[:30]) or None


def recorta_pauta(texto: str) -> str | None:
    """Recorta a ordem do dia de um edital/ata. None se nao achar."""
    t = re.sub(r"\s+", " ", texto or "")
    achados = list(RE_ORDEM_DIA.finditer(t))
    if not achados:
        return None
    # prefere a ocorrencia que abre a lista de materias ("Ordem do Dia: (i) ...");
    # a primeira costuma ser so a mencao no preambulo ("a ordem do dia da assembleia ...")
    m = next((a for a in achados if RE_ABRE_LISTA.match(norm_texto(t[a.end(): a.end() + 80]))),
             achados[0])
    resto = t[m.start(): m.start() + 6000]
    fim = RE_FIM_ORDEM.search(norm_texto(resto), 40)
    return resto[: fim.start()] if fim else resto[:3000]


def classifica(texto: str | None) -> tuple[str, list[str]]:
    t = norm_texto(texto or "")
    if not t:
        return "sem_pauta", []
    achou_c = [p.pattern for p in RE_CREDITO if p.search(t)]
    if achou_c:
        return "credito", achou_c
    achou_e = [p.pattern for p in RE_ESTRUTURAL if p.search(t)]
    if achou_e:
        return "estrutural", achou_e
    return "sem_pauta", []


def trecho(s: str | None, n: int = 300) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def texto_pdf(conteudo: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(conteudo))
    partes = []
    for p in r.pages[:15]:
        try:
            partes.append(p.extract_text() or "")
        except Exception:  # pagina com fonte quebrada
            partes.append("")
    return " ".join(partes)


# --------------------------------------------------------------------------------------
# universo e casamento
# --------------------------------------------------------------------------------------
def carrega_universo(caminho: pathlib.Path = UNIVERSO) -> list[dict]:
    if not caminho.exists():
        return []
    d = json.loads(caminho.read_text(encoding="utf-8"))
    por_cod: dict[str, dict] = {}
    for a in d.get("ativos", []):
        c = a.get("codigo")
        if not c:
            continue
        if c not in por_cod or (a.get("referencia") or "") > (por_cod[c].get("referencia") or ""):
            por_cod[c] = a
    out = []
    for a in por_cod.values():
        out.append({
            "codigo": a["codigo"], "isin": a.get("isin"), "tipo": a.get("tipo"),
            "devedor": a.get("emissor"), "securitizadora": a.get("securitizadora"),
            "serie": a.get("serie"), "emissao": a.get("emissao"),
            "_sec": chave_sec(a.get("securitizadora")), "_serie": num_serie(a.get("serie")),
            "_nome": norm_nome(a.get("emissor")),
        })
    return out


def mes_do_codigo(codigo: str | None) -> tuple[int | None, int | None]:
    """(ano, mes) embutidos no codigo B3/CETIP. CRI novo: AAL... (L = mes A..L);
    CRA: CRA0AA... (so o ano). Devolve (None, None) se o padrao nao bate."""
    c = (codigo or "").upper()
    m = re.match(r"^(\d\d)([A-L])\d{7}$", c)
    if m:
        return 2000 + int(m.group(1)), "ABCDEFGHIJKL".index(m.group(2)) + 1
    m = re.match(r"^CRA0(\d\d)", c)
    if m:
        return 2000 + int(m.group(1)), None
    return None, None


def data_compativel(mes_emissao: str | None, codigo: str | None) -> bool:
    """O mes/ano que o Fundos.NET mostra (emissao) bate com o embutido no codigo?
    Sem informacao de um dos lados: True (nao filtra). CRI: mesmo mes, tolerancia 1.
    CRA: mesmo ano (ou janeiro do ano seguinte)."""
    ano, mes = mes_do_codigo(codigo)
    if not mes_emissao or ano is None:
        return True
    a, m = int(mes_emissao[:4]), int(mes_emissao[5:7])
    if mes is None:
        return a == ano or (a == ano + 1 and m == 1)
    return abs((a * 12 + m) - (ano * 12 + mes)) <= 1


def casa_nome(nome_fnet: str, nome_uni: str) -> str | None:
    """exato | prefixo | fuzzy | None. O Fundos.NET corta o nome em ~20 caracteres."""
    a, b = norm_nome(nome_fnet), nome_uni
    if not a or not b:
        return None
    pal = a.split()
    if all(p in GENERICAS for p in pal) or len(a.replace(" ", "")) < 4:
        return None
    if a == b:
        return "exato"
    if len(a) >= 8 and b.startswith(a) and not all(p in GENERICAS for p in pal[:1]):
        return "prefixo"
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None
    # token_set_ratio da 100 quando um nome contem o outro; exige uma palavra
    # significativa (nao generica, >= 4 letras) em comum.
    comuns = {p for p in set(a.split()) & set(b.split()) if p not in GENERICAS and len(p) >= 4}
    if comuns and fuzz.token_set_ratio(a, b) >= 92:
        return "fuzzy"
    return None


def casa(doc: dict, universo: list[dict]) -> list[dict]:
    """Devolve [{ativo, match, match_nome}] para um documento."""
    if doc.get("tipo_titulo") not in ("CRI", "CRA"):
        return []
    if doc.get("isin"):
        hits = [u for u in universo if u.get("isin") and u["isin"] == doc["isin"]]
        if hits:
            return [{"ativo": u, "match": "isin", "match_nome": None} for u in hits]
    if doc.get("codigo"):
        hits = [u for u in universo if u["codigo"] == doc["codigo"]]
        if hits:
            return [{"ativo": u, "match": "codigo", "match_nome": None} for u in hits]
    # codigos B3 citados no proprio documento (ex.: "IF CRA022008NB") resolvem a ambiguidade
    citados = [c for c in (doc.get("codigos_no_texto") or "").split(";") if c]
    if citados:
        hits = [u for u in universo if u["codigo"] in citados]
        if hits:
            return [{"ativo": u, "match": "codigo", "match_nome": None} for u in hits]
    sec = chave_sec(doc.get("securitizadora"))
    s0 = num_serie(doc.get("serie"))
    if not sec or s0 is None:
        return []
    series = set(range(s0, s0 + 1 + int(doc.get("series_extra") or 0)))
    out = []
    for u in universo:
        if u["_sec"] != sec or u["_serie"] not in series or u.get("tipo") not in (None, doc["tipo_titulo"]):
            continue
        if u.get("emissao") and doc.get("emissao"):
            if str(u["emissao"]) == str(doc["emissao"]):
                out.append({"ativo": u, "match": "emissao_serie", "match_nome": None})
            continue
        if not data_compativel(doc.get("mes_emissao"), u["codigo"]):
            continue
        how = casa_nome(doc.get("devedor_fnet") or "", u["_nome"])
        if how:
            out.append({"ativo": u, "match": "serie_devedor", "match_nome": how})
    return out


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class Cliente:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        self._ult = 0.0
        self.erros: Counter = Counter()
        self.reqs = 0

    def get(self, url, **kw):
        espera = PAUSA - (time.monotonic() - self._ult)
        if espera > 0:
            time.sleep(espera)
        for tent in range(3):
            try:
                self.reqs += 1
                r = self.s.get(url, timeout=TIMEOUT, **kw)
                self._ult = time.monotonic()
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except requests.RequestException as e:
                self._ult = time.monotonic()
                log.warning("falha %s (%s), tentativa %d", url[:90], e, tent + 1)
                time.sleep(5 * (tent + 1))
        raise RuntimeError(f"sem resposta: {url[:120]}")

    def abre(self):
        self.get(URL_PAGINA, params={"paginaCertificados": "true"})

    def pagina(self, cat: int, ini: dt.date, fim: dt.date, s: int) -> dict:
        p = {
            "d": 1, "s": s, "l": POR_PAGINA, "o[0][dataEntrega]": "desc",
            "paginaCertificados": "true", "idCategoriaDocumento": cat,
            "dataInicial": ini.strftime("%d/%m/%Y"), "dataFinal": fim.strftime("%d/%m/%Y"),
        }
        r = self.get(URL_DADOS, params=p, headers={"X-Requested-With": "XMLHttpRequest",
                                                     "Accept": "application/json"})
        r.raise_for_status()
        if not r.content.strip():
            raise RuntimeError("resposta vazia do Fundos.NET")
        return r.json()

    def lista(self, cat: int, ini: dt.date, fim: dt.date) -> list[dict]:
        out, s = [], 0
        while True:
            j = self.pagina(cat, ini, fim, s)
            dados = j.get("data") or []
            out += dados
            total = j.get("recordsFiltered") or 0
            s += POR_PAGINA
            if not dados or s >= total:
                return out

    def documento(self, id_doc) -> bytes:
        r = self.get(URL_DOC.format(id=id_doc) + "&cvm=true")
        r.raise_for_status()
        return r.content


def janelas(ini: dt.date, fim: dt.date, dias: int = 15):
    a = ini
    while a <= fim:
        b = min(fim, a + dt.timedelta(days=dias - 1))
        yield a, b
        a = b + dt.timedelta(days=1)


# --------------------------------------------------------------------------------------
# persistencia
# --------------------------------------------------------------------------------------
COLS_BASE = ["id_documento", "data_entrega", "data_referencia", "securitizadora", "tipo_titulo",
             "emissao", "serie", "series_extra", "devedor_fnet", "mes_emissao", "isin", "codigo", "categoria",
             "tipo", "especie", "assunto", "status", "versao", "url", "match", "codigos_universo",
             "classe", "texto", "codigos_no_texto", "descricao_fnet"]
COLS_UNI = ["data_entrega", "codigo_universo", "tipo_titulo", "devedor", "securitizadora",
            "emissao", "serie", "categoria", "tipo", "especie", "classe", "match", "match_nome",
            "ambiguo", "assunto", "status", "url", "id_documento"]


def le_csv(caminho: pathlib.Path) -> list[dict]:
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def grava_csv(caminho: pathlib.Path, linhas: list[dict], cols: list[str]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for l in linhas:
            w.writerow({c: ("" if l.get(c) is None else l.get(c)) for c in cols})


def grava_dia(caminho: pathlib.Path, dia: str, docs: list[dict]):
    """JSON compacto, um documento por linha (diff legivel, arquivo pequeno)."""
    linhas = [json.dumps({k: d.get(k) for k in COLS_BASE if k != "descricao_fnet"}, ensure_ascii=False)
              for d in docs]
    caminho.write_text(
        '{"data": "%s", "fonte": "%s", "documentos": [\n%s\n]}\n' % (dia, URL_DADOS, ",\n".join(linhas)),
        encoding="utf-8")


def _vazio(v):
    return v in (None, "")


# --------------------------------------------------------------------------------------
# execucao
# --------------------------------------------------------------------------------------
def coleta(cli: Cliente, ini: dt.date, fim: dt.date, diag: dict) -> list[dict]:
    regs = []
    for cat, nome in CATEGORIAS.items():
        for a, b in janelas(ini, fim):
            try:
                brutos = cli.lista(cat, a, b)
            except Exception as e:  # registra e segue com as outras categorias
                diag["erros"][f"lista cat {cat} {a}..{b}"] = str(e)[:200]
                continue
            diag["lidos"] += len(brutos)
            for r in brutos:
                regs.append(parse_registro(r))
            log.info("cat %s (%s) %s..%s: %d", cat, nome, a, b, len(brutos))
    vistos, out = set(), []
    for r in regs:
        if r["id_documento"] in vistos:
            diag["descartados"]["duplicado"] += 1
            continue
        vistos.add(r["id_documento"])
        if r["tipo_titulo"] not in ("CRI", "CRA"):
            diag["descartados"][f"nao_cri_cra ({r['tipo_titulo'] or 'sem emissao: documento da propria securitizadora'})"] += 1
            continue
        out.append(r)
    return out


def enriquece(cli: Cliente, docs: list[dict], universo: list[dict], antigos: dict, diag: dict,
              max_textos: int | None = None):
    baixados = 0
    for d in docs:
        hits = casa(d, universo)
        d["_hits"] = hits
        d["match"] = hits[0]["match"] if hits else "nenhum"
        d["codigos_universo"] = ";".join(sorted({h["ativo"]["codigo"] for h in hits})) or None
        velho = antigos.get(str(d["id_documento"]))
        if (velho and not _vazio(velho.get("texto")) and velho.get("texto") != "erro"
                and not os.environ.get("FNET_RECLASSIFICAR")):
            d["assunto"] = velho.get("assunto") or None
            d["classe"] = velho.get("classe") or None
            d["texto"] = velho.get("texto")
            d["codigos_no_texto"] = velho.get("codigos_no_texto") or None
            _recasa(d, universo)
            continue
        if not hits or norm_texto(d.get("categoria") or "") not in CATEGORIAS_TEXTO:
            d["texto"] = None
            d["classe"] = None
            continue
        if max_textos is not None and baixados >= max_textos:
            d["texto"] = None
            d["classe"] = None
            diag["descartados"]["texto_nao_baixado_limite"] += 1
            continue
        try:
            # cache opcional de texto extraido, fora do repositorio (so para depuracao local)
            cache = os.environ.get("FNET_CACHE_TEXTOS")
            arq = pathlib.Path(cache) / f"{d['id_documento']}.txt" if cache else None
            if arq and arq.exists():
                txt = arq.read_text(encoding="utf-8")
            else:
                conteudo = cli.documento(d["id_documento"])
                baixados += 1
                if conteudo[:4] == b"%PDF":
                    txt = texto_pdf(conteudo)
                else:
                    from bs4 import BeautifulSoup
                    txt = BeautifulSoup(conteudo, "html.parser").get_text(" ")
                if arq:
                    arq.parent.mkdir(parents=True, exist_ok=True)
                    arq.write_text(txt, encoding="utf-8")
            d["codigos_no_texto"] = codigos_citados(txt)
            _recasa(d, universo)
            assembleia = norm_texto(d.get("categoria") or "").startswith("assembleia")
            pauta = recorta_pauta(txt) if assembleia else None
            if not txt.strip():
                d["texto"] = "sem_texto"
                d["classe"], _ = classifica(None)
            else:
                alvo = pauta if pauta else txt[:4000]
                d["texto"] = "pauta" if pauta else "inicio"
                d["classe"], _ = classifica(alvo)
                d["assunto"] = trecho(alvo)
        except Exception as e:
            d["texto"] = "erro"
            d["classe"] = None
            diag["erros"][f"doc {d['id_documento']}"] = str(e)[:200]
    diag["textos_baixados"] = baixados


def _recasa(d: dict, universo: list[dict]):
    hits = casa({**d, "series_extra": int(d.get("series_extra") or 0)}, universo)
    d["_hits"] = hits
    d["match"] = hits[0]["match"] if hits else "nenhum"
    d["codigos_universo"] = ";".join(sorted({h["ativo"]["codigo"] for h in hits})) or None


def mescla(antigos: dict, novos: list[dict]) -> list[dict]:
    m = dict(antigos)
    for d in novos:
        m[str(d["id_documento"])] = {k: v for k, v in d.items() if not k.startswith("_")}
    return sorted(m.values(), key=lambda r: (r.get("data_entrega") or "", str(r.get("id_documento"))),
                  reverse=True)


def linhas_universo(base: list[dict], universo: list[dict]) -> list[dict]:
    por_cod = {u["codigo"]: u for u in universo}
    # Emissoes ligadas a cada codigo. As confirmadas por codigo (no texto ou na descricao)
    # mandam; sem confirmacao, um codigo ligado a mais de uma emissao e ambiguo.
    emissoes: dict[str, set] = {}
    confirmadas: dict[str, set] = {}
    for d in base:
        chave = (d.get("securitizadora"), d.get("emissao"))
        for c in (d.get("codigos_universo") or "").split(";"):
            if c:
                emissoes.setdefault(c, set()).add(chave)
                if d.get("match") in ("isin", "codigo"):
                    confirmadas.setdefault(c, set()).add(chave)

    def ambiguo(c: str, d: dict) -> bool:
        if c in confirmadas:
            return (d.get("securitizadora"), d.get("emissao")) not in confirmadas[c] or len(confirmadas[c]) > 1
        return len(emissoes.get(c, ())) > 1
    out = []
    for d in base:
        for c in (d.get("codigos_universo") or "").split(";"):
            if not c:
                continue
            u = por_cod.get(c, {})
            # o casamento por nome guarda o tipo (exato/prefixo/fuzzy); recalcula aqui
            mn = None
            if d.get("match") == "serie_devedor" and u:
                mn = casa_nome(d.get("devedor_fnet") or "", u["_nome"])
            out.append({**d, "codigo_universo": c, "devedor": u.get("devedor"),
                        "securitizadora": d.get("securitizadora"), "match_nome": mn,
                        "ambiguo": ambiguo(c, d)})
    return out


def roda(args) -> int:
    hoje = dt.date.today()
    fim = dt.date.fromisoformat(args.data) if args.data else hoje
    dias = args.backfill or 5
    ini = fim - dt.timedelta(days=dias - 1)
    diag = {"lidos": 0, "gravados": 0, "descartados": Counter(), "erros": {}, "requisicoes": 0}
    cli = Cliente()
    cli.abre()
    universo = carrega_universo()
    if not universo:
        diag["erros"]["universo"] = f"{UNIVERSO} ausente ou vazio: nada casa"
    docs = coleta(cli, ini, fim, diag)

    base_csv = DIR_OUT / "base.csv"
    antigos = {r["id_documento"]: r for r in le_csv(base_csv)}
    enriquece(cli, docs, universo, antigos, diag, args.max_textos)
    base = mescla(antigos, docs)
    # reaplica o casamento em toda a base (o universo muda todo dia)
    for d in base:
        _recasa(d, universo)
        d.pop("_hits", None)
    uni = linhas_universo(base, universo)
    diag["requisicoes"] = cli.reqs

    DIR_OUT.mkdir(parents=True, exist_ok=True)
    por_dia: dict[str, list] = {}
    for d in docs:
        por_dia.setdefault((d.get("data_entrega") or "")[:10], []).append(
            {k: v for k, v in d.items() if not k.startswith("_")})
    for dia, lst in sorted(por_dia.items()):
        if not dia:
            continue
        lst.sort(key=lambda r: (r.get("data_entrega") or "", r["id_documento"]), reverse=True)
        grava_dia(DIR_OUT / f"{dia}.json", dia, lst)
    grava_csv(base_csv, base, COLS_BASE)
    grava_csv(DIR_OUT / "universo.csv", uni, COLS_UNI)
    diag["gravados"] = len(docs)
    resumo = {
        "fonte": "Fundos.NET (B3) - documentos de CRI/CRA",
        "url": URL_PAGINA + "?paginaCertificados=true",
        "data_referencia": fim.isoformat(),
        "janela": [ini.isoformat(), fim.isoformat()],
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "documentos_janela": len(docs),
        "documentos_base": len(base),
        "linhas_universo": len(uni),
        "por_classe_universo": dict(Counter(l.get("classe") or "nao_classificado" for l in uni)),
        "por_match_base": dict(Counter(d.get("match") for d in base)),
        "por_categoria_janela": dict(Counter(d.get("categoria") for d in docs)),
        "diagnostico": {**diag, "descartados": dict(diag["descartados"])},
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("janela %s..%s: %d docs CRI/CRA, base %d, universo %d linhas", ini, fim, len(docs),
             len(base), len(uni))
    resumo_md(resumo, uni)
    return 0


def resumo_md(resumo: dict, uni: list[dict]):
    linhas = [f"## Fundos.NET CRI/CRA {resumo['janela'][0]} a {resumo['janela'][1]}", "",
              f"- Documentos CRI/CRA na janela: **{resumo['documentos_janela']}**",
              f"- Base acumulada: **{resumo['documentos_base']}**",
              f"- Linhas em universo.csv: **{resumo['linhas_universo']}**",
              f"- Por classe (universo): `{resumo['por_classe_universo']}`",
              f"- Por match (base): `{resumo['por_match_base']}`",
              f"- Diagnostico: `{json.dumps(resumo['diagnostico'], ensure_ascii=False)[:1500]}`", ""]
    if uni:
        linhas += ["| data | codigo | devedor | especie | classe | match |", "|---|---|---|---|---|---|"]
        for l in uni[:10]:
            linhas.append(f"| {l.get('data_entrega')} | {l.get('codigo_universo')} | {l.get('devedor')} | "
                          f"{l.get('especie')} | {l.get('classe')} | {l.get('match')} |")
    _summary("\n".join(linhas) + "\n")


def inspeciona(args) -> int:
    cli = Cliente()
    r0 = cli.get(URL_PAGINA, params={"paginaCertificados": "true"})
    fim = dt.date.fromisoformat(args.data) if args.data else dt.date.today()
    ini = fim - dt.timedelta(days=6)
    out = [f"## Inspecao Fundos.NET (certificados) {ini}..{fim}", "",
           f"- Pagina: HTTP {r0.status_code}, {len(r0.content)} bytes"]
    exemplo = None
    for cat, nome in CATEGORIAS.items():
        try:
            j = cli.pagina(cat, ini, fim, 0)
        except Exception as e:
            out.append(f"- categoria {cat} ({nome}): ERRO {e}")
            continue
        dados = j.get("data") or []
        cats = Counter(d.get("categoriaDocumento") for d in dados)
        tipos = Counter(parse_descricao(d.get("descricaoFundo", ""))["tipo_titulo"] for d in dados)
        out.append(f"- categoria {cat} ({nome}): recordsFiltered={j.get('recordsFiltered')}, "
                   f"pagina={len(dados)}, categorias={dict(cats)}, titulos={dict(tipos)}")
        if dados and exemplo is None:
            exemplo = dados
    if exemplo:
        out += ["", f"Colunas reais: `{sorted(exemplo[0].keys())}`", "", "3 registros:", "```"]
        for d in exemplo[:3]:
            out.append(json.dumps({k: d[k] for k in ("id", "descricaoFundo", "categoriaDocumento",
                                                      "tipoDocumento", "especieDocumento", "dataEntrega",
                                                      "dataReferencia", "status", "versao")},
                                  ensure_ascii=False))
        out += ["```", "", "Parse:", "```"]
        for d in exemplo[:3]:
            out.append(json.dumps(parse_registro(d), ensure_ascii=False))
        out.append("```")
    uni = carrega_universo()
    out.append(f"\nUniverso dados/cri_cra: {len(uni)} codigos; securitizadoras "
               f"{dict(Counter(u['_sec'] for u in uni))}")
    _summary("\n".join(out) + "\n")
    return 0


def _summary(txt: str):
    print(txt)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(txt + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inspecionar", action="store_true")
    ap.add_argument("--backfill", type=int, help="dias corridos a reconstruir")
    ap.add_argument("--data", help="data final AAAA-MM-DD (padrao: hoje)")
    ap.add_argument("--max-textos", type=int, default=None,
                    help="limite de documentos baixados para classificar (padrao: sem limite)")
    args = ap.parse_args(argv)
    try:
        return inspeciona(args) if args.inspecionar else roda(args)
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        _summary(f"## fnet_cricra: erro nao tratado\n\n```\n{tb[-3000:]}\n```\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
