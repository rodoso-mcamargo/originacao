import pathlib
import re
import sys
import zipfile

import pandas as pd
import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
import fluxo_fundos as ff  # noqa: E402

FIX = RAIZ / "tests" / "fixtures" / "fluxo_fundos"


@pytest.fixture
def cadastro(tmp_path):
    z = tmp_path / "registro_fundo_classe.zip"
    with zipfile.ZipFile(z, "w") as zz:
        for n in ("registro_fundo.csv", "registro_classe.csv"):
            zz.write(FIX / n, n)
    return ff.ler_cadastro(z)


@pytest.fixture
def informe():
    return ff.ler_informe(FIX / "inf_diario.csv")


# --- normalizacao -----------------------------------------------------------
def test_norm_nome_tira_acento_pontuacao_e_sufixo():
    assert ff.norm_nome("Itaú Unibanco Asset Management Ltda.") == "ITAU UNIBANCO ASSET MANAGEMENT"
    assert ff.norm_nome("Sul América Investimentos S.A.") == "SUL AMERICA INVESTIMENTOS"
    assert ff.norm_nome("Cia. Energética Participações S/A") == "CIA ENERGETICA"


def test_cnpj():
    assert ff.so_digitos("00.888.897/0001-31") == "00888897000131"
    assert ff.fmt_cnpj("332266000131") == "00.332.266/0001-31"


# --- classificacao ----------------------------------------------------------
@pytest.mark.parametrize("nome,esperado", [
    ("KINEA INFRA INCENTIVADO FIF RF", "incentivado_infra"),
    ("XP DEBÊNTURES INCENTIVADAS CP", "incentivado_infra"),
    ("FUNDO LEI 12.431 RF", "incentivado_infra"),
    ("ITAÚ RF CRÉDITO PRIVADO", "credito_privado"),
    ("BRADESCO FIF RF CRED PRIV DI", "credito_privado"),
    ("BTG DEBENTURE DI", "credito_privado"),
    ("ITAÚ INSTITUCIONAL RF REFERENCIADO DI", "outros"),
])
def test_classificar(nome, esperado):
    assert ff.classificar(nome) == esperado


def test_excluidos_e_marca():
    assert ff.excluido_por_tipo("Classes de Cotas de Fundos FIDC")
    assert ff.excluido_por_tipo("FII")
    assert not ff.excluido_por_tipo("Classes de Cotas de Fundos FIF", "FUNDO RF CRED PRIV")
    marcas = {"Kinea": re.compile(ff.MARCAS["Kinea"]), "XP": re.compile(ff.MARCAS["XP"])}
    assert ff.marca_gestora("KINEA INVESTIMENTOS LTDA.", marcas) == "Kinea"
    assert ff.marca_gestora("XP ALLOCATION ASSET MANAGEMENT LTDA", marcas) == "XP"
    assert ff.marca_gestora("XPTO GESTORA", marcas) is None


# --- parsing ----------------------------------------------------------------
def test_ler_informe_soma_subclasses(informe):
    b = informe[informe["cnpj"] == "22222222000122"].iloc[0]
    assert b["pl"] == 80_000_000
    assert b["captacao"] == 100_000 and b["resgate"] == 50_000
    assert b["cotistas"] == 8
    assert pd.isna(b["quota"])            # duas subclasses: sem quota unica
    a = informe[informe["cnpj"] == "11111111000111"].iloc[0]
    assert a["quota"] == 1.0


def test_calendario_descarta_dia_avulso(informe):
    datas = ff.calendario(informe)
    assert "2026-10-11" not in datas and len(datas) == 7


def test_cadastro_prefere_classe(cadastro):
    assert cadastro.loc["11111111000111", "nivel"] == "classe"
    assert cadastro.loc["22222222000122", "gestor"] == "KINEA INVESTIMENTOS LTDA."
    assert cadastro.loc["22222222000122", "nome"] == "FUNDO B RENDA FIXA CRÉDITO PRIVADO"


# --- universo ---------------------------------------------------------------
def test_universo(cadastro, informe):
    ult = informe.sort_values("data").drop_duplicates("cnpj", keep="last").set_index("cnpj")
    marcas = {"Kinea": re.compile(ff.MARCAS["Kinea"])}
    diag = {}
    u = ff.montar_universo(cadastro, ult["pl"], {"22222222000122", "44444444000144"}, marcas, diag)
    u = u.set_index("cnpj")
    assert set(u.index) == {"11.111.111/0001-11", "22.222.222/0001-22"}   # sem FIDC, sem PL < 50 mi
    assert u.loc["22.222.222/0001-22", "origem"] == "cda+filtro_nome"
    assert u.loc["22.222.222/0001-22", "gestora_painel"] == "Kinea"
    assert u.loc["11.111.111/0001-11", "classe"] == "incentivado_infra"
    assert u.loc["22.222.222/0001-22", "classe"] == "credito_privado"
    assert diag["universo"]["descartados_fidc_fii_fip"] == 1


