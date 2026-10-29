import datetime as dt
import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
import snd  # noqa: E402

FIX = RAIZ / "tests" / "fixtures" / "snd"
HOJE = dt.date(2026, 10, 29)


@pytest.fixture(scope="module")
def caracs():
    txt = (FIX / "caracteristicas.txt").read_bytes().decode("cp1252")
    linhas, cols, gerado = snd.parse_caracteristicas(txt)
    return {c["codigo"]: c for c in linhas}, cols, gerado


@pytest.fixture(scope="module")
def agenda():
    txt = (FIX / "agenda.txt").read_bytes().decode("cp1252")
    return snd.parse_agenda(txt)[0]


def test_parse_caracteristicas(caracs):
    por_cod, cols, gerado = caracs
    assert "Unidade Monetaria.1" in cols  # coluna duplicada recebe sufixo
    assert gerado == "2026-10-29T12:05:40"
    c = por_cod["AALM12"]
    assert c["cnpj_emissor"] == "08213823000107"
    assert c["isin"] == "BRAALMDBS017"
    assert c["numero_emissao"] == 2 and c["serie"] == "UNI"
    assert c["data_emissao"] == "2024-10-02" and c["vencimento"] == "2030-10-02"
    assert c["volume_emissao"] == 1_000_000_000
    assert c["saldo_atual"] == 1_000_000_000
    assert c["indexador"] == "DI" and c["familia"] == "DI_SPREAD"
    assert c["remuneracao"] == "DI + 1,6%"
    assert c["garantia"] == "Real" and c["incentivada"] is False
    assert c["agente_fiduciario"] == "OLIVEIRA TRUST DTVM S/A"
    assert c["situacao"] == "Registrado"


def test_parse_ipca_e_vna(caracs):
    c = caracs[0]["ZXEI31"]
    assert c["familia"] == "IPCA" and c["incentivada"] is True
    assert c["remuneracao"] == "IPCA + 10%"
    assert c["valor_nominal_atual"] == pytest.approx(1006.06293)
    assert c["quantidade_mercado"] == 13140


def test_familia_e_remuneracao():
    assert snd.familia_de("DI", 108.0, None) == "DI_PCT"
    assert snd.remuneracao_de("DI", 108.0, None) == "108% DI"
    assert snd.familia_de("DI", 100.0, 0.0) == "DI_SPREAD"
    assert snd.remuneracao_de("DI", 100.0, None) == "100% DI"
    assert snd.familia_de("PRÉ", None, 12.5) == "PRE"
    assert snd.remuneracao_de(None, None, None) is None


def test_parse_agenda(agenda):
    tipos = {e["tipo"] for e in agenda}
    assert {"juros", "amortizacao", "vencimento", "resgate"} <= tipos
    am = [e for e in agenda if e["codigo"] == "AALM12" and e["tipo"] == "amortizacao"]
    assert am[0]["base_fonte"] == "VNA VAR/UNIF"  # coluna duplicada na fonte
    assert am[0]["percentual_ou_valor"] == pytest.approx(14.2857)
    assert am[0]["data_evento"] == "2027-10-02" and am[0]["data_pagamento"] == "2027-10-04"


def test_tipo_evento():
    assert snd.tipo_evento("incorporacao de juros") == "juros"
    assert snd.tipo_evento("AMORTIZACAO EXTRAORDINARIA") == "amortizacao"
    assert snd.tipo_evento("Resgate Total Antecipado") == "resgate"
    assert snd.tipo_evento("evento generico") == "outro"


def test_fluxo_vna_var_soma_o_saldo(caracs, agenda):
    c = caracs[0]["AALM12"]
    ev = [e for e in agenda if e["codigo"] == "AALM12"]
    fluxo, est = snd.fluxo_principal(c, ev)
    assert not est
    assert sum(v for _, v in fluxo) == pytest.approx(c["saldo_atual"])
    assert fluxo[0] == ("2027-10-04", pytest.approx(142_857_000))
    assert len(fluxo) == 7  # 6 amortizacoes + vencimento


def test_fluxo_resgatado_e_sem_saldo():
    zero = {"quantidade_mercado": 0, "saldo_atual": 0.0, "volume_emissao": 1e6}
    assert snd.fluxo_principal(zero, [{"tipo": "vencimento", "data_evento": "2027-01-01"}]) == ([], False)
    sem = {"quantidade_mercado": None, "saldo_atual": None, "volume_emissao": 1e6}
    fl, est = snd.fluxo_principal(sem, [{"tipo": "vencimento", "data_evento": "2027-01-01"}])
    assert est and fl == [("2027-01-01", 1e6)]


def test_fluxo_vne_fix_ipca_marca_estimado():
    c = {"saldo_atual": 2_000.0, "quantidade_mercado": 2, "valor_nominal": 1000.0,
         "quantidade": 2, "indexador": "IPCA"}
    ev = [{"tipo": "amortizacao", "data_evento": "2027-01-01", "percentual_ou_valor": 50.0,
           "base_fonte": "VNE FIX/UNIF"},
          {"tipo": "vencimento", "data_evento": "2028-01-01", "percentual_ou_valor": 0.0}]
    fl, est = snd.fluxo_principal(c, ev)
    assert est and fl == [("2027-01-01", 1000.0), ("2028-01-01", 1000.0)]


def test_vencimentos_por_emissor(caracs, agenda):
    cs = [dict(c, no_universo=c["codigo"] == "AALM12") for c in caracs[0].values()]
    v = {o["cnpj_emissor"]: o for o in snd.vencimentos_por_emissor(cs, agenda, HOJE)}
    a = v["08213823000107"]
    assert a["no_universo"] and a["n_papeis"] == 1 and not a["saldo_estimado"]
    assert a["venc_0_6m"] == 0
    assert a["venc_6_12m"] == pytest.approx(142_857_000, rel=1e-6)   # 2027-10
    assert a["venc_12_18m"] == pytest.approx(142_857_452.38, rel=1e-6)  # 2028-04
    assert a["venc_0_24m"] == pytest.approx(a["venc_6_12m"] + a["venc_12_18m"] + a["venc_18_24m"])


def test_somar_meses():
    assert snd.somar_meses(dt.date(2026, 8, 31), 6) == dt.date(2027, 2, 28)
    assert snd.somar_meses(dt.date(2026, 10, 29), 24) == dt.date(2028, 10, 29)


def test_utilidades():
    assert snd.numero("1.000,50") == 1000.5
    assert snd.numero("000001000000") == 1_000_000
    assert snd.numero(" - ") is None
    assert snd.data_iso("02/10/2030") == "2030-10-02"
    with pytest.raises(ValueError):
        snd.tabela_tab("sem cabecalho", "Codigo do Ativo")
