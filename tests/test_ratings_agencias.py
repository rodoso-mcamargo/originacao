# -*- coding: utf-8 -*-
import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
import ratings_agencias as ra  # noqa: E402

FIX = RAIZ / "tests" / "fixtures" / "ratings_agencias"


def ler(nome):
    return (FIX / nome).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def universo():
    return ra.carregar_universo(FIX / "painel.json", FIX / "cri_cra.json")


# --- classificacao -------------------------------------------------------
@pytest.mark.parametrize("titulo,esperado", [
    ("Moody's Local rebaixa rating da XYZ S.A. para A.br", "rebaixamento"),
    ("Fitch Downgrades XYZ to BB; Outlook Negative", "rebaixamento"),
    ("S&P coloca ratings 'brAA' da XYZ em CreditWatch negativo", "em_revisao"),
    ("Moody's Local coloca em revisão para rebaixamento os ratings da XYZ", "em_revisao"),
    ("fitch places xyz on rating watch negative", "em_revisao"),
    ("S&P eleva rating da XYZ para brAAA", "elevacao"),
    ("fitch-upgrades-xyz-to-aa-bra", "elevacao"),
    ("S&P revisa perspectiva da XYZ para negativa; ratings afirmados", "perspectiva_negativa"),
    ("Fitch Affirms XYZ; Revises Outlook to Positive", "perspectiva_positiva"),
    ("Fitch Withdraws XYZ's Ratings", "retirada"),
    ("Moody's Local atribui rating AA.br à XYZ", "atribuicao"),
    ("fitch-assigns-aaasf-bra-rating-to-seller-3-fidc", "atribuicao"),
    ("Fitch Afirma Rating 'AA(bra)' da XYZ; Perspectiva Estável", "afirmacao"),
    ("fitch-affirms-mercado-pago-at-aaa-bra-outlook-stable", "afirmacao"),
    ("Ensino superior privado no Brasil: novo marco regulatório", "relatorio"),
])
def test_classificar(titulo, esperado):
    assert ra.classificar(titulo) == esperado


def test_classificar_usa_paragrafo():
    assert ra.classificar("Comunicado sobre XYZ", "A agência rebaixou o rating da XYZ.") == "rebaixamento"


def test_todas_as_acoes_estao_na_lista():
    assert set(ra.ACOES) == {"rebaixamento", "elevacao", "perspectiva_negativa", "perspectiva_positiva",
                             "em_revisao", "afirmacao", "atribuicao", "retirada", "relatorio"}


# --- normalizacao e casamento --------------------------------------------
@pytest.mark.parametrize("bruto,norm", [
    ("AEGEA SANEAMENTO E PARTICIPAÇÕES S/A", "AEGEA SANEAMENTO E"),
    ("Aura Almas Mineração S.A.", "AURA ALMAS MINERACAO"),
    ("Cia. de Gás de São Paulo - Comgás", "DE GAS DE SAO PAULO COMGAS"),
    ("Oi S.A. - Em Recuperação Judicial", "OI"),
    ("XPTO Ltda.", "XPTO"),
])
def test_normalizar_nome(bruto, norm):
    assert ra.normalizar_nome(bruto) == norm


def test_universo_ignora_nome_so_generico(universo):
    assert "ENERGIA" not in universo          # "ENERGIA S.A." nao entra
    assert "AURA ALMAS MINERACAO" in universo
    assert universo["AEGEA SANEAMENTO E"]["codigos"] == {"AEGPA0", "CRI0200001"}


def test_casar_exato(universo):
    m = ra.casar("Aura Almas Mineração S.A.", universo)
    assert m and m[0]["tipo_match"] == "exato" and m[0]["codigos"] == "AALM12"


def test_casar_fuzzy_marcado(universo):
    m = ra.casar("Aegea Saneamento", universo)
    assert m and m[0]["tipo_match"] == "fuzzy" and m[0]["score"] >= ra.LIMIAR_FUZZY


def test_nunca_casa_por_palavra_generica(universo):
    assert ra.casar("Energia", universo) == []
    assert ra.casar("Saneamento", universo) == []
    assert ra.casar("Energia Sul S.A.", universo) == []   # SUL tambem e generica


def test_grupo_economico(universo, tmp_path):
    g = tmp_path / "grupos.csv"
    g.write_text("controladora;subsidiaria\nAura Minerals Inc;Aura Almas Mineração S.A.\n", encoding="utf-8")
    m = ra.casar("Aura Minerals Inc", universo, ra.carregar_grupos(g))
    assert [x["tipo_match"] for x in m] == ["grupo:AURA MINERALS INC"]


# --- Fitch ---------------------------------------------------------------
def test_parse_fitch_sitemap():
    ent = ra.parse_fitch_sitemap(ler("fitch_sitemap.xml"))
    assert len(ent) == 7 and all("/research/" in e["url"] for e in ent)