# --- calculo e alerta -------------------------------------------------------
def test_calculo_janelas_e_alerta(informe):
    datas = ff.calendario(informe)
    c = ff.calcular(informe[informe["cnpj"] != "33333333000133"], datas).set_index(["cnpj", "data"])
    a = c.loc["11111111000111"]
    assert a.loc["2026-10-02", "pct_pl_d1"] == pytest.approx(-2.0)          # -2 mi / 100 mi
    assert pd.isna(a.loc["2026-10-06", "liq_5d"])                            # so 4 dias de historico
    assert pd.isna(a.loc["2026-10-07", "pct_5d"])                            # sem PL antes da janela
    assert a.loc["2026-10-08", "liq_5d"] == pytest.approx(-10_000_000)
    assert a.loc["2026-10-08", "pct_5d"] == pytest.approx(-10.0)            # base = PL de 01/10
    assert a.loc["2026-10-08", "alerta"] == "5d"
    assert pd.isna(a.loc["2026-10-09", "liq_21d"])
    b = c.loc["22222222000122"]
    assert b.loc["2026-10-09", "liq_5d"] == pytest.approx(250_000)
    assert b.loc["2026-10-09", "alerta"] == ""

    eps = ff.episodios(c.reset_index())
    assert len(eps) == 1
    e = eps.iloc[0]
    assert (e["cnpj"], e["regra"], e["data_inicio"], e["data_fim"], e["dias"]) == \
           ("11111111000111", "resgate_5d", "2026-10-08", "2026-10-09", 2)


def test_agregado_por_gestor(informe, cadastro):
    datas = ff.calendario(informe)
    inf = informe[informe["cnpj"] != "33333333000133"]
    c = ff.calcular(inf, datas)
    ult = inf.sort_values("data").drop_duplicates("cnpj", keep="last").set_index("cnpj")
    u = ff.montar_universo(cadastro, ult["pl"], set(), {}, {})
    g = ff.agregar(c, u)
    tot = g[(g["data"] == "2026-10-08") & (g["gestor"] == "TOTAL") & (g["classe"] == "todas")].iloc[0]
    assert tot["n_fundos"] == 2
    assert tot["liq_5d"] == pytest.approx(-10_000_000 + 250_000)
    # gestor com uma classe so nao repete a linha "todas"
    assert g[(g["gestor"] == "GESTORA ALFA LTDA") & (g["classe"] == "todas")].empty


def test_em_alerta_corte():
    assert ff.em_alerta(-1, -5.01, 5.0)
    assert not ff.em_alerta(-1, -5.0, 5.0)
    assert not ff.em_alerta(1, 6.0, 5.0)
    assert not ff.em_alerta(None, -9.0, 5.0)


# --- regra 8: campo vazio na fonte fica null --------------------------------
@pytest.fixture
def informe_vazios():
    return ff.ler_informe(FIX / "inf_diario_vazios.csv")


def test_vazio_nao_vira_zero_na_leitura(informe_vazios):
    i = informe_vazios.set_index(["cnpj", "data"])
    c = i.loc[("66666666000166", "2026-10-05")]
    assert pd.isna(c["captacao"]) and c["resgate"] == 1_000_000
    c = i.loc[("66666666000166", "2026-10-06")]
    assert pd.isna(c["captacao"]) and pd.isna(c["resgate"])
    d = i.loc[("77777777000177", "2026-10-02")]           # SUB2 sem resgate: total da classe null
    assert pd.isna(d["resgate"]) and d["captacao"] == 100_000 and d["pl"] == 80_000_000
    assert i.loc[("77777777000177", "2026-10-05"), "resgate"] == 50_000


def test_liquido_e_janelas_null_com_fluxo_vazio(informe_vazios):
    datas = ff.calendario(informe_vazios)
    c = ff.calcular(informe_vazios, datas).set_index(["cnpj", "data"])
    so_um = c.loc[("66666666000166", "2026-10-05")]          # so captacao vazia
    assert pd.isna(so_um["liquido"]) and pd.isna(so_um["pct_pl_d1"])
    ambos = c.loc[("66666666000166", "2026-10-06")]          # captacao e resgate vazios
    assert pd.isna(ambos["liquido"]) and pd.isna(ambos["pct_pl_d1"])
    assert c.loc[("66666666000166", "2026-10-02"), "liquido"] == -1_000_000
    # janelas de 5 du que contem 05/10 ou 06/10 ficam null (nao somam zero no lugar)
    assert pd.isna(c.loc[("66666666000166", "2026-10-09"), "liq_5d"])
    assert pd.isna(c.loc[("66666666000166", "2026-10-09"), "pct_5d"])
    assert c.loc[("66666666000166", "2026-10-09"), "alerta"] == ""
    d = c.loc["77777777000177"]
    assert pd.isna(d.loc["2026-10-02", "liquido"])
    assert pd.isna(d.loc["2026-10-08", "liq_5d"])            # janela 02..08 contem 02/10
    assert d.loc["2026-10-09", "liq_5d"] == pytest.approx(250_000)   # janela 05..09 completa
    # o arquivo diario grava vazio, nao 0
    g = ff.arred(c.reset_index())
    assert pd.isna(g.loc[(g["cnpj"] == "66666666000166") & (g["data"] == "2026-10-06"), "liquido"]).all()


def test_por_gestor_particionado_por_mes(tmp_path):
    def linhas(datas, v):
        return pd.DataFrame([{"data": d, "gestor": "G", "classe": "todas", "liquido": v} for d in datas])
    ff.gravar_por_gestor(linhas(["2026-09-29", "2026-09-30", "2026-10-01"], 1), ["2026-09-29", "2026-09-30", "2026-10-01"], tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09.csv", "2026-10.csv"]
    # rodada seguinte so recalcula outubro: setembro fica intacto, outubro e substituido
    antes = (tmp_path / "2026-09.csv").read_bytes()
    ff.gravar_por_gestor(linhas(["2026-10-01", "2026-10-02"], 2), ["2026-10-01", "2026-10-02"], tmp_path)
    assert (tmp_path / "2026-09.csv").read_bytes() == antes
    out = pd.read_csv(tmp_path / "2026-10.csv")
    assert list(out["data"]) == ["2026-10-01", "2026-10-02"] and set(out["liquido"]) == {2}
