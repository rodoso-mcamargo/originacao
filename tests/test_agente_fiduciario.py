# -*- coding: utf-8 -*-
"""Testes do levantamento de relatorios de agente fiduciario."""
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import agente_fiduciario as af  # noqa: E402

FXA = RAIZ / "tests" / "fixtures" / "agente_fiduciario"


def test_parse_ot_titulo():
    assert af.parse_ot_titulo("Série 001 335ª Emissão") == ([1], "335")
    assert af.parse_ot_titulo("1ª Série 3ª Emissão") == ([1], "3")
    assert af.parse_ot_titulo("Serie1") == ([1], None)


def test_parse_pt_html():
    from bs4 import BeautifulSoup
    sopa = BeautifulSoup((FXA / "pentagono.html").read_text(encoding="utf-8"), "html.parser")
    nomes = [a.get_text(strip=True) for a in sopa.select("table a[href]")]
    p1 = af.parse_pt_arquivo(nomes[0])
    assert p1 == {"securitizadora": "BARI SECURITIZADORA S.A.", "series": [1], "emissao": "1"}
    p2 = af.parse_pt_arquivo(nomes[1])
    assert p2["series"] == [302, 303, 304] and p2["emissao"] == "1"


def test_parse_vx_operacoes():
    ops = af.parse_vx_operacoes((FXA / "vortx_busca.txt").read_text(encoding="utf-8"))
    assert ops == [{"id": 88685, "nickname": "RAIZEN VIII", "ifCode": "CRA022008NB", "isin": None,
                    "emissao": "38", "serie": "1"}]


def test_analisa_texto_negacao():
    r = af.analisa_texto((FXA / "relatorio.txt").read_text(encoding="utf-8"))
    assert r["flag_alerta"] is True
    assert r["flag_sem_negacao"] is True  # o descumprimento/waiver nao esta negado
    assert "descumpri" in r["trecho"].lower() or "waiver" in r["trecho"].lower()
    assert len(r["trecho"]) <= 300
    so_neg = af.analisa_texto("Não houve inadimplemento no exercício.")
    assert so_neg["flag_alerta"] is True and so_neg["flag_sem_negacao"] is False
    assert af.analisa_texto("Tudo regular.")["flag_alerta"] is False


def test_casa_sec_em_serie():
    alvos = [{"codigo": "C1", "tipo": "CRI", "_sec": "OPEA", "emissao": "335", "serie": 1},
             {"codigo": "C2", "tipo": "CRI", "_sec": "OPEA", "emissao": None, "serie": 1}]
    hits = af.casa_sec_em_serie(alvos, "OPEA SECURITIZADORA S.A.", "335", [1], "CRI")
    assert [h["codigo"] for h in hits] == ["C1"]
    assert af.casa_sec_em_serie(alvos, "OPEA SECURITIZADORA S.A.", "335", [2], "CRI") == []


def test_ficha_adimplente_e_negacao():
    r = af.analisa_texto("Início da rentabilidade Inadimplemento no período Adimplente Risco Corporativo")
    assert r["flag_alerta"] is True and r["flag_sem_negacao"] is False
    r = af.analisa_texto("Inadimplemento no período Inadimplente desde 15/03/2026")
    assert r["flag_sem_negacao"] is True