def test_fitch_slug():
    r = ra.registro_fitch("https://www.fitchratings.com/research/pt/non-bank-financial-institutions/"
                          "fitch-affirms-mercado-pago-mercado-credito-at-aaa-bra-outlook-stable-26-10-2026")
    assert r["data"] == "2026-10-26"
    assert r["entidade"] == "MERCADO PAGO MERCADO CREDITO"
    assert r["acao"] == "afirmacao" and r["perspectiva"] == "estavel" and r["escala"] == "nacional"
    assert r["nota_nova"] is None   # slug perde +/-

    r = ra.registro_fitch("https://www.fitchratings.com/research/pt/structured-finance/"
                          "fitch-assigns-aaasf-bra-rating-to-second-series-of-senior-quotas-of-seller-3-fidc-28-10-2026")
    assert r["entidade"] == "SELLER 3 FIDC" and r["acao"] == "atribuicao" and r["instrumento"] == "fidc"

    r = ra.registro_fitch("https://www.fitchratings.com/research/pt/infrastructure-project-finance/"
                          "fitch-affirms-aa-bra-from-delta-3s-debentures-outlook-stable-27-10-2026")
    assert r["entidade"] == "DELTA 3S" and r["instrumento"] == "debentures"

    r = ra.registro_fitch("https://www.fitchratings.com/research/pt/banks/"
                          "cooperativa-central-de-credito-do-espirito-santo-sicoob-central-es-27-10-2026")
    assert r["acao"] == "relatorio" and r["entidade"].startswith("COOPERATIVA CENTRAL")


def test_fitch_filtra_brasil_ou_universo(universo, monkeypatch):
    class R:
        status_code = 200
        headers = {}
        text = ler("fitch_sitemap.xml")

    class C:
        def get(self, url):
            return R()

    diag, amostra = ra.novo_diag("fitch"), {}
    regs = ra.coletar_fitch(C(), None, diag, amostra, universo)
    urls = [r["url"] for r in regs]
    assert sum("/research/pt/" in u for u in urls) == 4
    assert any("aura-almas" in u for u in urls)            # internacional, mas casa com o painel
    assert not any("adani" in u or "molymet" in u for u in urls)
    assert diag["descartados"]["fora_do_brasil_e_fora_do_universo"] == 2


# --- Moody's Local (RSS) -------------------------------------------------
def test_parse_rss_moodys():
    itens = ra.parse_rss(ler("moodys_feed.xml"))
    assert len(itens) == 3
    assert itens[0]["data"] == "2026-10-28"           # 21:30 UTC = 18:30 em Brasilia
    assert itens[1]["data"] == "2026-10-28"           # 02:10 UTC do dia 29 = 23:10 do dia 28
    r = ra.registro_texto("moodys_local", itens[0]["titulo"], itens[0]["paragrafo"], itens[0]["url"], itens[0]["data"])
    assert r["acao"] == "rebaixamento"
    assert (r["nota_anterior"], r["nota_nova"]) == ("AA.br", "A+.br")
    assert r["perspectiva"] == "negativa" and r["escala"] == "nacional"
    assert r["entidade"] == "Aura Almas Mineração S.A."
    r = ra.registro_texto("moodys_local", itens[1]["titulo"], itens[1]["paragrafo"], itens[1]["url"], itens[1]["data"])
    assert r["acao"] == "em_revisao" and r["nota_nova"] == "AAA.br" and r["instrumento"] == "debentures"
    assert r["entidade"] == "Aegea Saneamento e Participações S.A."
    r = ra.registro_texto("moodys_local", itens[2]["titulo"], itens[2]["paragrafo"], itens[2]["url"], itens[2]["data"])
    assert r["acao"] == "afirmacao" and r["nota_nova"] == "AA-.br" and r["perspectiva"] == "estavel"


def test_desafio_antirobo_detectado():
    class R:
        status_code = 403
        headers = {"cf-mitigated": "challenge"}
        text = "<title>Just a moment...</title>"
    assert ra.e_desafio_antirobo(R())


# --- S&P (HTML renderizado no servidor) ----------------------------------
def test_sp_next_data():
    regs, info = ra.registros_sp(ra.next_data(ler("sp_ratings_actions.html")))
    assert info["totalNumberOfRecords"] == 1
    a = regs[0]
    assert a["acao"] == "em_revisao" and a["data"] == "2026-10-27"
    assert a["entidade"].startswith("Aegea") and a["nota_nova"] == "brAA+"
    assert a["url"].endswith("/sourceId/999000111")
    assert regs[1]["acao"] == "relatorio" and regs[1]["data"] == "2026-08-20"


def test_custos_elevados_nao_e_elevacao():
    assert ra.classificar("Ensino superior privado", "Novo marco impõe custos elevados ao setor.") == "relatorio"
    assert ra.classificar("Fitch eleva os ratings da XYZ para 'AA(bra)'") == "elevacao"


def test_entidade_em_emissao():
    t = "S&P Global Ratings atribui rating 'brAA' à 5ª emissão de debêntures da Rumo S.A."
    assert ra.extrair_entidade_pt(t) == "Rumo S.A."
    assert ra.extrair_entidade_pt("Fitch Afirma Rating 'AA(bra)' da Copel; Perspectiva Estável") == "Copel"
    assert ra.extrair_entidade_pt("Moody's Local afirma ratings da Energisa S.A. e altera perspectiva") == "Energisa S.A."


def test_fuzzy_nao_casa_entidade_de_outro_pais():
    uni = {"TELEFONICA BRASIL": {"emissor": "TELEFÔNICA BRASIL S/A", "codigos": {"TLPP27"}}}
    assert ra.casar("Telefonica SA", uni) == []
