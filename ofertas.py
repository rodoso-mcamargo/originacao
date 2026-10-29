# -*- coding: utf-8 -*-
"""Coletor das ofertas publicas (Res. CVM 160) de debentures, CRI e CRA, e comparacao
da taxa de primario com o secundario do mesmo emissor.

Fonte: dados abertos da CVM, https://dados.cvm.gov.br/dataset/oferta-distrib
  zip: https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip
  arquivo usado: oferta_resolucao_160.csv (chave Numero_Requerimento).
  O arquivo da Res. 160 NAO traz serie, vencimento, indexador nem remuneracao: esses campos
  vem do SND (dados/snd/caracteristicas.csv), casando por CNPJ + numero da emissao.

Saidas (dados/ofertas/):
  base.csv                    todas as ofertas vistas (chave numero_requerimento)
  AAAA-MM-DD.csv              ofertas novas ou alteradas no dia
  primario_vs_secundario.csv  debentures: taxa de emissao (SND) vs taxa indicativa ANBIMA
                              do papel do mesmo emissor e indexador com duration mais proxima
  ultimo.json                 resumo + diagnostico
"""
from __future__ import annotations

import socket
_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse, csv, datetime as dt, hashlib, io, json, os, pathlib, re, sys, time, traceback
import unicodedata, zipfile

import requests

UA = "originacao-radar/1.0 (+https://github.com/rodoso-mcamargo/originacao)"
URL_ZIP = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"
ARQ_160 = "oferta_resolucao_160.csv"
TIMEOUT = 300
RAIZ = pathlib.Path(__file__).resolve().parent
DIR_OUT = RAIZ / "dados" / "ofertas"
SND_CARAC = RAIZ / "dados" / "snd" / "caracteristicas.csv"
UNIVERSO = RAIZ / "dados" / "ultimo.json"
UNIVERSO_CRI = RAIZ / "dados" / "cri_cra" / "ultimo.json"
DIR_SERIE = RAIZ / "dados" / "serie"
CACHE = pathlib.Path(os.environ.get("ORIGINACAO_CACHE", pathlib.Path.home() / ".cache" / "originacao"))
JANELA_MESES = 24
DIAS_TAXA_ATRAS = 7  # tolerancia para achar a taxa indicativa no pregao anterior

TIPOS = {  # Valor_Mobiliario (sem acento, minusculo) -> tipo
    "debentures": "DEBENTURE",
    "certificados de recebiveis imobiliarios": "CRI",
    "certificados de recebiveis do agronegocio": "CRA",
}

COLS_OFERTA = ["numero_requerimento", "numero_processo", "tipo", "valor_mobiliario", "status",
               "rito", "tipo_requerimento", "tipo_oferta", "data_requerimento", "data_registro",
               "data_encerramento", "cnpj_emissor", "nome_emissor", "cnpj_lider", "nome_lider",
               "emissao", "quantidade", "volume", "titulo_incentivado", "publico_alvo",
               "sustentavel", "regime_distribuicao", "devedores", "indexador", "remuneracao",
               "no_universo", "casamento", "nome_universo", "hash"]
COLS_PVS = ["numero_requerimento", "data_oferta", "cnpj_emissor", "emissor", "emissao",
            "codigo_primario", "familia", "remuneracao_primario", "taxa_primario", "vencimento",
            "duration_ref_anos", "duration_ref_fonte", "codigo_secundario",
            "duration_secundario_anos", "taxa_secundario", "data_taxa_secundario",
            "diferenca_bps", "diferenca_pct_cdi", "observacao"]

GENERICAS = {"ENERGIA", "SANEAMENTO", "TRANSMISSORA", "TRANSMISSAO", "GERACAO", "ENERGETICA",
             "DISTRIBUIDORA", "DISTRIBUICAO", "ELETRICA", "ELETRICIDADE", "HOLDING", "BRASIL",
             "BRASILEIRA", "DO", "DE", "DA", "DOS", "DAS", "E", "EM", "INVESTIMENTOS",
             "EMPREENDIMENTOS", "CONCESSIONARIA", "CONCESSOES", "RODOVIAS", "RODOVIA",
             "SERVICOS", "INDUSTRIA", "COMERCIO", "AGRO", "AGRICOLA", "AGROPECUARIA",
             "IMOBILIARIA", "IMOBILIARIOS", "SECURITIZADORA", "LOGISTICA", "SOLAR", "EOLICA",
             "RENOVAVEIS", "INFRAESTRUTURA", "GRUPO", "NACIONAL", "BANCO", "FINANCEIRA",
             "AMBIENTAL", "AGUAS", "SPE"}
