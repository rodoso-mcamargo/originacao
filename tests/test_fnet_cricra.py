# -*- coding: utf-8 -*-
"""Testes do coletor do Fundos.NET (CRI/CRA)."""
import json
import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import fnet_cricra as fc  # noqa: E402

FX = RAIZ / "tests" / "fixtures" / "fnet_cricra"


@pytest.fixture
def universo():
    return fc.carrega_universo(FX / "universo.json")


@pytest.fixture
def registros():
    d = json.loads((FX / "pesquisa.json").read_text(encoding="utf-8"))
    return [fc.parse_registro(r) for r in d["data"]]


# ---------------------------------------------------------------- parsing
def test_parse_descricao_varias_series():
    d = fc.parse_descricao("OPEA CRI Emissão:376 Série(s):1 (+2) Diversificado 12/2024 BRRBRACRIS10")
    assert d["securitizadora"] == "OPEA"
    assert d["tipo_titulo"] == "CRI"
    assert d["emissao"] == "376" and d["serie"] == "1" and d["series_extra"] == 2
    assert d["devedor_fnet"] == "Diversificado"
    assert d["mes_emissao"] == "2024-12"
    assert d["codigo_fnet"] == "BRRBRACRIS10"


def test_parse_descricao_securitizadora_com_espaco_e_codigo_cetip():
    d = fc.parse_descricao("FORTE CAP CRI Emissão:1 Série:616 Hope 08/2021 25K3493414")
    assert d["securitizadora"] == "FORTE CAP" and d["serie"] == "616" and d["series_extra"] == 0
    assert d["codigo_fnet"] == "25K3493414"


def test_parse_descricao_nao_reconhecida():
    assert fc.parse_descricao("texto qualquer")["tipo_titulo"] is None


def test_parse_registro(registros):
    r = registros[0]
    assert r["id_documento"] and r["url"].endswith(f"exibirDocumento?id={r['id_documento']}")
    assert r["data_entrega"][4] == "-" and "T" in r["data_entrega"]
    assert r["isin"].startswith("BR") and r["codigo"] is None
    assert r["status"] in ("ativo", "cancelado", "inativo")
    assert r["assunto"] is None  # a listagem nao traz assunto
    hab = [x for x in registros if x["securitizadora"] == "HABITASEC"][0]
    assert hab["codigo"] == "25K3493414" and hab["isin"] is None


def test_data_iso():
    assert fc.data_iso("29/10/2026 11:56") == "2026-10-29T11:56"
    assert fc.data_iso("27/02/2026") == "2026-02-27"
    assert fc.data_iso("") is None


# ---------------------------------------------------------------- normalizacao
def test_norm_nome_sufixos():
    assert fc.norm_nome("MRV ENGENHARIA E PARTICIPAÇÕES S.A.") == "MRV ENGENHARIA E"
    assert fc.norm_nome("Raízen Energia S/A") == "RAIZEN ENERGIA"
    assert fc.norm_nome("Coop. Agroindustrial LTDA - EM RECUPERAÇÃO JUDICIAL") == "COOP AGROINDUSTRIAL"


def test_chave_sec():
    assert fc.chave_sec("OPEA SECURITIZADORA S/A") == "OPEA"
    assert fc.chave_sec("ECO SEC AGRO") == fc.chave_sec("ECO SECURITIZADORA DE DIREITOS CREDITÓRIOS DO AGRONEGÓCIO S.A.")
    assert fc.chave_sec("COMPANHIA PROVÍNCIA DE SECURITIZAÇÃO") == "PROVINCIA"
    assert fc.chave_sec("VERT COMPANHIA SECURITIZADORA") == fc.chave_sec("VERTCIASEC") == "VERT"
    assert fc.chave_sec("RBCAPITALRES") == fc.chave_sec("RB CAPITAL INVESTIMENTOS DTVM LTDA")


def test_casa_nome():
    uni = fc.norm_nome("DIRECIONAL ENGENHARIA S.A.")
    assert fc.casa_nome("Direcional Engenhari", uni) == "prefixo"
    assert fc.casa_nome("DIRECIONAL ENGENHARIA S.A.", uni) == "exato"
    assert fc.casa_nome("Diversificado", fc.norm_nome("Diversificado")) is None  # generico
    assert fc.casa_nome("ENERGIA", fc.norm_nome("RAIZEN ENERGIA")) is None
    assert fc.casa_nome("Raizen", fc.norm_nome("RAÍZEN ENERGIA S.A.")) == "fuzzy"


def test_mes_do_codigo():
    assert fc.mes_do_codigo("22E1095384") == (2022, 5)
    assert fc.mes_do_codigo("CRA022008NB") == (2022, None)
    assert fc.mes_do_codigo("XYZ") == (None, None)
    assert fc.data_compativel("2022-06", "22E1095384")  # tolerancia de 1 mes
    assert not fc.data_compativel("2023-06", "22E1095384")
    assert fc.data_compativel("2022-08", "CRA022008NB")
    assert not fc.data_compativel("2020-06", "CRA022008NB")
    assert fc.data_compativel(None, "22E1095384")


