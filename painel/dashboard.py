"""
Gera o HTML do dashboard a partir de dados/ultimo.json e dados/serie.json.

    python -m coletor.dashboard --saida dashboard.html
    python -m coletor.dashboard --demo   # marca o cabeçalho como dados simulados

O arquivo resultante é autocontido (dados embutidos) e vai para o Artifact.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import statistics
import sys
from bisect import bisect_left as _bisect

RAIZ = pathlib.Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

ROTULO_FAMILIA = {
    "DI_SPREAD": "DI +",
    "IPCA": "IPCA +",
    "DI_PCT": "% do DI",
    "IGPM": "IGP-M +",
    "PRE": "Pré",
    "OUTRO": "Outro",
}

ROTULO_SINAL = {
    "spread_esticado": "spread esticado",
    "spread_comprimido": "spread comprimido",
    "caro_vs_pares": "caro vs pares",
    "apertado_vs_pares": "apertado vs pares",
    "abertura_dia": "abriu no dia",
    "fechamento_dia": "fechou no dia",
    "abertura_semana": "abriu na semana",
    "fechamento_semana": "fechou na semana",
    "desconto_pu": "desconto no PU",
    "baixa_liquidez": "baixa liquidez",
}

# sinais que sinalizam prêmio (oportunidade de compra) vs. compressão
SINAL_TOM = {
    "spread_esticado": "abre", "caro_vs_pares": "abre",
    "abertura_dia": "abre", "abertura_semana": "abre", "desconto_pu": "abre",
    "spread_comprimido": "fecha", "apertado_vs_pares": "fecha",
    "fechamento_dia": "fecha", "fechamento_semana": "fecha",
    "baixa_liquidez": "alerta",
}

CAMPOS_TABELA = [
    "codigo", "emissor", "familia", "indice_bruto", "taxa_emissao",
    "vencimento", "duration_anos",
    "spread_bps", "taxa_indicativa", "pu", "pct_pu_par",
    "bid_ask_bps", "liquidez",
    "d_spread_1d", "d_spread_5d", "d_spread_21d", "zscore_spread", "media_60d_bps",
    "percentil_pares", "vs_pares_bps", "percentil_pu", "bucket", "benchmark",
    "sinais", "dias_historico",
]

# Base considerada estreita: poucos donos E muito concentrada. Num papel assim,
# a saída de um único detentor explica o gap melhor que qualquer tese de crédito.
LIMITE_FUNDOS_ESTREITA = 60
LIMITE_TOP5_ESTREITA = 55.0


def series_por_familia(serie: dict, mapa_familia: dict[str, str], dias: int = 90) -> dict:
    """Mediana diária do spread por família, para o gráfico de evolução."""
    por_data: dict[str, dict[str, list[float]]] = {}
    for codigo, pontos in serie.items():
        fam = mapa_familia.get(codigo)
        if fam not in ("DI_SPREAD", "IPCA", "DI_PCT"):
            continue
        for p in pontos:
            if p.get("spread_bps") is None:
                continue
            por_data.setdefault(p["data"], {}).setdefault(fam, []).append(p["spread_bps"])

    datas = sorted(por_data)[-dias:]
    saida = {"datas": datas, "familias": {}}
    for fam in ("DI_SPREAD", "IPCA", "DI_PCT"):
        linha = []
        for d in datas:
            vals = por_data.get(d, {}).get(fam, [])
            linha.append(round(statistics.median(vals), 1) if len(vals) >= 3 else None)
        if any(v is not None for v in linha):
            saida["familias"][fam] = linha
    return saida


def carregar_cda() -> dict | None:
    """Carteiras dos fundos (CDA/CVM). Opcional: o painel funciona sem."""
    f = DADOS / "cda" / "ultimo.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def resumo_cda(cda: dict) -> dict:
    """Ranking de detentores do universo e os papéis de base mais estreita."""
    pp = cda["por_papel"]

    por_fundo: dict[str, list] = {}
    for v in pp.values():
        for x in v["detentores"]:
            r = por_fundo.setdefault(x["fundo"], [0.0, 0])
            r[0] += x["vl"]
            r[1] += 1
    maiores = sorted(por_fundo.items(), key=lambda kv: -kv[1][0])[:15]

    # Concentração só é informativa em papel com base larga o bastante:
    # com 5 detentores, "top 5" é 100% por definição.
    candidatos = [
        (c, v) for c, v in pp.items()
        if v["n_fundos"] >= 20 and v["vl_total"] >= 100e6 and v["top5_pct"] is not None
    ]
    concentrados = sorted(candidatos, key=lambda kv: -kv[1]["top5_pct"])[:15]

    return {
        "mes": cda["mes_referencia"],
        "papeis_com_dado": cda["papeis_com_dado"],
        "maiores_detentores": [
            {"fundo": f, "vl": round(vl, 2), "papeis": n} for f, (vl, n) in maiores
        ],
        "concentrados": [
            {"codigo": c, "top5_pct": v["top5_pct"], "n_fundos": v["n_fundos"],
             "vl_total": v["vl_total"]} for c, v in concentrados
        ],
    }


def carregar_ratings() -> dict | None:
    """
    Ações de rating levantadas por busca na web pela rotina diária.
    Opcional: sem o arquivo, a seção simplesmente não aparece.

    É material de TERCEIROS, não auditado. O painel apresenta cada item com
    agência, data e link para a fonte, e nunca afirma causa entre a ação de
    rating e o movimento de spread — as duas coisas ficam lado a lado e a
    leitura é de quem olha.
    """
    f = DADOS / "ratings.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def carregar_acoes() -> dict | None:
    """
    Variação da ação do emissor (ou da controladora), coletada pela rotina
    diária. Opcional: sem o arquivo, a seção simplesmente não aparece.

    Entra no painel do MESMO jeito que a ação de rating: é sinal de alerta que
    corrobora, não prova de nada. Queda de ação e abertura de spread ficam lado
    a lado e o painel nunca afirma que uma causou a outra.

    Duas ressalvas que o template repete na tela. A cobertura é parcial por
    razão estrutural, não por falha de mapeamento: boa parte do universo é SPE
    de concessão e saneamento, que não tem equity negociado — emissor sem
    ticker entra com zero neste eixo, igual a emissor sem achado de agência.
    E quando o ticker é da CONTROLADORA, o sinal é mais fraco: SPE com receita
    ring-fenced não se move junto com a holding, então o eixo entra com peso
    reduzido e a tela diz de quem é a ação.
    """
    f = DADOS / "acoes" / "ultimo.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


# ── CRI e CRA (ANBIMA secundário) ──────────────────────────────────────────
# Opcional: sem dados/cri_cra/ultimo.json, payload["cricra"] fica None e o
# template simplesmente não desenha as partes de CRI/CRA.
def carregar_cricra() -> dict | None:
    f = DADOS / "cri_cra" / "ultimo.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def carregar_hist_cricra() -> list[dict]:
    """Linhas diárias acumuladas em dados/cri_cra/AAAA-MM-DD.csv (opcional).
    A página da ANBIMA já traz ~5 pregões por papel no ultimo.json; os CSVs
    diários estendem essa janela conforme a coleta acumula."""
    import csv
    d = DADOS / "cri_cra"
    out: list[dict] = []
    if not d.exists():
        return out
    for arq in sorted(d.glob("*.csv"))[-260:]:
        with arq.open(encoding="utf-8", newline="") as fh:
            for l in csv.DictReader(fh):
                def fl(k):
                    v = l.get(k)
                    try:
                        return float(v) if v not in ("", None) else None
                    except ValueError:
                        return None
                out.append({
                    "codigo": l.get("codigo"), "tipo": l.get("tipo"),
                    "emissor": l.get("emissor"), "securitizadora": l.get("securitizadora"),
                    "vencimento": l.get("vencimento"), "indexador": l.get("indexador") or None,
                    "taxa_indicativa": fl("taxa_indicativa"), "pu": fl("pu"),
                    "pct_pu_par": fl("pct_pu_par"), "duration": fl("duration"),
                    "desvio": fl("desvio"), "referencia": l.get("referencia") or arq.stem,
                })
    return out


def _curvas_ntnb(snapshot: dict, serie: dict) -> dict:
    """NTN-B implícita nas debêntures IPCA+, por data: taxa - spread/100 em cada
    vértice de referência (ref_ntnb). Sem gross-up, igual à metodologia das
    debêntures. Retorna {data: (xs_anos, ys_taxa)}; a data do snapshot usa o
    próprio ultimo.json e as anteriores vêm de dados/serie."""
    vert_de = {p["codigo"]: p.get("ref_ntnb") for p in snapshot["papeis"]
               if p.get("familia") == "IPCA" and p.get("ref_ntnb")}
    por_data: dict[str, dict[str, list[float]]] = {}
    for cod, rn in vert_de.items():
        for x in serie.get(cod, []):
            if x.get("taxa") is None or x.get("spread_bps") is None:
                continue
            por_data.setdefault(x["data"], {}).setdefault(rn, []).append(
                x["taxa"] - x["spread_bps"] / 100.0)
    hoje = snapshot["data_referencia"]
    por_data[hoje] = {}
    for p in snapshot["papeis"]:
        if p.get("codigo") in vert_de and p.get("spread_bps") is not None \
                and p.get("taxa_indicativa") is not None:
            por_data[hoje].setdefault(vert_de[p["codigo"]], []).append(
                p["taxa_indicativa"] - p["spread_bps"] / 100.0)
    curvas = {}
    for d, vs in por_data.items():
        try:
            dd = _dt.date.fromisoformat(d)
        except Exception:
            continue
        pts = []
        for rn, taxas in vs.items():
            try:
                pts.append(((_dt.date.fromisoformat(rn) - dd).days / 365.25,
                            statistics.median(taxas)))
            except Exception:
                continue
        pts.sort()
        if len(pts) >= 3:
            curvas[d] = ([x for x, _ in pts], [y for _, y in pts])
    return curvas


def _familia_cricra(idx: str | None) -> str:
    u = (idx or "").upper()
    if "IPCA" in u:
        return "IPCA"
    if "% DO DI" in u or re.search(r"\d+%\s*DO\s*DI", u):
        return "DI_PCT"
    if "DI" in u and "+" in u:
        return "DI_SPREAD"
    if "DI" in u:
        return "DI_PCT"
    if "PRE" in u or "PRÉ" in u:
        return "PRE"
    return "OUTRO"


def _bucket_dur(dur: float | None) -> str:
    if dur is None:
        return "?"
    if dur < 2:
        return "0-2"
    if dur < 4:
        return "2-4"
    if dur < 7:
        return "4-7"
    return "7+"


def payload_cricra(cc: dict, snapshot: dict, serie: dict | None = None,
                   hist: list | None = None) -> dict:
    """Enriquecimento de CRI/CRA: spread comparável às debêntures, histórico
    curto de spread e score de stress próprio (triagem separada).

    A página da ANBIMA traz ~5 pregões POR PAPEL — o ultimo.json tem várias
    linhas por código. O papel é UM só: a linha de hoje é a mais recente, e as
    anteriores viram o histórico de onde saem Δ1d e Δ da janela.

    Spread por data (duration em dias úteis -> anos, /252):
      CDI+  : taxa*100
      IPCA+ : (taxa - NTN-B implícita daquela data, interpolada na duration)*100
      % DI  : (pct/100 - 1) * CDI corrente * 100
    Pré e sem indexador ficam sem spread (não há curva pré no painel).
    """
    serie = serie or {}
    curvas = _curvas_ntnb(snapshot, serie)
    datas_curva = sorted(curvas)
    cdi = snapshot.get("cdi_aa")

    def ntnb(data: str, anos: float):
        if not datas_curva:
            return None
        i = _bisect(datas_curva, data)
        d = datas_curva[i] if i < len(datas_curva) and datas_curva[i] == data \
            else datas_curva[max(0, i - 1)]
        xs, ys = curvas[d]
        if anos <= xs[0]:
            return ys[0]
        if anos >= xs[-1]:
            return ys[-1]
        j = _bisect(xs, anos)
        x0, x1, y0, y1 = xs[j - 1], xs[j], ys[j - 1], ys[j]
        return y0 + (y1 - y0) * (anos - x0) / (x1 - x0)

    def spread_de(a: dict, fam: str, dur):
        tx = a.get("taxa_indicativa")
        if tx is None:
            return None
        if fam == "DI_SPREAD":
            return round(tx * 100, 1)
        if fam == "IPCA":
            rr = ntnb(a.get("referencia") or cc.get("data_referencia") or "", dur if dur else 4.0)
            return None if rr is None else round((tx - rr) * 100, 1)
        if fam == "DI_PCT" and cdi:
            return round((tx / 100.0 - 1.0) * cdi * 100, 1)
        return None

    # agrupa todas as linhas (ultimo.json + CSVs acumulados) por código e data
    por_cod: dict[str, dict[str, dict]] = {}
    for a in list(hist or []) + list(cc.get("ativos", [])):
        cod, ref = a.get("codigo"), a.get("referencia") or cc.get("data_referencia")
        if not cod or not ref:
            continue
        por_cod.setdefault(cod, {})[ref] = a       # ultimo.json vem por último e prevalece

    rows = []
    for cod, por_data in por_cod.items():
        datas = sorted(por_data)
        a = por_data[datas[-1]]
        if datas[-1] < (cc.get("data_referencia") or datas[-1]):
            # papel que saiu da página: só entra se ainda for o dado mais novo dele
            pass
        fam = _familia_cricra(a.get("indexador")
                              or next((por_data[d].get("indexador") for d in reversed(datas)
                                       if por_data[d].get("indexador")), None))
        dur = None if a.get("duration") is None else round(a["duration"] / 252.0, 2)
        sp = spread_de(a, fam, dur)
        hs = []
        for d in datas:
            x = por_data[d]
            dx = None if x.get("duration") is None else x["duration"] / 252.0
            s = spread_de(x, fam, dx if dx else dur)
            if s is not None:
                hs.append((d, s))
        r = {
            "cod": cod, "tp": a.get("tipo"), "fam": fam, "idx": a.get("indexador"),
            "dev": a.get("emissor"), "sec": a.get("securitizadora"),
            "tx": None if a.get("taxa_indicativa") is None else round(a["taxa_indicativa"], 2),
            "spread": sp,
            "pu": None if a.get("pu") is None else round(a["pu"], 1),
            "par": None if a.get("pct_pu_par") is None else round(a["pct_pu_par"], 1),
            "dur": dur, "venc": a.get("vencimento"), "bucket": _bucket_dur(dur),
            "ref": datas[-1], "desvio": a.get("desvio"),
        }
        if sp is not None and len(hs) >= 2 and hs[-1][0] == datas[-1]:
            sps = [s for _, s in hs]
            r["d1"] = round(sps[-1] - sps[-2], 1)
            r["dj"] = round(sps[-1] - sps[0], 1)          # janela disponível
            r["nj"] = len(sps) - 1                         # pregões na janela
            r["dj_ini"] = hs[0][0]
            if len(sps) >= 5:
                r["s5"] = round(statistics.mean(sps[-5:]), 1)
            if len(sps) >= 26:
                r["b30"] = round(statistics.mean(sps[-26:-21]), 1)
                r["d30"] = round(r["s5"] - r["b30"], 1)
        if r["tx"] is None and r["par"] is None:
            continue                                        # sem nada para mostrar
        rows.append(r)

    # stress score próprio, dentro de (família x bucket de duration):
    #   nível de spread (35%) + acima da mediana dos pares (25%) +
    #   desconto no PU (25%: o maior entre posição nos pares e desconto absoluto)
    #   + abertura na janela curta (15%).
    # Papel SEM taxa indicativa mas com PU (ANBIMA só marca preço — é o caso
    # dos CRAs da Raízen a ~40% do par) entra só pelo PU, com teto de 70.
    grp: dict[tuple, list] = {}
    for r in rows:
        grp.setdefault((r["fam"], r["bucket"]), []).append(r)
    for (_fam, _bk), g in grp.items():
        sps = sorted(x["spread"] for x in g if x["spread"] is not None)
        pars = sorted(x["par"] for x in g if x["par"] is not None)
        med = statistics.median(sps) if sps else None
        for r in g:
            e_pu = 0.0
            if r["par"] is not None:
                e_abs = max(0.0, min(1.0, (97.0 - r["par"]) / 37.0))
                e_rank = 0.0
                if len(pars) >= 5 and r["par"] < 100:
                    e_rank = max(0.0, min(1.0, 1.0 - sum(1 for x in pars if x < r["par"]) / len(pars)))
                e_pu = max(e_abs, 0.5 * e_rank)
            if r["spread"] is None:
                r["score"] = round(70 * e_pu, 1)
                r["so_pu"] = True
                continue
            r["med_pares"] = None if med is None else round(med, 1)
            e_niv = max(0.0, min(1.0, r["spread"] / 800.0))
            e_par = 0.0 if med is None else max(0.0, min(1.0, (r["spread"] - med) / 400.0))
            e_d = max(0.0, min(1.0, (r.get("dj") or 0) / 100.0))
            r["score"] = round(100 * (0.35 * e_niv + 0.25 * e_par + 0.25 * e_pu + 0.15 * e_d), 1)

    datas_all = sorted({r["ref"] for r in rows})
    return {
        "ref": cc.get("data_referencia"),
        "n": len(rows),
        "com_spread": sum(1 for r in rows if r["spread"] is not None),
        "n_cri": sum(1 for r in rows if r["tp"] == "CRI"),
        "n_cra": sum(1 for r in rows if r["tp"] == "CRA"),
        "pregoes_hist": max((r.get("nj", 0) for r in rows), default=0) + 1 if rows else 0,
        "cdi": cdi,
        "curva_ntnb_ref": datas_curva[-1] if datas_curva else None,
        "rows": rows,
    }



# ── grupo econômico ────────────────────────────────────────────────────────
# O mesmo risco aparece com vários nomes: holding e subsidiárias (Hapvida,
# BCBF e Ultra Som), fusões (Marfrig + BRF = MBRF), grafias diferentes entre as
# fontes ("DIAGNÓSTICOS"/"DIAGNÓSTICAS DA AMÉRICA") e nomes truncados pela
# ANBIMA. Triagens e a lista em paralelo agrupam por esta chave. Mapa curado à
# mão: só entra vínculo societário conhecido; o resto cai na chave do nome.
GRUPOS = [
    ("Hapvida", r"^(HAPVIDA|BCBF PARTICIPACOES|ULTRA SOM SERVICOS MEDICOS|NOTRE DAME INTERMEDICA)"),
    ("Simpar", r"^(SIMPAR|JSL|VAMOS LOCACAO|MOVIDA|CS INFRA|CS BRASIL|AUTOMOB)\b"),
    ("MBRF (Marfrig + BRF)", r"^(MARFRIG|BRF|MBRF)\b"),
    ("Dasa", r"^(DASA|DIAGNOSTIC[OA]S DA AMERICA)\b"),
    ("CSN", r"^(CSN|COMPANHIA SIDERURGICA NACIONAL)\b"),
    ("Raízen", r"^RAIZEN\b"),
    ("JBS", r"^(JBS|SEARA ALIMENTOS)\b"),
    ("Localiza", r"^(LOCALIZA|COMPANHIA DE LOCACAO DAS AMERICAS|UNIDAS LOCA)"),
    ("Cogna", r"^COGNA\b"),
    ("Cyrela", r"^CYRELA\b"),
    ("Cury", r"^CURY\b"),
    ("Direcional", r"^DIRECIONAL\b"),
    ("Allos", r"^(ALLOS|ALIANSCE SONAE)\b"),
    ("JHSF", r"^JHSF\b"),
    ("Minerva", r"^MINERVA\b"),
    ("Boa Safra", r"^BOA SAFRA\b"),
    ("BTG Pactual", r"^(BANCO BTG PACTUAL|BTG PACTUAL)\b"),
    ("FS", r"^FS (AGRISOLUTIONS|INDUSTRIA|FLORESTAL)\b"),
    ("SLC Agrícola", r"^SLC AGRICOLA\b"),
    ("Petrobras", r"^PETROLEO BRASILEIRO\b"),
    ("Vibra", r"^(VIBRA|PETROBRAS DISTRIBUIDORA)\b"),
    ("Energisa", r"^ENERGISA\b"),
    ("Enel", r"^(ENEL|ELETROPAULO|AMPLA ENERGIA|COMPANHIA ENERGETICA DO CEARA)\b"),
    ("Arteris", r"^(ARTERIS|AUTOPISTA)\b"),
    ("Iguá", r"^IGUA\b"),
    ("Aegea", r"^(AEGEA|AGUAS DO RIO)\b"),
    ("Motiva (CCR)", r"^(MOTIVA|CCR)\b"),
    ("Cosan", r"^(COSAN|RUMO|COMGAS|COMPANHIA DE GAS DE SAO PAULO)\b"),
    ("Neoenergia", r"^(NEOENERGIA|COMPANHIA DE ELETRICIDADE DO ESTADO DA BAHIA|COELBA|CELPE|COSERN|ELEKTRO)\b"),
    ("CPFL", r"^(CPFL|COMPANHIA PAULISTA DE FORCA E LUZ|RGE SUL)\b"),
    ("Eletrobras (Axia)", r"^(ELETROBRAS|AXIA|CENTRAIS ELETRICAS BRASILEIRAS|FURNAS|COMPANHIA HIDRO ELETRICA DO SAO FRANCISCO|CENTRAIS ELETRICAS DO NORTE)\b"),
    ("Rede D'Or", r"^REDE D ?OR\b"),
]
_GRUPOS_RX = [(g, re.compile(rx)) for g, rx in GRUPOS]


def _chave_nome(nome: str | None) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", nome or "").encode("ascii", "ignore").decode().upper()
    s = re.sub(r"[.,/\-']", " ", s)
    s = re.sub(r"\b(S ?A|LTDA|EIRELI)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def grupo_de(nome: str | None) -> str | None:
    k = _chave_nome(nome)
    for g, rx in _GRUPOS_RX:
        if rx.search(k):
            return g
    return None


def marcar_grupos(papeis: list, rows: list) -> None:
    """Carimba 'gk' (chave de grupo) em debêntures e CRI/CRA, e 'grupo'/'grp'
    (rótulo) quando o vínculo vem do mapa curado. Sem grupo, a chave é o nome
    normalizado; nome truncado pela fonte (>= 16 caracteres) é casado com a
    versão mais longa que começa igual."""
    _base = lambda k: (lambda b: b if len(b) >= 6 else k)(re.sub(r"(\s+(SPE|[IVX]{1,4}|\d{1,2}))+$", "", k))
    chaves = {_base(_chave_nome(p.get("emissor"))) for p in papeis} | {_base(_chave_nome(r.get("dev"))) for r in rows}
    chaves.discard("")
    longas = sorted(chaves, key=len, reverse=True)

    def canon(k: str) -> str:
        # série/SPE numerada do mesmo nome ("AMBIENTAL CEARA 1 SPE", "ALLOS VI",
        # "CURY III") é o mesmo tomador
        base = re.sub(r"(\s+(SPE|[IVX]{1,4}|\d{1,2}))+$", "", k)
        if len(base) >= 6:
            k = base
        if len(k) < 16:
            return k
        for c in longas:
            if len(c) > len(k) and c.startswith(k):
                return c
        return k

    for p in papeis:
        g = grupo_de(p.get("emissor"))
        if g:
            p["grupo"] = g
        p["gk"] = ("G:" + g) if g else canon(_chave_nome(p.get("emissor")))
    for r in rows:
        g = grupo_de(r.get("dev"))
        if g:
            r["grp"] = g
        k = _chave_nome(r.get("dev"))
        r["gk"] = ("G:" + g) if g else (canon(k) if k else "COD:" + r["cod"])


def montar_payload(snapshot: dict, serie: dict, demo: bool) -> dict:
    papeis = [
        {k: p.get(k) for k in CAMPOS_TABELA}
        for p in snapshot["papeis"]
        if p.get("spread_bps") is not None
    ]
    mapa_familia = {p["codigo"]: p["familia"] for p in snapshot["papeis"]}

    # Variação de spread na janela inteira do histórico (~3 meses). O Δ21d
    # sozinho não pega movimento que se acumulou ao longo de meses.
    #
    # E as janelas do gráfico de deslocamento: em vez de um dia contra outro
    # dia, MÉDIA contra MÉDIA. Um pregão isolado carrega o ruído da marcação
    # daquele dia — bid-ask largo, poucos informantes — e vira seta que não
    # existe. Média de 5 pregões contra média de 5 pregões mede deslocamento
    # de nível, que é o que interessa. Os índices são posicionais sobre os
    # pregões COM spread do próprio papel, então um papel que não foi marcado
    # em alguns dias encurta a janela em vez de deslocá-la no calendário.
    for p in papeis:
        pts = [x for x in serie.get(p["codigo"], []) if x.get("spread_bps") is not None]
        sp = [x["spread_bps"] for x in pts]
        if len(pts) >= 40:
            p["spread_ini"] = pts[0]["spread_bps"]
            p["d_spread_janela"] = round(pts[-1]["spread_bps"] - pts[0]["spread_bps"], 1)
            p["pregoes_janela"] = len(pts)

        def media(xs):
            return round(statistics.mean(xs), 1) if xs else None

        if len(sp) >= 5:
            p["spread_5d"] = media(sp[-5:])
        # base A: os 5 pregões que terminam 21 pregões atrás (~30 dias corridos)
        if len(sp) >= 26 and p.get("spread_5d") is not None:
            p["spread_base30"] = media(sp[-26:-21])
            p["d_5v30"] = round(p["spread_5d"] - p["spread_base30"], 1)
        # base B: os 15 pregões que terminam 63 pregões atrás (~3 meses)
        if len(sp) >= 78 and p.get("spread_5d") is not None:
            p["spread_base3m"] = media(sp[-78:-63])
            p["d_5v3m"] = round(p["spread_5d"] - p["spread_base3m"], 1)

    # carteira dos fundos, quando disponivel
    cda = carregar_cda()
    if cda:
        pp = cda["por_papel"]
        for p in papeis:
            v = pp.get(p["codigo"])
            if not v:
                continue
            p["cda_giro"] = round(v["vl_comprado"] + v["vl_vendido"], 2)
            p["cda_vendido"] = v["vl_vendido"]
            p["cda_comprado"] = v["vl_comprado"]
            p["cda_compradores"] = v["compradores"][:5]
            p["cda_fundos"] = v["n_fundos"]
            p["cda_vl_total"] = v["vl_total"]
            p["cda_top5_pct"] = v["top5_pct"]
            p["cda_vendido"] = v["vl_vendido"]
            p["cda_comprado"] = v["vl_comprado"]
            p["cda_detentores"] = v["detentores"][:5]
            p["cda_vendedores"] = v["vendedores"][:5]

    # deltas de cabeçalho: mediana de hoje vs a de ontem
    evol = series_por_familia(serie, mapa_familia)
    resumo = {}
    for fam, linha in evol["familias"].items():
        atuais = [v for v in linha if v is not None]
        resumo[fam] = {
            "mediana": atuais[-1] if atuais else None,
            "delta": (
                round(atuais[-1] - atuais[-2], 1) if len(atuais) >= 2 else None
            ),
            "n": sum(1 for p in papeis if p["familia"] == fam),
        }

    # CRI/CRA (ANBIMA secundário), quando disponivel — spread comparável + triagem própria.
    # Defensivo: qualquer problema no bloco de CRI/CRA NUNCA quebra o painel de
    # debêntures — degrada para None e o template não desenha as partes de CRI/CRA.
    try:
        cc = carregar_cricra()
        cricra = payload_cricra(cc, snapshot, serie, carregar_hist_cricra()) if cc else None
    except Exception:
        cricra = None
    try:
        marcar_grupos(papeis, cricra["rows"] if cricra else [])
    except Exception:
        pass

    return {
        "demo": demo,
        "data": snapshot["data_referencia"],
        "gerado_em": snapshot["gerado_em"],
        "cdi": snapshot.get("cdi_aa"),
        "cobertura": snapshot["cobertura"],
        "agregados": snapshot["agregados"],
        "resumo": resumo,
        "evolucao": evol,
        "cda": resumo_cda(cda) if cda else None,
        "ratings": carregar_ratings(),
        "acoes": carregar_acoes(),
        "cricra": cricra,
        "papeis": papeis,
        "rotulos": {"familia": ROTULO_FAMILIA, "sinal": ROTULO_SINAL, "tom": SINAL_TOM},
    }


def gerar(saida: pathlib.Path, demo: bool = False) -> pathlib.Path:
    snapshot = json.loads((DADOS / "ultimo.json").read_text(encoding="utf-8"))
    serie_f = DADOS / "serie.json"
    serie = json.loads(serie_f.read_text(encoding="utf-8")) if serie_f.exists() else {}
    payload = montar_payload(snapshot, serie, demo)

    template = (pathlib.Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    # ensure_ascii=True: nomes de emissores com acento viram \uXXXX, então o
    # bloco de dados fica ASCII puro e independe de o servidor mandar charset.
    html = template.replace(
        "/*__DADOS__*/null",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
    )
    saida.write_text(html, encoding="utf-8")
    return saida


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default=str(RAIZ / "dashboard.html"))
    ap.add_argument("--demo", action="store_true", help="marca como dados simulados")
    a = ap.parse_args(argv)
    p = gerar(pathlib.Path(a.saida), a.demo)
    print(f"{p} ({p.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
