#!/usr/bin/env python3
"""
Monitor de debentures BR - coleta diaria do secundario ANBIMA.

Arquivo unico de proposito: rodar no GitHub Actions sem instalar nada alem de
`requests`. Baixa os arquivos publicos da ANBIMA, calcula spreads sobre DI e
NTN-B, escore de liquidez e sinais de distorcao, e grava tudo em dados/.

    python monitor.py                 # ultimo dia util
    python monitor.py --data 2026-08-31
    python monitor.py --backfill 60   # z-score precisa de ao menos 20 pregoes

Saidas:
    dados/bruto/AAAA-MM-DD_*.txt   copia fiel do arquivo original (auditoria)
    dados/diario/AAAA-MM-DD.json   snapshot enriquecido do dia
    dados/serie.json               historico compacto por papel
    dados/ultimo.json              ultimo snapshot + destaques  <- o dashboard le este
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import re
import statistics
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass, asdict, field

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("monitor")

RAIZ = pathlib.Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
MAX_HISTORICO = 260



# ========================================================================
# fontes
# ========================================================================
BASE = "https://www.anbima.com.br/informacoes"

# Debêntures — mercado secundário (taxas indicativas)
URL_DEBENTURES = BASE + "/merc-sec-debentures/arqs/db{aammdd}.txt"
# Títulos públicos federais — mercado secundário (LTN, LFT, NTN-B, NTN-F)
URL_TITULOS_PUBLICOS = BASE + "/merc-sec/arqs/ms{aammdd}.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/plain,*/*",
}

TIMEOUT = 45


class FonteIndisponivel(RuntimeError):
    """Arquivo não publicado para a data pedida (feriado, D+0 antes das 19h, etc)."""


def _baixar(url: str, tentativas: int = 3) -> str:
    erro = None
    for n in range(tentativas):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                raise FonteIndisponivel(f"404 em {url}")
            r.raise_for_status()
            texto = r.content.decode("latin-1")
            # A ANBIMA devolve HTTP 200 com página de erro quando não há arquivo.
            if "@" not in texto or len(texto) < 500:
                raise FonteIndisponivel(f"conteúdo inesperado em {url}")
            return texto
        except FonteIndisponivel:
            raise
        except Exception as e:  # rede instável
            erro = e
            log.warning("falha %s (tentativa %d/%d): %s", url, n + 1, tentativas, e)
            time.sleep(3 * (n + 1))
    raise RuntimeError(f"não foi possível baixar {url}: {erro}")


def _aammdd(data: dt.date) -> str:
    return data.strftime("%y%m%d")


def baixar_debentures(data: dt.date) -> str:
    return _baixar(URL_DEBENTURES.format(aammdd=_aammdd(data)))


def baixar_titulos_publicos(data: dt.date) -> str:
    return _baixar(URL_TITULOS_PUBLICOS.format(aammdd=_aammdd(data)))


# CDI acumulado no ano (% a.a.), série 4389 do SGS/Bacen — usado para converter
# papéis '%' do DI em spread equivalente.
URL_CDI = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.4389/dados/ultimos/1?formato=json"
)


def baixar_cdi() -> float | None:
    try:
        r = requests.get(URL_CDI, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        dados = r.json()
        return float(dados[-1]["valor"].replace(",", "."))
    except Exception as e:
        log.warning("CDI indisponível: %s", e)
        return None


def ultimo_dia_util(referencia: dt.date | None = None) -> dt.date:
    """Último dia útil estritamente anterior à referência (sem calendário de feriados)."""
    d = (referencia or dt.date.today()) - dt.timedelta(days=1)
    while d.weekday() >= 5:  # 5=sáb, 6=dom
        d -= dt.timedelta(days=1)
    return d


def coletar(data: dt.date, dir_bruto: pathlib.Path) -> dict[str, str]:
    """Baixa os arquivos do dia e guarda cópia bruta para auditoria."""
    dir_bruto.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}

    for nome, fn in (
        ("debentures", baixar_debentures),
        ("titulos_publicos", baixar_titulos_publicos),
    ):
        try:
            texto = fn(data)
        except FonteIndisponivel as e:
            log.warning("%s indisponível para %s: %s", nome, data, e)
            continue
        out[nome] = texto
        destino = dir_bruto / f"{data.isoformat()}_{nome}.txt"
        destino.write_text(texto, encoding="utf-8")
        log.info("%s: %d linhas -> %s", nome, texto.count("\n"), destino)

    if "debentures" not in out:
        raise FonteIndisponivel(
            f"arquivo de debêntures não publicado para {data} "
            "(provável feriado ou dia sem divulgação)"
        )
    return out


# ========================================================================
# parser
# ========================================================================
VAZIOS = {"", "--", "-", "N/A", "n/d"}

COLUNAS_DEB = [
    "codigo", "nome", "vencimento", "indice", "taxa_compra", "taxa_venda",
    "taxa_indicativa", "desvio_padrao", "intervalo_min", "intervalo_max",
    "pu", "pct_pu_par", "duration_du", "pct_reune", "ref_ntnb",
]


def num(v: str | None) -> float | None:
    """'1.082,256172' ou '1082,256172' -> float. Vazios viram None."""
    if v is None:
        return None
    v = v.strip()
    if v in VAZIOS:
        return None
    v = v.replace("%", "").strip()
    # separador de milhar é ponto, decimal é vírgula
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def data_br(v: str | None) -> dt.date | None:
    if not v or v.strip() in VAZIOS:
        return None
    v = v.strip()
    # O arquivo de debentures usa dd/mm/aaaa; o de titulos publicos usa aaaammdd.
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- indexador

RE_SPREAD = re.compile(r"([+\-])\s*([\d.,]+)\s*%?")
RE_PCT_DI = re.compile(r"([\d.,]+)\s*%?\s*d[oe]\s*(DI|CDI)", re.I)


def classificar_indice(bruto: str) -> tuple[str, float | None]:
    """Devolve (familia, taxa_de_emissao). Família em {DI_SPREAD, DI_PCT, IPCA, IGPM, PRE, OUTRO}."""
    s = (bruto or "").strip()
    su = s.upper()

    if RE_PCT_DI.search(su):
        m = RE_PCT_DI.search(su)
        return "DI_PCT", num(m.group(1))
    if "IPCA" in su:
        m = RE_SPREAD.search(su)
        return "IPCA", num(m.group(2)) if m else None
    if "IGP" in su:
        m = RE_SPREAD.search(su)
        return "IGPM", num(m.group(2)) if m else None
    if "DI" in su or "CDI" in su:
        m = RE_SPREAD.search(su)
        return "DI_SPREAD", num(m.group(2)) if m else None
    if "PRE" in su or "PRÉ" in su or su in VAZIOS:
        return "PRE", None
    return "OUTRO", None


RE_MARCACOES = re.compile(r"\s*\(\*+\)\s*")


def limpar_emissor(nome: str) -> tuple[str, list[str]]:
    marcas = RE_MARCACOES.findall(nome or "")
    limpo = RE_MARCACOES.sub(" ", nome or "").strip()
    limpo = re.sub(r"\s+", " ", limpo)
    return limpo, [m.strip() for m in marcas]


@dataclass
class Debenture:
    codigo: str
    emissor: str
    marcacoes: list[str] = field(default_factory=list)
    vencimento: dt.date | None = None
    indice_bruto: str = ""
    familia: str = "OUTRO"
    taxa_emissao: float | None = None
    taxa_compra: float | None = None
    taxa_venda: float | None = None
    taxa_indicativa: float | None = None
    desvio_padrao: float | None = None
    intervalo_min: float | None = None
    intervalo_max: float | None = None
    pu: float | None = None
    pct_pu_par: float | None = None
    duration_du: float | None = None
    duration_anos: float | None = None
    pct_reune: float | None = None
    ref_ntnb: dt.date | None = None
    bid_ask_bps: float | None = None

    def dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, dt.date):
                d[k] = v.isoformat()
        return d


def parse_debentures(texto: str) -> list[Debenture]:
    linhas = [l for l in texto.splitlines() if "@" in l]
    saida: list[Debenture] = []

    for linha in linhas:
        campos = linha.split("@")
        if len(campos) < 13:
            continue
        codigo = campos[0].strip()
        # cabeçalho e rodapés
        if not re.fullmatch(r"[A-Z0-9]{4,8}", codigo):
            continue

        campos = campos + [""] * (15 - len(campos))
        v = dict(zip(COLUNAS_DEB, [c.strip() for c in campos[:15]]))

        emissor, marcas = limpar_emissor(v["nome"])
        familia, tx_emissao = classificar_indice(v["indice"])
        dur_du = num(v["duration_du"])
        compra, venda = num(v["taxa_compra"]), num(v["taxa_venda"])

        saida.append(
            Debenture(
                codigo=codigo,
                emissor=emissor,
                marcacoes=marcas,
                vencimento=data_br(v["vencimento"]),
                indice_bruto=v["indice"],
                familia=familia,
                taxa_emissao=tx_emissao,
                taxa_compra=compra,
                taxa_venda=venda,
                taxa_indicativa=num(v["taxa_indicativa"]),
                desvio_padrao=num(v["desvio_padrao"]),
                intervalo_min=num(v["intervalo_min"]),
                intervalo_max=num(v["intervalo_max"]),
                pu=num(v["pu"]),
                pct_pu_par=num(v["pct_pu_par"]),
                duration_du=dur_du,
                duration_anos=round(dur_du / 252, 3) if dur_du else None,
                pct_reune=num(v["pct_reune"]),
                ref_ntnb=data_br(v["ref_ntnb"]),
                # bid-ask em taxa: compra (yield maior) - venda (yield menor)
                bid_ask_bps=(
                    round((compra - venda) * 100, 1)
                    if compra is not None and venda is not None
                    else None
                ),
            )
        )
    return saida


# ---------------------------------------------------------- títulos públicos

@dataclass
class TituloPublico:
    titulo: str
    vencimento: dt.date | None
    taxa_indicativa: float | None
    pu: float | None

    def dict(self) -> dict:
        d = asdict(self)
        if isinstance(d["vencimento"], dt.date):
            d["vencimento"] = d["vencimento"].isoformat()
        return d


def parse_titulos_publicos(texto: str) -> list[TituloPublico]:
    saida: list[TituloPublico] = []
    for linha in texto.splitlines():
        if "@" not in linha:
            continue
        c = [x.strip() for x in linha.split("@")]
        if len(c) < 9:
            continue
        titulo = c[0].upper()
        if titulo not in {"LTN", "LFT", "NTN-B", "NTN-C", "NTN-F"}:
            continue
        saida.append(
            TituloPublico(
                titulo=titulo,
                vencimento=data_br(c[4]),
                taxa_indicativa=num(c[7]),
                pu=num(c[8]),
            )
        )
    return saida


def curva_ntnb(titulos: list[TituloPublico]) -> dict[str, float]:
    """{'2035-05-15': 7.12, ...} — taxa real indicativa por vencimento de NTN-B."""
    return {
        t.vencimento.isoformat(): t.taxa_indicativa
        for t in titulos
        if t.titulo == "NTN-B" and t.vencimento and t.taxa_indicativa is not None
    }


# ========================================================================
# metricas
# ========================================================================
BUCKETS = [
    ("0-2a", 0.0, 2.0),
    ("2-4a", 2.0, 4.0),
    ("4-6a", 4.0, 6.0),
    ("6a+", 6.0, 99.0),
]


def bucket_duration(anos: float | None) -> str | None:
    if anos is None:
        return None
    for nome, lo, hi in BUCKETS:
        if lo <= anos < hi:
            return nome
    return "6a+"


def _interpolar_ntnb(curva: dict[str, float], venc_alvo: dt.date) -> float | None:
    """Interpola linearmente a taxa da NTN-B para um vencimento arbitrário."""
    if not curva:
        return None
    pares = sorted((dt.date.fromisoformat(k), v) for k, v in curva.items())
    datas = [p[0] for p in pares]
    if venc_alvo <= datas[0]:
        return pares[0][1]
    if venc_alvo >= datas[-1]:
        return pares[-1][1]
    i = bisect_left(datas, venc_alvo)
    d0, t0 = pares[i - 1]
    d1, t1 = pares[i]
    if d1 == d0:
        return t0
    w = (venc_alvo - d0).days / (d1 - d0).days
    return t0 + w * (t1 - t0)


# Aliquota de IR usada para trazer papel isento a base comparavel com titulo
# tributado. A quase totalidade do IPCA+ do secundario e incentivada (Lei
# 12.431): sem o gross-up, o spread contra a NTN-B sai negativo e nao se compara
# com o DI+. Guardamos as duas medidas e o painel mostra a bruta ao lado.
ALIQUOTA_IR = 0.15


def calcular_spread(deb, curva_ntnb: dict[str, float], cdi: float | None):
    """Devolve (spread_bps, spread_bruto_bps, benchmark_usado).

    spread_bps        -> medida principal, comparavel entre familias
    spread_bruto_bps  -> como o papel e cotado, sem ajuste de imposto
    """
    tx = deb.taxa_indicativa
    if tx is None:
        return None, None, "sem_taxa"

    if deb.familia == "DI_SPREAD":
        s = round(tx * 100, 1)
        return s, s, "DI"

    if deb.familia == "IPCA":
        ref = None
        if deb.ref_ntnb:
            ref = curva_ntnb.get(deb.ref_ntnb.isoformat())
            if ref is None:
                ref = _interpolar_ntnb(curva_ntnb, deb.ref_ntnb)
        if ref is None and deb.vencimento:
            ref = _interpolar_ntnb(curva_ntnb, deb.vencimento)
        if ref is None:
            return None, None, "sem_ntnb"
        bruto = round((tx - ref) * 100, 1)
        cheio = round((tx / (1 - ALIQUOTA_IR) - ref) * 100, 1)
        return cheio, bruto, f"NTN-B {ref:.2f}% (gross-up {ALIQUOTA_IR:.0%})"

    if deb.familia == "DI_PCT":
        if cdi is None:
            return None, None, "sem_cdi"
        s = round(cdi * (tx / 100 - 1) * 100, 1)
        return s, s, f"%CDI (CDI={cdi:.2f}%)"

    return None, None, "sem_benchmark"


def escore_liquidez(deb) -> float | None:
    """
    0 a 100 — quanto maior, mais confiável/negociável o papel parece.
    Composto por bid-ask, dispersão entre informantes e presença no Reune.
    """
    partes, pesos = [], []

    if deb.bid_ask_bps is not None:
        # 10 bps ou menos = ótimo; 150 bps ou mais = péssimo
        s = max(0.0, min(1.0, (150 - abs(deb.bid_ask_bps)) / 140))
        partes.append(s); pesos.append(0.45)

    if deb.desvio_padrao is not None:
        # desvio-padrão em pontos de taxa: 0,02 ótimo; 0,50 ruim
        s = max(0.0, min(1.0, (0.50 - deb.desvio_padrao) / 0.48))
        partes.append(s); pesos.append(0.35)

    if deb.pct_reune is not None:
        partes.append(max(0.0, min(1.0, deb.pct_reune / 60))); pesos.append(0.20)

    if not partes:
        return None
    return round(100 * sum(p * w for p, w in zip(partes, pesos)) / sum(pesos), 1)


def enriquecer(debentures, curva_ntnb: dict[str, float], cdi: float | None) -> list[dict]:
    """Snapshot do dia com spread, bucket e liquidez por papel."""
    linhas = []
    for d in debentures:
        spread, bruto, bench = calcular_spread(d, curva_ntnb, cdi)
        r = d.dict()
        r["spread_bps"] = spread
        r["spread_bruto_bps"] = bruto
        r["benchmark"] = bench
        r["bucket"] = bucket_duration(d.duration_anos)
        r["liquidez"] = escore_liquidez(d)
        linhas.append(r)

    # percentil do spread dentro do par (família + bucket de duration)
    grupos: dict[tuple, list[float]] = {}
    for r in linhas:
        if r["spread_bps"] is None or r["bucket"] is None:
            continue
        grupos.setdefault((r["familia"], r["bucket"]), []).append(r["spread_bps"])
    for g in grupos.values():
        g.sort()

    # percentil do % PU par no mesmo grupo. Um corte absoluto nao serve: papel
    # DI+ tem mediana ~100% do par, mas IPCA+ incentivado amortiza e fica em
    # ~93%, entao 63% deles cairiam num gatilho de "PU abaixo de 95".
    grupos_pu: dict[tuple, list[float]] = {}
    for r in linhas:
        if r["pct_pu_par"] is None or r["bucket"] is None:
            continue
        grupos_pu.setdefault((r["familia"], r["bucket"]), []).append(r["pct_pu_par"])
    for g in grupos_pu.values():
        g.sort()
    for r in linhas:
        g = grupos_pu.get((r["familia"], r["bucket"]))
        if not g or r["pct_pu_par"] is None or len(g) < 8:
            r["percentil_pu"] = None
        else:
            r["percentil_pu"] = round(100 * bisect_left(g, r["pct_pu_par"]) / len(g), 1)

    for r in linhas:
        chave = (r["familia"], r["bucket"])
        g = grupos.get(chave)
        if not g or r["spread_bps"] is None or len(g) < 5:
            r["percentil_pares"] = None
            r["mediana_pares_bps"] = None
            r["vs_pares_bps"] = None
            continue
        i = bisect_left(g, r["spread_bps"])
        r["percentil_pares"] = round(100 * i / len(g), 1)
        med = statistics.median(g)
        r["mediana_pares_bps"] = round(med, 1)
        r["vs_pares_bps"] = round(r["spread_bps"] - med, 1)

    return linhas


def comparar_historico(hoje: list[dict], historico: dict[str, list[dict]],
                       data: dt.date | None = None) -> list[dict]:
    """
    Acrescenta variações e z-score.
    `historico` = {codigo: [{'data':..., 'spread_bps':..., 'pu':...}, ...]} em ordem cronológica.

    `data` e a data de referencia do snapshot: so entram no calculo pontos
    ESTRITAMENTE ANTERIORES a ela. Sem esse corte, um backfill rodado sobre
    historico ja existente compararia cada dia com pregoes do futuro.
    """
    corte = data.isoformat() if data else None
    for r in hoje:
        serie = historico.get(r["codigo"], [])
        if corte:
            serie = [p for p in serie if p["data"] < corte]
        spreads = [p["spread_bps"] for p in serie if p.get("spread_bps") is not None]
        pus = [p["pu"] for p in serie if p.get("pu") is not None]

        def delta(seq, atual, n):
            if atual is None or len(seq) < n:
                return None
            return round(atual - seq[-n], 1)

        r["d_spread_1d"] = delta(spreads, r["spread_bps"], 1)
        r["d_spread_5d"] = delta(spreads, r["spread_bps"], 5)
        r["d_spread_21d"] = delta(spreads, r["spread_bps"], 21)
        r["d_pu_1d_pct"] = (
            round(100 * (r["pu"] / pus[-1] - 1), 3)
            if r["pu"] and pus and pus[-1] else None
        )

        janela = spreads[-60:]
        if r["spread_bps"] is not None and len(janela) >= 20:
            mu = statistics.fmean(janela)
            sd = statistics.pstdev(janela)
            r["zscore_spread"] = round((r["spread_bps"] - mu) / sd, 2) if sd > 1e-9 else None
            r["media_60d_bps"] = round(mu, 1)
        else:
            r["zscore_spread"] = None
            r["media_60d_bps"] = None
        r["dias_historico"] = len(spreads)
    return hoje


# ------------------------------------------------------------------- sinais

def gerar_sinais(linhas: list[dict], min_liquidez: float = 45.0) -> list[dict]:
    """
    Watchlist de distorções. Um papel só entra se tiver liquidez mínima —
    spread largo em papel ilíquido costuma ser ruído de marcação, não oportunidade.
    """
    for r in linhas:
        sinais = []
        liq = r.get("liquidez")
        liquido = liq is not None and liq >= min_liquidez

        z = r.get("zscore_spread")
        if liquido and z is not None:
            if z >= 1.5:
                sinais.append(("spread_esticado", f"z={z:+.2f} vs 60d — prêmio acima do próprio histórico"))
            elif z <= -1.5:
                sinais.append(("spread_comprimido", f"z={z:+.2f} vs 60d — pouco prêmio vs histórico"))

        vp = r.get("vs_pares_bps")
        pc = r.get("percentil_pares")
        if liquido and vp is not None and pc is not None:
            if pc >= 90:
                sinais.append(("caro_vs_pares", f"{vp:+.0f} bps vs mediana do bucket (p{pc:.0f})"))
            elif pc <= 10:
                sinais.append(("apertado_vs_pares", f"{vp:+.0f} bps vs mediana do bucket (p{pc:.0f})"))

        d1 = r.get("d_spread_1d")
        if liquido and d1 is not None and abs(d1) >= 15:
            sinais.append(
                ("abertura_dia" if d1 > 0 else "fechamento_dia", f"{d1:+.0f} bps no dia")
            )

        d5 = r.get("d_spread_5d")
        if liquido and d5 is not None and abs(d5) >= 40:
            sinais.append(
                ("abertura_semana" if d5 > 0 else "fechamento_semana", f"{d5:+.0f} bps em 5 pregões")
            )

        par = r.get("pct_pu_par")
        ppu = r.get("percentil_pu")
        if par is not None and ppu is not None and ppu <= 10:
            sinais.append(
                ("desconto_pu", f"PU a {par:.1f}% do par — decil mais baixo do bucket (p{ppu:.0f})")
            )

        if liq is not None and liq < 25:
            sinais.append(("baixa_liquidez", "bid-ask largo / dispersão alta — trate a marcação com ressalva"))

        r["sinais"] = [{"tipo": t, "detalhe": d} for t, d in sinais]
    return linhas


def destaques(linhas: list[dict], n: int = 15) -> dict:
    """Rankings do dia para o topo do dashboard."""
    def top(chave, reverse=True, filtro=None):
        base = [r for r in linhas if r.get(chave) is not None]
        if filtro:
            base = [r for r in base if filtro(r)]
        return sorted(base, key=lambda r: r[chave], reverse=reverse)[:n]

    liq = lambda r: (r.get("liquidez") or 0) >= 45

    return {
        "maior_abertura_dia": top("d_spread_1d", True, liq),
        "maior_fechamento_dia": top("d_spread_1d", False, liq),
        "maior_abertura_semana": top("d_spread_5d", True, liq),
        "z_mais_alto": top("zscore_spread", True, liq),
        "z_mais_baixo": top("zscore_spread", False, liq),
        "maior_desconto_par": top("pct_pu_par", False),
        "mais_liquidos": top("liquidez", True),
    }


def agregados(linhas: list[dict]) -> dict:
    """Termômetro do mercado: mediana de spread por família e bucket."""
    out: dict[str, dict] = {}
    for fam in ("DI_SPREAD", "IPCA", "DI_PCT"):
        por_bucket = {}
        for nome, _, _ in BUCKETS:
            vals = [
                r["spread_bps"] for r in linhas
                if r["familia"] == fam and r["bucket"] == nome and r["spread_bps"] is not None
            ]
            if len(vals) >= 3:
                por_bucket[nome] = {
                    "n": len(vals),
                    "mediana_bps": round(statistics.median(vals), 1),
                    "p25_bps": round(sorted(vals)[len(vals) // 4], 1),
                    "p75_bps": round(sorted(vals)[3 * len(vals) // 4], 1),
                }
        if por_bucket:
            out[fam] = por_bucket
    return out


# ========================================================================
# pipeline
# ========================================================================
# O historico vive em UM CSV POR PREGAO, nao num JSON unico reescrito todo dia.
# Um arquivo unico de ~5 MB regravado diariamente empilharia mais de 1 GB de
# objetos no git em um ano; assim cada dia acrescenta ~45 KB e o repositorio
# cresce ~11 MB por ano.
SERIE = "serie"
COLS_SERIE = ("codigo", "spread_bps", "spread_bruto_bps", "pu", "taxa")


def carregar_serie() -> dict[str, list[dict]]:
    """Reconstroi {codigo: [pontos em ordem cronologica]} a partir de dados/serie/*.csv."""
    import csv

    dir_serie = DADOS / SERIE
    serie: dict[str, list[dict]] = {}
    if not dir_serie.exists():
        # migracao do formato antigo, se ainda existir
        antigo = DADOS / "serie.json"
        if antigo.exists():
            log.info("migrando serie.json para CSV por pregao")
            return json.loads(antigo.read_text(encoding="utf-8"))
        return serie

    for arq in sorted(dir_serie.glob("*.csv"))[-MAX_HISTORICO:]:
        data = arq.stem
        with arq.open(encoding="utf-8", newline="") as fh:
            for linha in csv.DictReader(fh):
                p = {"data": data}
                for c in COLS_SERIE[1:]:
                    v = linha.get(c, "")
                    p[c] = float(v) if v not in ("", None) else None
                serie.setdefault(linha["codigo"], []).append(p)
    log.info("historico: %d papeis em %d pregoes",
             len(serie), len(list(dir_serie.glob("*.csv"))))
    return serie


def gravar_dia(data: dt.date, linhas: list[dict]) -> None:
    """Grava o CSV daquele pregao e poda o que passou da janela."""
    import csv

    dir_serie = DADOS / SERIE
    dir_serie.mkdir(parents=True, exist_ok=True)
    with (dir_serie / f"{data.isoformat()}.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS_SERIE)
        for r in linhas:
            w.writerow([
                r["codigo"],
                "" if r.get("spread_bps") is None else r["spread_bps"],
                "" if r.get("spread_bruto_bps") is None else r["spread_bruto_bps"],
                "" if r.get("pu") is None else r["pu"],
                "" if r.get("taxa_indicativa") is None else r["taxa_indicativa"],
            ])

    antigos = sorted(dir_serie.glob("*.csv"))[:-MAX_HISTORICO]
    for a in antigos:
        a.unlink()
        log.info("podado do historico: %s", a.name)


def gravar_serie(serie: dict[str, list[dict]]) -> None:
    """Mantido so para remover o serie.json legado depois da migracao."""
    antigo = DADOS / "serie.json"
    if antigo.exists() and (DADOS / SERIE).exists():
        antigo.unlink()
        log.info("serie.json antigo removido — historico agora em dados/serie/")


def processar(data: dt.date, serie: dict[str, list[dict]]) -> dict:
    brutos = coletar(data, DADOS / "bruto")

    debs = parse_debentures(brutos["debentures"])
    log.info("%s: %d debêntures parseadas", data, len(debs))
    if len(debs) < 100:
        raise RuntimeError(f"apenas {len(debs)} papéis — arquivo suspeito, abortando")

    curva = {}
    if "titulos_publicos" in brutos:
        tps = parse_titulos_publicos(brutos["titulos_publicos"])
        curva = curva_ntnb(tps)
        log.info("curva NTN-B: %d vértices", len(curva))

    cdi = baixar_cdi()

    linhas = enriquecer(debs, curva, cdi)
    # histórico: só pregões anteriores a `data`
    linhas = comparar_historico(linhas, serie, data)
    linhas = gerar_sinais(linhas)

    # alimenta a série: em memoria (para os proximos dias do backfill) e em disco
    for r in linhas:
        pontos = serie.setdefault(r["codigo"], [])
        pontos[:] = [p for p in pontos if p["data"] != data.isoformat()]
        pontos.append(
            {
                "data": data.isoformat(),
                "spread_bps": r["spread_bps"],
                "spread_bruto_bps": r.get("spread_bruto_bps"),
                "pu": r["pu"],
                "taxa": r["taxa_indicativa"],
            }
        )
        pontos.sort(key=lambda p: p["data"])
    gravar_dia(data, linhas)

    com_spread = [r for r in linhas if r["spread_bps"] is not None]
    snapshot = {
        "data_referencia": data.isoformat(),
        "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cdi_aa": cdi,
        "cobertura": {
            "papeis": len(linhas),
            "com_spread": len(com_spread),
            "curva_ntnb_vertices": len(curva),
            "por_familia": {
                f: sum(1 for r in linhas if r["familia"] == f)
                for f in sorted({r["familia"] for r in linhas})
            },
        },
        "agregados": agregados(linhas),
        "destaques": destaques(linhas),
        "papeis": linhas,
    }

    (DADOS / "diario").mkdir(parents=True, exist_ok=True)
    (DADOS / "diario" / f"{data.isoformat()}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    (DADOS / "ultimo.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    return snapshot


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD")
    ap.add_argument("--backfill", type=int, default=0, help="nº de dias úteis para trás")
    a = ap.parse_args(argv)

    DADOS.mkdir(parents=True, exist_ok=True)
    serie = carregar_serie()

    if a.backfill:
        datas, d = [], dt.date.today()
        while len(datas) < a.backfill:
            d = ultimo_dia_util(d)
            datas.append(d)
        datas.reverse()  # cronológico, para o z-score se construir corretamente
    else:
        datas = [dt.date.fromisoformat(a.data) if a.data else ultimo_dia_util()]

    ok = 0
    for d in datas:
        try:
            snap = processar(d, serie)
            ok += 1
            c = snap["cobertura"]
            log.info(
                "%s OK — %d papéis, %d com spread", d, c["papeis"], c["com_spread"]
            )
        except FonteIndisponivel as e:
            log.warning("%s pulado: %s", d, e)
        except Exception as e:
            log.error("%s falhou: %s", d, e)
            if len(datas) == 1:
                gravar_serie(serie)
                return 1

    gravar_serie(serie)
    log.info("concluído: %d/%d dias", ok, len(datas))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