ROMANO = re.compile(r"^(?=[IVXL]+$)L?X{0,3}(IX|IV|V?I{0,3})$")
SUFIXOS = ["EM RECUPERACAO JUDICIAL", "PARTICIPACOES", "COMPANHIA", "LTDA", "CIA", "S A", "SA"]

sessao = requests.Session()
sessao.headers.update({"User-Agent": UA})


# ----------------------------------------------------------------- normalizacao (regra 11)
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def normalizar(nome: str) -> str:
    s = sem_acento(nome).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    mudou = True
    while mudou:
        mudou = False
        for suf in SUFIXOS:
            for padrao in (rf"(^| ){suf}$", rf"^{suf} "):
                novo = re.sub(padrao, " ", s).strip()
                if novo != s:
                    s, mudou = novo, True
    return re.sub(r"\s+", " ", s).strip()


def especificos(norm: str) -> set[str]:
    """Palavras que distinguem a empresa (numeral romano/digito conta: SPE I != SPE II)."""
    return {t for t in norm.split()
            if t not in GENERICAS and (len(t) >= 3 or ROMANO.match(t) or t.isdigit())}


class Casador:
    """Casa nomes com o universo: exato primeiro, depois fuzzy (token_set_ratio >= 92)."""

    def __init__(self, nomes: list[str]):
        self.por_norm = {}
        for n in nomes:
            k = normalizar(n)
            if k and especificos(k):
                self.por_norm.setdefault(k, n)

    def casar(self, nome: str):
        k = normalizar(nome)
        if not k or not especificos(k):
            return None, None  # nunca casar por palavra generica sozinha
        if k in self.por_norm:
            return self.por_norm[k], "exato"
        from rapidfuzz import fuzz, process
        r = process.extractOne(k, list(self.por_norm), scorer=fuzz.token_set_ratio, score_cutoff=92)
        if r:
            cand = r[0]
            a, b = especificos(k), especificos(cand)
            menor, maior = (a, b) if len(a) <= len(b) else (b, a)
            # fuzzy so com >= 2 palavras especificas em comum e no maximo 1 sobrando
            sobra = maior - menor
            if (len(menor) >= 2 and menor <= maior and len(sobra) <= 1
                    and not any(ROMANO.match(t) or t.isdigit() for t in sobra)):
                return self.por_norm[cand], "fuzzy"
        return None, None