# ---------------------------------------------------------------- casamento
def test_universo_dedup(universo):
    assert len(universo) == 5  # CRA022008NB aparece duas vezes
    assert {u["_sec"] for u in universo} == {"OPEA", "HABITASEC"}


def test_casa_por_codigo(registros, universo):
    hab = [x for x in registros if x["securitizadora"] == "HABITASEC"][0]
    hits = fc.casa(hab, universo)
    assert [h["match"] for h in hits] == ["codigo"]
    assert hits[0]["ativo"]["codigo"] == "25K3493414"


def test_casa_serie_devedor_com_filtro_de_data(registros, universo):
    direc = [x for x in registros if "Direcional" in (x["devedor_fnet"] or "")][0]
    hits = fc.casa(direc, universo)
    # serie 2 esta em 1..4; so o codigo de 07/2025 (25G) bate com a data da emissao
    assert [h["ativo"]["codigo"] for h in hits] == ["25G0013406"]
    assert hits[0]["match"] == "serie_devedor"


def test_nao_casa_debenture(registros, universo):
    deb = [x for x in registros if x["tipo_titulo"] == "DEB"][0]
    assert fc.casa(deb, universo) == []


def test_casa_isin_quando_universo_tem():
    uni = [{"codigo": "X1", "isin": "BRAPCSCRIK00", "tipo": "CRI", "_sec": "OPEA", "_serie": 9, "_nome": "Y"}]
    doc = fc.parse_registro({"id": 1, "descricaoFundo": "OPEA CRI Emissão:224 Série(s):1 (+1) MRV XI 09/2023 BRAPCSCRIK00",
                             "status": "AC"})
    assert fc.casa(doc, uni)[0]["match"] == "isin"


# ---------------------------------------------------------------- classificacao
def test_recorta_pauta_prefere_lista():
    txt = (FX / "edital_df.txt").read_text(encoding="utf-8")
    p = fc.recorta_pauta(txt)
    assert p.upper().startswith("ORDEM DO DIA: DELIBERAR")
    assert "vencimento antecipado" not in p  # corta no "Quorum"


def test_classifica_credito():
    txt = (FX / "edital_credito.txt").read_text(encoding="utf-8")
    classe, regras = fc.classifica(fc.recorta_pauta(txt))
    assert classe == "credito" and regras


def test_classifica_sem_pauta_df():
    txt = (FX / "edital_df.txt").read_text(encoding="utf-8")
    assert fc.classifica(fc.recorta_pauta(txt))[0] == "sem_pauta"


@pytest.mark.parametrize("texto,classe", [
    ("deliberar sobre a renúncia ao direito de declarar o vencimento antecipado", "credito"),
    ("aprovar o perdão do pagamento da multa", "credito"),
    ("a não decretação do vencimento antecipado dos CRI", "credito"),
    ("alteração do cronograma de amortização e da data de vencimento", "credito"),
    ("a repactuação da remuneração dos CRI", "credito"),
    ("pedido de recuperação judicial da Devedora", "credito"),
    ("verificação dos covenants financeiros", "credito"),
    ("o inadimplemento da parcela de juros", "credito"),
    ("a substituição do agente fiduciário", "estrutural"),
    ("a substituição da garantia de alienação fiduciária", "estrutural"),
    ("a liberação parcial das garantias", "estrutural"),
    ("o resgate antecipado facultativo total", "estrutural"),
    ("amortização extraordinária parcial", "estrutural"),
    ("a mudança de controle da Devedora", "estrutural"),
    ("aprovar as demonstrações financeiras do patrimônio separado", "sem_pauta"),
    ("", "sem_pauta"),
])
def test_classifica_regex(texto, classe):
    assert fc.classifica(texto)[0] == classe


def test_trecho():
    assert fc.trecho("a" * 400).endswith("...") and len(fc.trecho("a" * 400)) == 300
    assert fc.trecho(None) is None


def test_codigos_citados_resolvem_casamento(universo):
    txt = "FATO RELEVANTE (IF CRA022008NB e CRA022008ND) A OPEA vem a público..."
    assert fc.codigos_citados(txt) == "CRA022008NB;CRA022008ND"
    assert fc.codigos_citados("sem codigo") is None
    doc = fc.parse_registro({"id": 2, "status": "AC",
                             "descricaoFundo": "OPEA CRA Emissão:73 Série(s):1 (+2) RAÍZEN ENERGIA S.A 10/2023 BRAPCSCRA1Y9"})
    assert fc.casa(doc, universo) == []  # 2023 nao bate com o ano do codigo
    doc["codigos_no_texto"] = fc.codigos_citados(txt)
    hits = fc.casa(doc, universo)
    assert [(h["ativo"]["codigo"], h["match"]) for h in hits] == [("CRA022008NB", "codigo")]
