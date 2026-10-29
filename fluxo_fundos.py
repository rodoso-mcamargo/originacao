#!/usr/bin/env python3
"""
Fluxo dos fundos de credito privado - informe diario da CVM.

Captacao e resgate diarios de cada fundo/classe do universo (credito privado,
incentivados/infra e os fundos que aparecem na CDA do radar), com fluxo liquido
em R$ e % do PL, acumulados de 5, 21 e 63 dias uteis, agregado por gestor e
total, e alerta de resgate forte.

    python fluxo_fundos.py                   # mes corrente e anterior (rotina diaria)
    python fluxo_fundos.py --meses 3         # backfill: grava os ultimos 3 meses (ate 24)
    python fluxo_fundos.py --inspecionar     # so descreve as fontes, nao grava
    python fluxo_fundos.py --resumo          # markdown do ultimo.json (para o summary)
    python fluxo_fundos.py --cache DIR       # onde ficam os zips (padrao ~/.cache/fluxo-fundos)

Fontes (CVM, dados abertos):
    https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_AAAAMM.zip
    https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip  (cadastro pos-175)

Saidas em dados/fluxo_fundos/ (so o recorte do universo; os zips ficam no cache):
    universo.csv     cnpj, nome, gestor, classe, origem (+ gestora_painel, pl)
    AAAA-MM-DD.csv   uma linha por fundo do universo que informou naquela competencia
    por_gestor/AAAA-MM.csv  serie agregada por gestor x classe, um arquivo por mes
                     (chave: data, gestor, classe)
    alertas.csv      episodios de resgate forte (chave: cnpj, regra, data_inicio)
    ultimo.json      data de referencia, maiores resgates e bloco diagnostico
"""
from __future__ import annotations

import socket

_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse
import datetime as dt
import io
import json
import logging
import os
import pathlib
import re
import sys
import time
import traceback
import unicodedata
import zipfile

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("fluxo_fundos")

RAIZ = pathlib.Path(__file__).resolve().parent
DIR_OUT = RAIZ / "dados" / "fluxo_fundos"
GESTORAS_FLOW = RAIZ / "painel" / "gestoras-flow.json"   # so leitura
DIR_CDA = RAIZ / "dados" / "cda"                          # so leitura

BASE = "https://dados.cvm.gov.br/dados/FI"
URL_INF = BASE + "/DOC/INF_DIARIO/DADOS/inf_diario_fi_{aaaamm}.zip"
URL_CAD = BASE + "/CAD/DADOS/registro_fundo_classe.zip"
HEADERS = {"User-Agent": "radar-secundario-originacao/1.0 (+https://github.com/rodoso-mcamargo/originacao)"}
TIMEOUT = 300
PAUSA = 1.0  # segundos entre requisicoes (<= 1 req/s)

# ---------------------------------------------------------------------------
# Cortes e parametros (documentados no README da pasta)
# ---------------------------------------------------------------------------
CORTES = {
    # alerta quando o resgate liquido acumulado passa destes % do PL base
    # (PL do dia util anterior ao inicio da janela)
    "resgate_5d_pct": 5.0,
    "resgate_21d_pct": 10.0,
    # PL minimo das classes de renda fixa que entram pelo nome (origem filtro_nome);
    # os fundos da CDA entram sem corte de PL
    "pl_minimo_filtro_nome": 50e6,
    # um dia entra no calendario de dias uteis se tiver pelo menos esta fracao
    # da mediana de linhas por dia (descarta datas avulsas de fim de semana)
    "fracao_calendario": 0.2,
    # data de referencia = ultimo dia com pelo menos esta fracao do universo informando
    "cobertura_referencia": 0.8,
}
JANELAS = (5, 21, 63)
MESES_EXTRAS = 3        # meses lidos antes do periodo gravado, para as janelas de 63 du
MAX_MESES = 24

PALAVRAS_CREDITO = ("CRED PRIV", "CREDITO PRIVADO", "INCENTIVAD", "INFRA", "DEBENTURE", "12.431", "12431")
PALAVRAS_INFRA = ("INCENTIVAD", "INFRA", "12.431", "12431")
RE_EXCLUIDOS = re.compile(r"\b(FIDC|FII|FIP|FMIEE|FIAGRO)\b")

# marca da gestora (nomes de painel/gestoras-flow.json) -> regex no nome do gestor
# (mesmo mapa do carteira_gestoras.py; copiado para nao acoplar os scripts)
MARCAS = {
    "Itaú": r"ITAU", "BB": r"\bBB\b|BANCO DO BRASIL", "Bradesco": r"BRADESCO|\bBRAM\b",
    "Santander": r"SANTANDER", "Safra": r"\bSAFRA\b", "Kinea": r"KINEA", "Caixa": r"\bCAIXA\b",
    "BTG": r"\bBTG\b", "Absolute": r"ABSOLUTE", "XP": r"\bXPA?\b", "ARX": r"\bARX\b",
    "JGP": r"\bJGP\b", "Sparta": r"SPARTA", "Icatu": r"ICATU", "AZ Quest": r"AZ ?QUEST",
    "SulAmérica": r"SULAMERICA", "Legacy": r"LEGACY", "SPX": r"\bSPX\b",
}
SUFIXOS = ("EM RECUPERACAO JUDICIAL", "PARTICIPACOES", "COMPANHIA", "LTDA", "CIA", "S A", "SA")