# ----------------------------------------------------------------- download / parsing
def baixar_zip() -> tuple[bytes, dict]:
    """Baixa o zip (com cache por Last-Modified em ORIGINACAO_CACHE)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    arq, meta = CACHE / "oferta_distribuicao.zip", CACHE / "oferta_distribuicao.meta.json"
    info = {"url": URL_ZIP}
    try:
        h = sessao.head(URL_ZIP, timeout=60)
        info["status_head"] = h.status_code
        lm = h.headers.get("Last-Modified")
    except requests.RequestException as e:
        lm = None
        info["erro_head"] = str(e)
    if lm and arq.exists() and meta.exists() and json.loads(meta.read_text()).get("lm") == lm:
        info.update(origem="cache", last_modified=lm)
        return arq.read_bytes(), info
    time.sleep(1)
    r = sessao.get(URL_ZIP, timeout=TIMEOUT)
    r.raise_for_status()
    arq.write_bytes(r.content)
    meta.write_text(json.dumps({"lm": r.headers.get("Last-Modified") or lm}))
    info.update(origem="download", status=r.status_code, bytes=len(r.content),
                last_modified=r.headers.get("Last-Modified"))
    return r.content, info


def ler_160(conteudo_zip: bytes) -> tuple[list[str], list[dict]]:
    z = zipfile.ZipFile(io.BytesIO(conteudo_zip))
    nome = next((n for n in z.namelist() if n.lower() == ARQ_160), None)
    if not nome:
        raise ValueError(f"{ARQ_160} nao esta no zip: {z.namelist()}")
    txt = z.read(nome).decode("latin1")
    rd = csv.DictReader(io.StringIO(txt), delimiter=";")
    return rd.fieldnames, list(rd)


def _v(r, k):
    s = re.sub(r"\s+", " ", (r.get(k) or "")).strip()
    return s or None


def _num(s):
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def _cnpj(s):
    d = re.sub(r"\D", "", s or "")
    return d.zfill(14) if d else None


def tipo_de(valor_mobiliario: str):
    return TIPOS.get(sem_acento(valor_mobiliario or "").strip().lower())


def normalizar_oferta(r: dict) -> dict | None:
    tipo = tipo_de(r.get("Valor_Mobiliario"))
    if not tipo:
        return None
    emissao = _v(r, "Emissao")
    o = {
        "numero_requerimento": _v(r, "Numero_Requerimento"),
        "numero_processo": _v(r, "Numero_Processo"),
        "tipo": tipo,
        "valor_mobiliario": _v(r, "Valor_Mobiliario"),
        "status": _v(r, "Status_Requerimento"),
        "rito": _v(r, "Rito_Requerimento"),
        "tipo_requerimento": _v(r, "Tipo_requerimento"),
        "tipo_oferta": _v(r, "Tipo_Oferta"),
        "data_requerimento": _v(r, "Data_requerimento"),
        "data_registro": _v(r, "Data_Registro"),
        "data_encerramento": _v(r, "Data_Encerramento"),
        "cnpj_emissor": _cnpj(r.get("CNPJ_Emissor")),
        "nome_emissor": _v(r, "Nome_Emissor"),
        "cnpj_lider": _cnpj(r.get("CNPJ_Lider")),
        "nome_lider": _v(r, "Nome_Lider"),
        "emissao": int(emissao) if emissao and emissao.isdigit() else emissao,
        "quantidade": _num(r.get("Qtde_Total_Registrada")),
        "volume": _num(r.get("Valor_Total_Registrado")),
        "titulo_incentivado": _v(r, "Titulo_incentivado"),
        "publico_alvo": _v(r, "Publico_alvo"),
        "sustentavel": _v(r, "Titulo_classificado_como_sustentavel"),
        "regime_distribuicao": _v(r, "Regime_distribuicao"),
        "devedores": (_v(r, "Identificacao_devedores_coobrigados") or "")[:300] or None,
        "indexador": None,     # a fonte (Res. 160) nao traz
        "remuneracao": None,   # a fonte (Res. 160) nao traz
    }
    if not o["numero_requerimento"]:
        return None
    return o


def partes_devedores(txt: str) -> list[str]:
    """Quebra o campo livre de devedores em nomes candidatos."""
    # nomes colados sem separador: "SEARA ALIMENTOS LTDA.JBS S.A."
    txt = re.sub(r"\bS/A\b\.?", "S.A.", txt or "")
    txt = re.sub(r"(S\.\s?A\.?|LTDA\.?)(?=[A-ZÀ-Ý])", r"\1;", txt)
    partes = re.split(r"[;\n/,()]|\bCNPJ\b|\binscrit[ao]\b|\bsociedade\b|\be\s(?=[A-Z])", txt)
    out = []
    for p in partes:
        p = re.sub(r"^\s*(\d+\.\s*)?(devedor(a|as|es)?( principal)?|cedente|fiador(a)?)\s*:?", "", p.strip(), flags=re.I)
        if len(p.strip()) >= 3:
            out.append(p.strip())
    return out


def hash_oferta(o: dict) -> str:
    chaves = [c for c in COLS_OFERTA if c not in ("hash", "no_universo", "casamento", "nome_universo")]
    s = "|".join("" if o.get(k) is None else str(o.get(k)) for k in chaves)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def data_oferta(o: dict):
    return o.get("data_registro") or o.get("data_requerimento")


def na_janela(o: dict, hoje: dt.date, meses=JANELA_MESES) -> bool:
    d = o.get("data_requerimento") or o.get("data_registro")
    if not d:
        return False
    a, m = divmod(hoje.month - 1 - meses, 12)
    corte = dt.date(hoje.year + a, m + 1, min(hoje.day, 28)).isoformat()
    return d >= corte


# ----------------------------------------------------------------- universo / SND / secundario
def ler_csv(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def universo():
    papeis, nomes_cri = [], []
    try:
        papeis = json.loads(UNIVERSO.read_text(encoding="utf-8")).get("papeis", [])
    except FileNotFoundError:
        pass
    try:
        nomes_cri = [a.get("emissor") for a in json.loads(UNIVERSO_CRI.read_text(encoding="utf-8")).get("ativos", [])
                     if a.get("emissor")]
    except FileNotFoundError:
        pass
    return papeis, nomes_cri


_serie_cache: dict[str, dict] = {}


def taxa_na_data(codigo: str, data: str):
    """Taxa indicativa ANBIMA (dados/serie/AAAA-MM-DD.csv) na data ou no pregao anterior."""
    d0 = dt.date.fromisoformat(data)
    for i in range(DIAS_TAXA_ATRAS + 1):
        d = (d0 - dt.timedelta(days=i)).isoformat()
        if d not in _serie_cache:
            _serie_cache[d] = {r["codigo"]: r for r in ler_csv(DIR_SERIE / f"{d}.csv")}
        r = _serie_cache[d].get(codigo)
        if r and r.get("taxa"):
            return float(r["taxa"]), d
    return None, None


def comparar_primario_secundario(ofertas: list[dict], snd: list[dict], papeis: list[dict]):
    por_cnpj_emissao, cnpj_de = {}, {}
    cnpjs_snd = {c.get("cnpj_emissor") for c in snd}
    for c in snd:
        cnpj_de[c["codigo"]] = c.get("cnpj_emissor")
        try:
            k = (c.get("cnpj_emissor"), int(float(c["numero_emissao"])))
        except (TypeError, ValueError, KeyError):
            continue
        por_cnpj_emissao.setdefault(k, []).append(c)
    sec_por_cnpj = {}
    for p in papeis:
        cnpj = cnpj_de.get(p.get("codigo"))
        if cnpj:
            sec_por_cnpj.setdefault(cnpj, []).append(p)
    dur_de = {p["codigo"]: p.get("duration_anos") for p in papeis}

    linhas = []
    for o in ofertas:
        if o["tipo"] != "DEBENTURE":
            continue
        dof = data_oferta(o)
        base = {"numero_requerimento": o["numero_requerimento"], "data_oferta": dof,
                "cnpj_emissor": o["cnpj_emissor"], "emissor": o["nome_emissor"], "emissao": o["emissao"]}
        try:
            serie = por_cnpj_emissao.get((o["cnpj_emissor"], int(o["emissao"])), [])
        except (TypeError, ValueError):
            serie = []
        if not serie:
            obs = ("emissao nao encontrada no SND (CNPJ + numero da emissao)" if o["cnpj_emissor"] in cnpjs_snd
                   else "emissor fora do universo do painel (sem linhas em dados/snd/caracteristicas.csv)")
            linhas.append({**base, "observacao": obs})
            continue
        if dof:  # mesmo numero de emissao reaproveitado por outra empresa incorporada: exige data proxima
            d0 = dt.date.fromisoformat(dof)
            serie = [c for c in serie if c.get("data_emissao")
                     and abs((dt.date.fromisoformat(c["data_emissao"]) - d0).days) <= 180]
        if not serie:
            linhas.append({**base, "observacao": "emissao do SND com data distante da oferta"})
            continue
        cods_emissao = {c["codigo"] for c in serie}
        for c in sorted(serie, key=lambda x: x["codigo"]):
            fam = c.get("familia") or None
            tx_p = _num(c.get("percentual_indexador")) if fam == "DI_PCT" else _num(c.get("taxa_juros"))
            if fam not in ("DI_SPREAD", "DI_PCT") and not tx_p:
                tx_p = None  # IPCA/PRE com taxa 0 no SND = taxa nao informada
            l = {**base, "codigo_primario": c["codigo"], "familia": fam,
                 "remuneracao_primario": c.get("remuneracao"), "taxa_primario": tx_p,
                 "vencimento": c.get("vencimento")}
            # duration de referencia do papel novo
            ref, fonte = dur_de.get(c["codigo"]), "duration_anbima"
            if ref is None and c.get("vencimento") and dof:
                ref = round((dt.date.fromisoformat(c["vencimento"]) - dt.date.fromisoformat(dof)).days / 365.25, 2)
                fonte = "prazo_ate_vencimento"
            l["duration_ref_anos"], l["duration_ref_fonte"] = ref, fonte
            cands = [p for p in sec_por_cnpj.get(o["cnpj_emissor"], [])
                     if p.get("familia") == fam and p["codigo"] not in cods_emissao
                     and p.get("duration_anos") is not None]
            if not cands or ref is None:
                l["observacao"] = "sem papel do mesmo emissor e indexador no secundario"
                linhas.append(l)
                continue
            p = min(cands, key=lambda p: abs(p["duration_anos"] - ref))
            l["codigo_secundario"], l["duration_secundario_anos"] = p["codigo"], p["duration_anos"]
            tx_s, d_s = taxa_na_data(p["codigo"], dof) if dof else (None, None)
            l["taxa_secundario"], l["data_taxa_secundario"] = tx_s, d_s
            if tx_p is None or tx_s is None:
                l["observacao"] = "falta taxa " + ("do primario" if tx_p is None else "do secundario na data da oferta")
            elif fam == "DI_PCT":
                l["diferenca_pct_cdi"] = round(tx_p - tx_s, 2)
                l["observacao"] = "%DI: diferenca em pontos de % do CDI, nao em bps"
            else:
                l["diferenca_bps"] = round((tx_p - tx_s) * 100, 1)
            linhas.append(l)
    return linhas


# ----------------------------------------------------------------- execucao
def gravar_csv(caminho: pathlib.Path, linhas: list[dict], cols: list[str]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for l in linhas:
            w.writerow({k: ("" if l.get(k) is None else l.get(k)) for k in cols})


def inspecionar():
    print("## Ofertas CVM (Res. 160) - inspecao\n")
    try:
        conteudo, info = baixar_zip()
        z = zipfile.ZipFile(io.BytesIO(conteudo))
        print(f"- zip: `{info}`\n- arquivos: `{[(i.filename, i.file_size) for i in z.infolist()]}`")
        cols, linhas = ler_160(conteudo)
        print(f"- {ARQ_160}: {len(linhas):,} linhas, colunas ({len(cols)}): `{cols}`\n")
        alvo = [r for r in linhas if tipo_de(r.get("Valor_Mobiliario"))]
        print(f"- debentures/CRI/CRA: {len(alvo):,}\n")
        for r in alvo[-3:]:
            print("    " + " | ".join(f"{k}={r.get(k)}" for k in cols[:18]))
    except Exception as e:
        print(f"- ERRO: `{type(e).__name__}: {e}`")


def rodar(hoje: dt.date):
    diag = {"lidos": 0, "gravados": {}, "descartados": {}, "erros": {}}
    conteudo, info = baixar_zip()
    cols, linhas = ler_160(conteudo)
    diag["lidos"] = len(linhas)
    diag["fonte"] = info
    fora_tipo = fora_janela = 0
    ofertas = []
    for r in linhas:
        o = normalizar_oferta(r)
        if not o:
            fora_tipo += 1
            continue
        if not na_janela(o, hoje):
            fora_janela += 1
            continue
        ofertas.append(o)
    diag["descartados"] = {"outro_valor_mobiliario": fora_tipo, f"fora_janela_{JANELA_MESES}m": fora_janela}

    papeis, nomes_cri = universo()
    snd = ler_csv(SND_CARAC)
    if not snd:
        diag["erros"]["snd"] = f"{SND_CARAC.relative_to(RAIZ)} ausente: sem indexador/cruzamento por CNPJ"
    cnpj_univ = {}
    cods_univ = {p["codigo"] for p in papeis}
    for c in snd:
        if c["codigo"] in cods_univ and c.get("cnpj_emissor"):
            cnpj_univ[c["cnpj_emissor"]] = c.get("emissor")
    cas_deb = Casador([p.get("emissor") for p in papeis if p.get("emissor")])
    cas_cri = Casador(nomes_cri)
    for o in ofertas:
        nome, como = None, None
        if o["tipo"] == "DEBENTURE":
            if o["cnpj_emissor"] in cnpj_univ:
                nome, como = cnpj_univ[o["cnpj_emissor"]], "cnpj"
            else:
                nome, como = cas_deb.casar(o["nome_emissor"])
        else:
            for dev in partes_devedores(o.get("devedores")):
                nome, como = cas_cri.casar(dev)
                if nome:
                    break
        o["no_universo"], o["casamento"], o["nome_universo"] = bool(nome), como, nome
        o["hash"] = hash_oferta(o)

    DIR_OUT.mkdir(parents=True, exist_ok=True)
    base_ant = {r["numero_requerimento"]: r for r in ler_csv(DIR_OUT / "base.csv")}
    mudaram = [o for o in ofertas if base_ant.get(o["numero_requerimento"], {}).get("hash") != o["hash"]]
    base = dict(base_ant)
    for o in ofertas:
        base[o["numero_requerimento"]] = o
    chave = lambda r: int(r["numero_requerimento"]) if str(r["numero_requerimento"]).isdigit() else 0
    gravar_csv(DIR_OUT / "base.csv", sorted(base.values(), key=chave), COLS_OFERTA)
    if not base_ant:
        diag["primeira_carga"] = "sem base anterior: arquivo do dia nao gravado (seria a base inteira)"
    elif mudaram:
        gravar_csv(DIR_OUT / f"{hoje.isoformat()}.csv", sorted(mudaram, key=chave), COLS_OFERTA)
    pvs = comparar_primario_secundario(ofertas, snd, papeis)
    gravar_csv(DIR_OUT / "primario_vs_secundario.csv", pvs, COLS_PVS)

    por_tipo = {}
    for o in ofertas:
        por_tipo[o["tipo"]] = por_tipo.get(o["tipo"], 0) + 1
    diag["gravados"] = {"base": len(base), "novas_ou_alteradas": len(mudaram),
                        "primario_vs_secundario": len(pvs),
                        "pvs_com_diferenca": sum(1 for l in pvs if l.get("diferenca_bps") is not None
                                                 or l.get("diferenca_pct_cdi") is not None)}
    resumo = {
        "fonte": "CVM dados abertos - oferta-distrib (Res. 160)",
        "url": URL_ZIP,
        "data_referencia": hoje.isoformat(),
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "janela_meses": JANELA_MESES,
        "ofertas_na_janela": len(ofertas),
        "por_tipo": por_tipo,
        "no_universo": sum(1 for o in ofertas if o["no_universo"]),
        "volume_debentures_janela": round(sum(o["volume"] or 0 for o in ofertas if o["tipo"] == "DEBENTURE"), 2),
        "diagnostico": diag,
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8")
    return resumo, pvs


def resumo_md(s: dict, pvs: list[dict]) -> str:
    d = s["diagnostico"]
    out = [f"## Ofertas CVM {s['data_referencia']}\n",
           f"- Linhas lidas (Res. 160): {d['lidos']:,} | fonte: `{d.get('fonte', {}).get('origem')}`",
           f"- Na janela de {s['janela_meses']}m: **{s['ofertas_na_janela']}** `{s['por_tipo']}`, "
           f"no universo: {s['no_universo']}",
           f"- Gravados: `{d['gravados']}` | descartados: `{d['descartados']}`",
           f"- Erros: `{d['erros']}`\n"]
    com = [l for l in pvs if l.get("diferenca_bps") is not None][-10:]
    if com:
        out += ["| Oferta | Emissor | Primario | Secundario | Dif. (bps) |", "|---|---|---|---|---:|"]
        for l in com:
            out.append(f"| {l['data_oferta']} | {l['emissor']} | {l['codigo_primario']} {l['taxa_primario']} "
                       f"| {l['codigo_secundario']} {l['taxa_secundario']} | {l['diferenca_bps']} |")
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
            inspecionar()
            return 0
        s, pvs = rodar(hoje)
        md = resumo_md(s, pvs)
        print(md)
        no_summary(md)
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        no_summary("## Ofertas: erro\n\n```\n" + tb + "\n```")
        return 1


if __name__ == "__main__":
    sys.exit(main())