# nome, gestor e classe ficam em universo.csv (join por cnpj), para o arquivo diario ficar enxuto
COLS_DIA = ["data", "cnpj", "pl", "captacao", "resgate", "liquido",
            "pct_pl_d1", "liq_5d", "pct_5d", "liq_21d", "pct_21d", "liq_63d", "pct_63d",
            "cotistas", "quota", "alerta"]


# ---------------------------------------------------------------------------
# Normalizacao e classificacao
# ---------------------------------------------------------------------------
def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm_texto(s) -> str:
    """Maiusculas, sem acento, espacos simples (mantem pontuacao: '12.431')."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return re.sub(r"\s+", " ", sem_acento(str(s)).upper()).strip()


def norm_nome(s) -> str:
    """Regra 11: sem acento, maiusculas, sem pontuacao, sem sufixos societarios."""
    t = re.sub(r"[^A-Z0-9 ]", " ", norm_texto(s))
    t = re.sub(r"\s+", " ", t).strip()
    mudou = True
    while mudou and t:
        mudou = False
        for suf in SUFIXOS:
            if t.endswith(" " + suf) or t == suf:
                t = t[: -len(suf)].strip()
                mudou = True
    return t


def so_digitos(cnpj) -> str:
    d = re.sub(r"\D", "", str(cnpj or ""))
    return d.zfill(14) if d else ""


def fmt_cnpj(d: str) -> str:
    d = so_digitos(d)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d) == 14 else d


def tem_palavra_credito(nome: str) -> bool:
    n = norm_texto(nome)
    return any(p in n for p in PALAVRAS_CREDITO)


def classificar(nome: str) -> str:
    """incentivado_infra | credito_privado | outros (so pelo nome)."""
    n = norm_texto(nome)
    if any(p in n for p in PALAVRAS_INFRA):
        return "incentivado_infra"
    if any(p in n for p in PALAVRAS_CREDITO):
        return "credito_privado"
    return "outros"


def excluido_por_tipo(*textos) -> bool:
    return any(RE_EXCLUIDOS.search(norm_texto(t)) for t in textos if t)


def marca_gestora(gestor: str, marcas: dict[str, re.Pattern]) -> str | None:
    g = norm_texto(gestor)
    for n, p in marcas.items():
        if p.search(g):
            return n
    return None


def num(v):
    try:
        x = float(v)
        return None if pd.isna(x) else x
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Download com cache
# ---------------------------------------------------------------------------
_ultima_req = [0.0]


def _pausa():
    falta = PAUSA - (time.time() - _ultima_req[0])
    if falta > 0:
        time.sleep(falta)
    _ultima_req[0] = time.time()


def baixar(url: str, cache: pathlib.Path, sempre_conferir: bool, diag: dict) -> pathlib.Path | None:
    """Baixa url para o cache. Se ja existe e sempre_conferir=False, usa o do cache.
    Se sempre_conferir, faz HEAD e so baixa de novo se Last-Modified/tamanho mudaram."""
    cache.mkdir(parents=True, exist_ok=True)
    nome = url.rsplit("/", 1)[1]
    alvo, meta = cache / nome, cache / (nome + ".meta.json")
    antigo = json.loads(meta.read_text()) if meta.exists() and alvo.exists() else None
    if antigo and not sempre_conferir:
        return alvo
    try:
        _pausa()
        h = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
        if h.status_code == 404:
            log.info("%s nao publicado (404)", nome)
            return None
        h.raise_for_status()
        novo = {"last_modified": h.headers.get("Last-Modified"), "tamanho": h.headers.get("Content-Length")}
        if antigo and antigo == novo:
            log.info("%s sem mudanca (cache)", nome)
            return alvo
        _pausa()
        log.info("baixando %s", url)
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            tmp = alvo.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for bloco in r.iter_content(1 << 20):
                    fh.write(bloco)
        tmp.replace(alvo)
        meta.write_text(json.dumps(novo))
        return alvo
    except Exception as e:  # noqa: BLE001
        diag.setdefault("erros", []).append(f"{nome}: {type(e).__name__}: {e}")
        if alvo.exists():
            log.warning("falha ao conferir %s (%s); usando o cache", nome, e)
            return alvo
        raise


# ---------------------------------------------------------------------------
# Leitura das fontes
# ---------------------------------------------------------------------------
def ler_informe(arq) -> pd.DataFrame:
    """Le um inf_diario_fi_AAAAMM.csv (dentro do zip ou arquivo aberto) e devolve uma
    linha por (cnpj, data): classes com subclasses vem com uma linha por subclasse, que
    sao somadas (PL, captacao, resgate, cotistas); a quota so fica quando ha uma linha."""
    if isinstance(arq, (str, pathlib.Path)) and str(arq).endswith(".zip"):
        z = zipfile.ZipFile(arq)
        nomes = [n for n in z.namelist() if n.lower().endswith(".csv")]
        partes = [pd.read_csv(z.open(n), sep=";", dtype=str, encoding="latin-1") for n in nomes]
        df = pd.concat(partes, ignore_index=True)
    else:
        df = pd.read_csv(arq, sep=";", dtype=str, encoding="latin-1")
    df.columns = [c.strip().upper() for c in df.columns]
    col_cnpj = next(c for c in ("CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO") if c in df.columns)
    out = pd.DataFrame({
        "cnpj": df[col_cnpj].map(so_digitos),
        "data": df["DT_COMPTC"].str.strip(),
        "pl": pd.to_numeric(df["VL_PATRIM_LIQ"], errors="coerce"),
        "captacao": pd.to_numeric(df["CAPTC_DIA"], errors="coerce"),
        "resgate": pd.to_numeric(df["RESG_DIA"], errors="coerce"),
        "cotistas": pd.to_numeric(df.get("NR_COTST"), errors="coerce"),
        "quota": pd.to_numeric(df["VL_QUOTA"], errors="coerce"),
    })
    # regra 8: campo vazio na fonte nao vira zero. Na soma das subclasses, se alguma
    # subclasse veio sem o campo, o total da classe fica null (soma parcial seria inventada).
    somar = ["pl", "captacao", "resgate", "cotistas"]
    g = out.groupby(["cnpj", "data"], sort=False)
    agg = g[somar].sum(min_count=1)
    faltas = out[somar].isna().groupby([out["cnpj"], out["data"]], sort=False).sum()
    agg = agg.mask(faltas > 0)
    agg["quota"] = g["quota"].first()
    agg["linhas"] = g.size()
    agg = agg.reset_index()
    agg.loc[agg["linhas"] > 1, "quota"] = None
    return agg


def ler_cadastro(arq) -> pd.DataFrame:
    """registro_fundo_classe.zip -> uma linha por CNPJ (classe e, para fundos sem classe
    registrada, o proprio fundo), com nome, gestor, tipo, classificacao, situacao, PL."""
    z = zipfile.ZipFile(arq)

    def ler(nome):
        n = next(x for x in z.namelist() if x.lower().endswith(nome))
        return pd.read_csv(z.open(n), sep=";", dtype=str, encoding="latin-1")

    fu = ler("registro_fundo.csv")
    cl = ler("registro_classe.csv")
    fu_idx = fu.set_index("ID_Registro_Fundo")
    cl = cl.join(fu_idx[["Denominacao_Social", "Gestor", "CPF_CNPJ_Gestor", "Tipo_Fundo"]]
                 .rename(columns={"Denominacao_Social": "nome_fundo"}), on="ID_Registro_Fundo")
    a = pd.DataFrame({
        "cnpj": cl["CNPJ_Classe"].map(so_digitos), "nome": cl["Denominacao_Social"],
        "nome_fundo": cl["nome_fundo"], "gestor": cl["Gestor"], "cnpj_gestor": cl["CPF_CNPJ_Gestor"],
        "tipo": cl["Tipo_Classe"], "classificacao": cl["Classificacao"], "situacao": cl["Situacao"],
        "pl_cadastro": pd.to_numeric(cl["Patrimonio_Liquido"], errors="coerce"), "nivel": "classe"})
    b = pd.DataFrame({
        "cnpj": fu["CNPJ_Fundo"].map(so_digitos), "nome": fu["Denominacao_Social"],
        "nome_fundo": fu["Denominacao_Social"], "gestor": fu["Gestor"], "cnpj_gestor": fu["CPF_CNPJ_Gestor"],
        "tipo": fu["Tipo_Fundo"], "classificacao": None, "situacao": fu["Situacao"],
        "pl_cadastro": pd.to_numeric(fu["Patrimonio_Liquido"], errors="coerce"), "nivel": "fundo"})
    cad = pd.concat([a, b], ignore_index=True)
    # prefere a classe; entre duplicadas, a que esta em funcionamento
    cad["_ord"] = (cad["nivel"] != "classe").astype(int) * 2 + (cad["situacao"] != "Em Funcionamento Normal").astype(int)
    cad = cad.sort_values(["cnpj", "_ord"]).drop_duplicates("cnpj").drop(columns="_ord")
    return cad.set_index("cnpj")


def ler_cnpjs_cda() -> tuple[set[str], str | None]:
    arqs = sorted(p for p in DIR_CDA.glob("*.csv") if re.fullmatch(r"\d{4}-\d{2}\.csv", p.name))
    if not arqs:
        return set(), None
    df = pd.read_csv(arqs[-1], dtype=str, usecols=["cnpj"])
    return {so_digitos(c) for c in df["cnpj"].dropna()}, arqs[-1].name


def ler_marcas_painel() -> dict[str, re.Pattern]:
    try:
        g = json.loads(GESTORAS_FLOW.read_text(encoding="utf-8"))
        nomes = [x["n"] for x in g.get("gestoras", [])]
    except Exception:  # noqa: BLE001
        nomes = []
    out = {}
    for n in nomes:
        pat = MARCAS.get(n) or r"\b" + re.escape(norm_texto(n)) + r"\b"
        out[n] = re.compile(pat)
    return out


# ---------------------------------------------------------------------------
# Universo
# ---------------------------------------------------------------------------
def montar_universo(cad: pd.DataFrame, pl_informe: pd.Series, cnpjs_cda: set[str],
                    marcas: dict[str, re.Pattern], diag: dict) -> pd.DataFrame:
    """(1) fundos da CDA do radar (dados/cda, sem corte de PL); as gestoras de
    painel/gestoras-flow.json so trazem nome (sem CNPJ de fundo), entao viram a coluna
    gestora_painel de cada fundo; (2) classes de renda fixa ativas com palavra de credito
    no nome e PL >= corte. Sem FIDC, FII, FIP."""
    linhas, descartes = {}, {}

    def pl_de(c):
        v = pl_informe.get(c)
        if v is None or pd.isna(v):
            v = cad["pl_cadastro"].get(c) if c in cad.index else None
        return None if v is None or pd.isna(v) else float(v)

    def add(c, origem):
        r = cad.loc[c] if c in cad.index else None
        nome = (r["nome"] if r is not None else None) or ""
        if r is not None and excluido_por_tipo(r["tipo"], nome):
            descartes[c] = "FIDC/FII/FIP"
            return
        if c not in linhas:
            gestor = None if r is None or pd.isna(r["gestor"]) else r["gestor"]
            nome_cls = nome + " " + ("" if r is None or pd.isna(r["nome_fundo"]) else r["nome_fundo"])
            linhas[c] = {"cnpj": fmt_cnpj(c), "nome": nome or None, "gestor": gestor,
                         "classe": classificar(nome_cls),
                         "gestora_painel": marca_gestora(gestor or "", marcas) if gestor else None,
                         "pl": pl_de(c), "origem": [], "sem_cadastro": r is None}
        if origem not in linhas[c]["origem"]:
            linhas[c]["origem"].append(origem)

    for c in sorted(cnpjs_cda):
        add(c, "cda")

    ativos = cad[(cad["situacao"] == "Em Funcionamento Normal")
                 & cad["tipo"].fillna("").str.contains("FIF|^FI$|FACFIF", regex=True)]
    for c, r in ativos.iterrows():
        nomes = f"{r['nome']} {'' if pd.isna(r['nome_fundo']) else r['nome_fundo']}"
        if not tem_palavra_credito(nomes) or excluido_por_tipo(r["tipo"], nomes):
            continue
        pl = pl_de(c)
        rf = r["classificacao"] == "Renda Fixa" or (pd.isna(r["classificacao"]) and "RENDA FIXA" in norm_texto(nomes))
        if rf and pl is not None and pl >= CORTES["pl_minimo_filtro_nome"]:
            add(c, "filtro_nome")

    u = pd.DataFrame(linhas.values())
    if u.empty:
        u = pd.DataFrame(columns=["cnpj", "nome", "gestor", "classe", "gestora_painel", "pl", "origem", "sem_cadastro"])
    u["origem"] = u["origem"].map(lambda xs: "+".join(xs))
    diag["universo"] = {
        "fundos": int(len(u)),
        "por_origem": {k: int(v) for k, v in u["origem"].str.split("+").explode().value_counts().items()},
        "por_classe": {k: int(v) for k, v in u["classe"].value_counts().items()},
        "descartados_fidc_fii_fip": len(descartes),
    }
    diag["fundos_sem_cadastro"] = sorted(u.loc[u["sem_cadastro"], "cnpj"].tolist())
    return u.drop(columns="sem_cadastro").sort_values(["gestor", "nome"], na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Calculo
# ---------------------------------------------------------------------------
def calendario(inf: pd.DataFrame) -> list[str]:
    cont = inf.groupby("data").size()
    corte = cont.median() * CORTES["fracao_calendario"]
    dias = [d for d, n in cont.items() if n >= corte and dt.date.fromisoformat(d).weekday() < 5]
    return sorted(dias)


def calcular(inf: pd.DataFrame, datas: list[str]) -> pd.DataFrame:
    """inf: linhas (cnpj, data, pl, captacao, resgate, cotistas, quota) so do universo.
    Devolve as mesmas linhas com pl_d1, liquido, % e acumulados por janela.
    Dia sem informe conta fluxo zero nas janelas; PL segue o ultimo informado.
    Dia com informe mas captacao ou resgate vazio: liquido null, e toda janela que
    contem esse dia fica null (regra 8: nao inventar zero).
    Base do % de cada janela = PL do dia util anterior ao inicio da janela."""
    inf = inf[inf["data"].isin(datas)]
    if inf.empty:
        return inf.assign(**{c: [] for c in COLS_DIA if c not in inf.columns})
    idx = pd.Index(datas, name="data")
    piv = lambda col: inf.pivot(index="data", columns="cnpj", values=col).reindex(idx)  # noqa: E731
    cap, res, pl = piv("captacao"), piv("resgate"), piv("pl")
    informou = inf.assign(_u=True).pivot(index="data", columns="cnpj", values="_u").reindex(idx)
    informou = informou.notna()          # o fundo tem linha no informe naquele dia
    liq = cap - res                     # captacao ou resgate vazio na fonte -> liquido null
    sem_fluxo = informou & liq.isna()   # informou, mas sem o fluxo do dia
    pl_ff = pl.ffill()
    pl_d1 = pl_ff.shift(1)
    base_ok = lambda b: b.where(b > 0)  # noqa: E731
    res_df = {"pl_d1": pl_d1, "liquido": liq, "pct_pl_d1": liq / base_ok(pl_d1) * 100}
    liq0 = liq.fillna(0)
    for n in JANELAS:
        roll = liq0.rolling(n, min_periods=1).sum()
        roll.iloc[: n - 1] = float("nan")   # historico insuficiente -> null
        roll = roll.mask(sem_fluxo.astype(int).rolling(n, min_periods=1).sum() > 0)   # janela com fluxo vazio -> null
        base = base_ok(pl_ff.shift(n))
        res_df[f"liq_{n}d"] = roll
        res_df[f"pct_{n}d"] = roll / base * 100
    longo = pd.concat({k: v.where(informou).stack(future_stack=True) for k, v in res_df.items()}, axis=1)
    longo = longo.reset_index()
    out = inf.merge(longo, on=["data", "cnpj"], how="left")
    out["alerta"] = out.apply(rotulo_alerta, axis=1)
    return out.sort_values(["data", "cnpj"]).reset_index(drop=True)


def em_alerta(liq, pct, corte) -> bool:
    return liq is not None and pct is not None and not pd.isna(liq) and not pd.isna(pct) and liq < 0 and -pct > corte


def rotulo_alerta(r) -> str:
    regras = []
    if em_alerta(r.get("liq_5d"), r.get("pct_5d"), CORTES["resgate_5d_pct"]):
        regras.append("5d")
    if em_alerta(r.get("liq_21d"), r.get("pct_21d"), CORTES["resgate_21d_pct"]):
        regras.append("21d")
    return "+".join(regras)


def episodios(calc: pd.DataFrame) -> pd.DataFrame:
    """Episodios de alerta por fundo e regra: dias seguidos (entre os dias em que o
    fundo informou) com a regra disparada."""
    regs = []
    for regra, jan in (("resgate_5d", "5d"), ("resgate_21d", "21d")):
        d = calc[["cnpj", "data", f"liq_{jan}", f"pct_{jan}", "alerta", "pl"]].sort_values(["cnpj", "data"])
        d = d.assign(on=d["alerta"].str.split("+").map(lambda xs: jan in xs))
        for cnpj, g in d.groupby("cnpj", sort=False):
            run = (g["on"] != g["on"].shift()).cumsum()
            for _, gg in g[g["on"]].groupby(run[g["on"]]):
                pior = gg.loc[gg[f"pct_{jan}"].idxmin()]
                regs.append({"cnpj": cnpj, "regra": regra, "data_inicio": gg["data"].iloc[0],
                             "data_fim": gg["data"].iloc[-1], "dias": len(gg),
                             "pior_pct": round(float(pior[f"pct_{jan}"]), 2),
                             "liq_no_pior": round(float(pior[f"liq_{jan}"]), 2),
                             "data_pior": pior["data"]})
    cols = ["cnpj", "regra", "data_inicio", "data_fim", "dias", "pior_pct", "liq_no_pior", "data_pior"]
    return pd.DataFrame(regs, columns=cols)


def agregar(calc: pd.DataFrame, uni: pd.DataFrame) -> pd.DataFrame:
    """Serie por gestor x classe, mais 'todas' e o TOTAL. % = soma dos fluxos / soma das bases."""
    meta = uni.assign(cnpj=uni["cnpj"].map(so_digitos))[["cnpj", "gestor", "classe"]]
    d = calc.merge(meta, on="cnpj", how="left")
    d["gestor"] = d["gestor"].fillna("(sem cadastro)")
    for n in JANELAS:   # base de cada janela, para o % agregado
        d[f"base_{n}d"] = (d[f"liq_{n}d"] / d[f"pct_{n}d"] * 100).where(d[f"pct_{n}d"].notna() & (d[f"pct_{n}d"] != 0))
    d["base_d1"] = d["pl_d1"].where(d["pct_pl_d1"].notna())
    somar = ["pl", "pl_d1", "captacao", "resgate", "liquido", "base_d1"] + \
            [f"liq_{n}d" for n in JANELAS] + [f"base_{n}d" for n in JANELAS]
    partes = []
    for chaves, gest, cls in ((["data", "gestor", "classe"], None, None),
                              (["data", "gestor"], None, "todas"),
                              (["data", "classe"], "TOTAL", None),
                              (["data"], "TOTAL", "todas")):
        g = d.groupby(chaves)
        a = g[somar].sum(min_count=1)
        a["n_fundos"] = g.size()
        a = a.reset_index()
        if gest:
            a["gestor"] = gest
        if cls:
            a["classe"] = cls
        partes.append(a)
    a = pd.concat(partes, ignore_index=True)
    # "todas" de um gestor so quando ele tem mais de uma classe naquele dia (senao repete a linha)
    n_cls = partes[0].groupby(["data", "gestor"]).size()
    so_uma = a.set_index(["data", "gestor"]).index.isin(n_cls[n_cls == 1].index)
    a = a[~((a["classe"] == "todas") & (a["gestor"] != "TOTAL") & so_uma)]
    a["pct_pl_d1"] = a["liquido"] / a["base_d1"].where(a["base_d1"] > 0) * 100
    for n in JANELAS:
        a[f"pct_{n}d"] = a[f"liq_{n}d"] / a[f"base_{n}d"].where(a[f"base_{n}d"] > 0) * 100
    cols = ["data", "gestor", "classe", "n_fundos", "pl", "captacao", "resgate", "liquido", "pct_pl_d1"] + \
           [x for n in JANELAS for x in (f"liq_{n}d", f"pct_{n}d")]
    return a[cols].sort_values(["data", "gestor", "classe"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Gravacao
# ---------------------------------------------------------------------------
def arred(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if c.startswith("pct"):
            df[c] = df[c].round(3)
        elif c in ("pl", "pl_d1", "captacao", "resgate", "liquido") or c.startswith("liq_"):
            df[c] = df[c].round(0).astype("Int64")   # R$ inteiros
    return df


def gravar_diarios(calc: pd.DataFrame, datas_gravar: list[str]) -> int:
    d = calc[calc["data"].isin(datas_gravar)].copy()
    d["cnpj"] = d["cnpj"].map(fmt_cnpj)
    d["cotistas"] = d["cotistas"].astype("Int64")
    d = arred(d)[COLS_DIA]
    n = 0
    for data, g in d.groupby("data"):
        g.to_csv(DIR_OUT / f"{data}.csv", index=False, encoding="utf-8", lineterminator="\n")
        n += 1
    return n


def mesclar_csv(caminho: pathlib.Path, novo: pd.DataFrame, chave: list[str], substituir) -> pd.DataFrame:
    """Base acumulada: tira do antigo as linhas em que substituir(df) e verdadeiro e junta o novo."""
    if caminho.exists():
        velho = pd.read_csv(caminho, dtype={"cnpj": str})
        velho = velho[~substituir(velho)]
        novo = pd.concat([velho, novo], ignore_index=True)
    novo = novo.drop_duplicates(chave, keep="last").sort_values(chave).reset_index(drop=True)
    novo.to_csv(caminho, index=False, encoding="utf-8", lineterminator="\n")
    return novo


def gravar_por_gestor(pg_novo: pd.DataFrame, datas_gravar: list[str], pasta: pathlib.Path) -> list[str]:
    """Serie por gestor particionada por mes (por_gestor/AAAA-MM.csv): a rodada diaria so
    regrava os meses que recalculou. Chave em cada arquivo: data, gestor, classe."""
    pasta.mkdir(parents=True, exist_ok=True)
    gravados = []
    for mes in sorted({d[:7] for d in datas_gravar}):
        datas_mes = [d for d in datas_gravar if d[:7] == mes]
        mesclar_csv(pasta / f"{mes}.csv", pg_novo[pg_novo["data"].str[:7] == mes], ["data", "gestor", "classe"],
                    lambda v, dm=datas_mes: v["data"].isin(dm))
        gravados.append(mes)
    return gravados


def mesclar_alertas(eps: pd.DataFrame, uni: pd.DataFrame, data_ini: str, data_ant: str, data_ref: str,
                    ult_por_fundo: dict) -> pd.DataFrame:
    """eps: episodios recalculados a partir de data_ini (primeiro dia em que as duas regras
    ja podem ser avaliadas). Antigos que acabaram antes de data_ini ficam; episodio que
    comeca em data_ini e continua um antigo (que chegou ate data_ant) herda o inicio."""
    caminho = DIR_OUT / "alertas.csv"
    eps = eps.copy()
    eps["cnpj"] = eps["cnpj"].map(fmt_cnpj)
    velho = pd.read_csv(caminho, dtype=str) if caminho.exists() else pd.DataFrame(columns=eps.columns)
    for i, r in eps[eps["data_inicio"] == data_ini].iterrows():
        m = velho[(velho["cnpj"] == r["cnpj"]) & (velho["regra"] == r["regra"]) & (velho["data_fim"] >= data_ant)]
        if not m.empty:
            eps.at[i, "data_inicio"] = m["data_inicio"].min()
    velho = velho[velho["data_fim"] < data_ini].assign(ativo=False)   # dali em diante e recalculado
    meta = uni.set_index("cnpj")[["nome", "gestor", "classe"]]
    eps = eps.join(meta, on="cnpj")
    # ativo: vai ate o ultimo informe do fundo, e esse informe nao e anterior a data de referencia
    eps["ativo"] = [r["data_fim"] == ult_por_fundo.get(so_digitos(r["cnpj"])) and r["data_fim"] >= data_ref
                    for _, r in eps.iterrows()]
    tudo = pd.concat([velho, eps], ignore_index=True)
    tudo["ativo"] = tudo["ativo"].astype(str).str.lower().map({"true": True}).fillna(False)
    cols = ["cnpj", "nome", "gestor", "classe", "regra", "data_inicio", "data_fim", "dias", "ativo",
            "pior_pct", "liq_no_pior", "data_pior"]
    tudo = tudo[cols].drop_duplicates(["cnpj", "regra", "data_inicio"], keep="last")
    tudo = tudo.sort_values(["data_inicio", "cnpj", "regra"]).reset_index(drop=True)
    tudo.to_csv(caminho, index=False, encoding="utf-8", lineterminator="\n")
    return tudo


def meses_ate(aaaamm: str, n: int) -> list[str]:
    a, m = int(aaaamm[:4]), int(aaaamm[4:])
    out = []
    for _ in range(n):
        out.append(f"{a}{m:02d}")
        m -= 1
        if m == 0:
            a, m = a - 1, 12
    return out[::-1]


# ---------------------------------------------------------------------------
# Modos
# ---------------------------------------------------------------------------
def inspecionar(cache: pathlib.Path) -> None:
    diag: dict = {}
    hoje = dt.date.today()
    print("## Fluxo de fundos - inspecao das fontes\n")
    for aaaamm in meses_ate(hoje.strftime("%Y%m"), 2)[::-1]:
        url = URL_INF.format(aaaamm=aaaamm)
        _pausa()
        h = requests.head(url, headers=HEADERS, timeout=60)
        print(f"### `{url}`\n\n- status: {h.status_code} | Last-Modified: {h.headers.get('Last-Modified')} "
              f"| tamanho: {h.headers.get('Content-Length')}\n")
        if h.status_code != 200:
            continue
        arq = baixar(url, cache, True, diag)
        z = zipfile.ZipFile(arq)
        for n in z.namelist():
            df = pd.read_csv(z.open(n), sep=";", dtype=str, encoding="latin-1")
            print(f"- `{n}`: {len(df):,} linhas, datas {df['DT_COMPTC'].min()} a {df['DT_COMPTC'].max()}")
            print(f"- colunas: `{list(df.columns)}`")
            print(f"- tipos: `{df.iloc[:, 0].value_counts().to_dict()}`")
            sub = df["ID_SUBCLASSE"].notna().sum() if "ID_SUBCLASSE" in df.columns else 0
            print(f"- linhas com ID_SUBCLASSE: {sub:,}\n")
            print("```\n" + df.head(3).to_csv(sep=";", index=False) + "```\n")
        break
    _pausa()
    h = requests.head(URL_CAD, headers=HEADERS, timeout=60)
    print(f"### `{URL_CAD}`\n\n- status: {h.status_code} | Last-Modified: {h.headers.get('Last-Modified')} "
          f"| tamanho: {h.headers.get('Content-Length')}\n")
    if h.status_code == 200:
        arq = baixar(URL_CAD, cache, True, diag)
        z = zipfile.ZipFile(arq)
        for n in z.namelist():
            df = pd.read_csv(z.open(n), sep=";", dtype=str, encoding="latin-1")
            print(f"- `{n}`: {len(df):,} linhas")
            print(f"- colunas: `{list(df.columns)}`\n")
            print("```\n" + df.head(3).to_csv(sep=";", index=False) + "```\n")
    if diag.get("erros"):
        print("### Erros\n\n" + "\n".join(f"- {e}" for e in diag["erros"]))


def rodar(meses: int, cache: pathlib.Path) -> dict:
    meses = max(1, min(int(meses), MAX_MESES))
    diag: dict = {"erros": []}
    DIR_OUT.mkdir(parents=True, exist_ok=True)

    # meses: o mais recente publicado e para tras (gravados + extras para as janelas)
    hoje = dt.date.today().strftime("%Y%m")
    lista = meses_ate(hoje, meses + MESES_EXTRAS)
    gravar_meses = set(lista[-meses:])
    zips, lidos = {}, []
    for i, aaaamm in enumerate(lista):
        recente = aaaamm in meses_ate(hoje, 2)   # mes corrente e anterior: sempre confere
        try:
            z = baixar(URL_INF.format(aaaamm=aaaamm), cache, recente, diag)
        except Exception as e:  # noqa: BLE001
            diag["erros"].append(f"inf_diario {aaaamm}: {type(e).__name__}: {e}")
            z = None
        if z is None:
            if aaaamm == hoje:   # inicio do mes: ainda nao publicado -> estende um mes para tras
                gravar_meses.add(meses_ate(hoje, meses + 1)[0])
            continue
        zips[aaaamm] = z
    if not zips:
        raise RuntimeError("nenhum informe diario disponivel")

    partes = []
    for aaaamm, z in sorted(zips.items()):
        p = ler_informe(z)
        lidos.append({"mes": aaaamm, "linhas_cnpj_data": int(len(p)), "linhas_brutas": int(p["linhas"].sum()),
                      "fundos": int(p["cnpj"].nunique()), "datas": [p["data"].min(), p["data"].max()]})
        partes.append(p)
    inf = pd.concat(partes, ignore_index=True)
    datas = calendario(inf)

    cad = ler_cadastro(baixar(URL_CAD, cache, True, diag))
    ult = inf.sort_values("data").drop_duplicates("cnpj", keep="last").set_index("cnpj")
    cnpjs_cda, arq_cda = ler_cnpjs_cda()
    uni = montar_universo(cad, ult["pl"], cnpjs_cda, ler_marcas_painel(), diag)
    uni.to_csv(DIR_OUT / "universo.csv", index=False, encoding="utf-8", lineterminator="\n")

    alvo = set(uni["cnpj"].map(so_digitos))
    inf_u = inf[inf["cnpj"].isin(alvo)].drop(columns="linhas")
    calc = calcular(inf_u, datas)

    datas_gravar = [d for d in datas if d[:7].replace("-", "") in gravar_meses]
    n_arq = gravar_diarios(calc, datas_gravar)

    # data de referencia: ultimo dia com boa cobertura do universo
    cont = calc.groupby("data").size()
    com_dado = calc["cnpj"].nunique()
    boas = [d for d, n in cont.items() if n >= CORTES["cobertura_referencia"] * com_dado]
    data_ref = max(boas) if boas else max(cont.index)

    pg = arred(agregar(calc, uni))
    pg_novo = pg[pg["data"].isin(datas_gravar)]
    gravar_por_gestor(pg_novo, datas_gravar, DIR_OUT / "por_gestor")

    ult_por_fundo = calc.groupby("cnpj")["data"].max().to_dict()
    # alertas so a partir do dia em que a janela de 21 du ja tem base (posicao 21 do calendario)
    i0 = min(21, len(datas) - 1)
    eps = episodios(calc[calc["data"] >= datas[i0]])
    alertas = mesclar_alertas(eps, uni, datas[i0], datas[max(i0 - 1, 0)], data_ref, ult_por_fundo)

    ref = calc[calc["data"] == data_ref].merge(
        uni.assign(cnpj=uni["cnpj"].map(so_digitos))[["cnpj", "nome", "gestor", "classe"]], on="cnpj", how="left")
    top = ref[ref["liq_21d"] < 0].sort_values("liq_21d").head(10)
    top_lista = [{"cnpj": fmt_cnpj(r["cnpj"]), "nome": r["nome"], "gestor": r["gestor"], "classe": r["classe"],
                  "pl": num(round(r["pl"], 2)), "liq_21d": num(round(r["liq_21d"], 2)),
                  "pct_21d": num(round(r["pct_21d"], 3)) if pd.notna(r["pct_21d"]) else None}
                 for _, r in top.iterrows()]
    tot = pg[(pg["data"] == data_ref) & (pg["gestor"] == "TOTAL")].set_index("classe")
    totais = {c: {k: num(tot.loc[c, k]) for k in ["n_fundos", "pl", "liquido", "liq_5d", "pct_5d",
                                                  "liq_21d", "pct_21d", "liq_63d", "pct_63d"]}
              for c in tot.index}

    sem_informe = sorted(set(uni["cnpj"]) - {fmt_cnpj(c) for c in calc["cnpj"].unique()})
    diag.update({
        "meses_lidos": lidos,
        "meses_gravados": sorted(gravar_meses & set(zips)),
        "linhas_lidas": int(sum(x["linhas_brutas"] for x in lidos)),
        "linhas_universo": int(len(calc)),
        "linhas_gravadas": int(calc["data"].isin(datas_gravar).sum()),
        "descartadas_fora_universo": int(len(inf) - len(inf_u)),
        "dias_uteis_no_calendario": len(datas),
        "arquivos_diarios_gravados": n_arq,
        "cda_usada": arq_cda,
        "fundos_com_informe": int(com_dado),
        "fundos_sem_informe": len(sem_informe),
        "amostra_sem_informe": sem_informe[:20],
        "n_fundos_sem_cadastro": len(diag.get("fundos_sem_cadastro", [])),
        "data_mais_recente": max(datas),
        "fundos_na_data_mais_recente": int(cont.get(max(datas), 0)),
        "cortes": CORTES,
    })
    saida = {
        "data_referencia": data_ref,
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fonte": "CVM - informe diario de fundos (inf_diario_fi) e cadastro pos-175 (registro_fundo_classe)",
        "universo": int(len(uni)),
        "totais": totais,
        "maiores_resgates_21d": top_lista,
        "alertas_ativos": int(alertas["ativo"].sum()),
        "fundos_em_alerta": int(alertas.loc[alertas["ativo"], "cnpj"].nunique()),
        "diagnostico": diag,
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(saida, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("universo=%d datas=%d arquivos=%d ref=%s alertas_ativos=%d", len(uni), len(datas), n_arq,
             data_ref, saida["alertas_ativos"])
    return saida


def resumo() -> None:
    s = json.loads((DIR_OUT / "ultimo.json").read_text(encoding="utf-8"))
    d = s["diagnostico"]
    br = lambda v: "-" if v is None else f"{v/1e6:,.1f}"  # noqa: E731
    pc = lambda v: "-" if v is None else f"{v:,.2f}%"  # noqa: E731
    print(f"## Fluxo de fundos - referencia {s['data_referencia']}\n")
    print(f"- Universo: **{s['universo']}** fundos/classes `{d['universo']['por_origem']}` `{d['universo']['por_classe']}`")
    print(f"- Meses lidos: {[m['mes'] for m in d['meses_lidos']]} | gravados: {d['meses_gravados']}")
    print(f"- Linhas lidas: {d['linhas_lidas']:,} | do universo: {d['linhas_universo']:,} | gravadas: {d['linhas_gravadas']:,}")
    print(f"- Arquivos diarios: {d['arquivos_diarios_gravados']} | dias uteis: {d['dias_uteis_no_calendario']}")
    print(f"- Fundos com informe: {d['fundos_com_informe']} | sem informe: {d['fundos_sem_informe']} "
          f"| sem cadastro: {d['n_fundos_sem_cadastro']}")
    print(f"- Data mais recente: {d['data_mais_recente']} ({d['fundos_na_data_mais_recente']} fundos)")
    print(f"- Alertas ativos: **{s['alertas_ativos']}** em {s['fundos_em_alerta']} fundos")
    if d.get("erros"):
        print("- Erros: " + "; ".join(d["erros"]))
    print("\n### Total por classe (R$ mi)\n\n| Classe | Fundos | PL | Liq. dia | 5d | 21d | 63d |\n|---|---:|---:|---:|---:|---:|---:|")
    for c, t in s["totais"].items():
        print(f"| {c} | {t['n_fundos']:.0f} | {br(t['pl'])} | {br(t['liquido'])} | {br(t['liq_5d'])} ({pc(t['pct_5d'])}) "
              f"| {br(t['liq_21d'])} ({pc(t['pct_21d'])}) | {br(t['liq_63d'])} ({pc(t['pct_63d'])}) |")
    print("\n### 10 maiores resgates liquidos em 21 dias uteis\n\n| Fundo | Gestor | Classe | PL (R$ mi) | 21d (R$ mi) | % PL |\n|---|---|---|---:|---:|---:|")
    for r in s["maiores_resgates_21d"]:
        print(f"| {r['nome']} | {r['gestor']} | {r['classe']} | {br(r['pl'])} | {br(r['liq_21d'])} | {pc(r['pct_21d'])} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meses", type=int, default=2, help="meses gravados (1-24); padrao 2 = corrente e anterior")
    ap.add_argument("--inspecionar", action="store_true")
    ap.add_argument("--resumo", action="store_true")
    ap.add_argument("--cache", default=os.environ.get("FLUXO_CACHE", str(pathlib.Path.home() / ".cache" / "fluxo-fundos")))
    a = ap.parse_args()
    cache = pathlib.Path(a.cache)
    try:
        if a.resumo:
            resumo()
        elif a.inspecionar:
            inspecionar(cache)
        else:
            rodar(a.meses, cache)
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        resumo_gh = os.environ.get("GITHUB_STEP_SUMMARY")
        if resumo_gh:
            with open(resumo_gh, "a", encoding="utf-8") as fh:
                fh.write(f"## fluxo_fundos.py falhou\n\n```\n{tb}\n```\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
