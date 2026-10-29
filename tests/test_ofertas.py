import csv
import datetime as dt
import io
import json
import pathlib
import sys
import zipfile

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
import ofertas  # noqa: E402

FIX = RAIZ / "tests" / "fixtures" / "ofertas"
HOJE = dt.date(2026, 10, 29)


def zip_fixture() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("oferta_resolucao_160.csv", (FIX / "oferta_resolucao_160.csv").read_bytes())
        z.writestr("oferta_distribuicao.csv", b"Numero_Processo;Tipo_Ativo\n")
    return buf.getvalue()


# ------------------------------------------------------------ normalizacao
def test_normalizar():
    assert ofertas.normalizar("Aura Almas Mineração S.A.") == "AURA ALMAS MINERACAO"
    assert ofertas.normalizar("JHSF PARTICIPAÇÕES S/A") == "JHSF"
    assert ofertas.normalizar("Cocal Comércio Ltda.") == "COCAL COMERCIO"
    assert ofertas.normalizar("OI S.A. - EM RECUPERACAO JUDICIAL") == "OI"
    assert ofertas.normalizar("Companhia Paulista de Força e Luz") == "PAULISTA DE FORCA E LUZ"


def test_casador_exato_fuzzy_e_generico():
    c = ofertas.Casador(["MRV ENGENHARIA E PARTICIPAÇÕES S.A", "VALE S/A", "UTE GNA I GERACAO DE ENERGIA S.A.",
                         "ATHON GERACAO DISTRIBUIDA S/A", "ENERGIA S.A."])
    assert c.casar("MRV Engenharia e Participações S/A") == ("MRV ENGENHARIA E PARTICIPAÇÕES S.A", "exato")
    assert c.casar("ATHON ENERGIA GERACAO DISTRIBUIDA S.A.")[1] == "fuzzy"
    assert c.casar("FLORESTAL VALE DO CORISCO S.A.") == (None, None)  # 1 palavra so nao basta
    assert c.casar("UTE GNA II GERACAO DE ENERGIA S.A.") == (None, None)  # SPE II != SPE I
    assert c.casar("ATHON GERACAO DISTRIBUIDA VI S.A.") == (None, None)
    assert c.casar("ENERGIA S.A.") == (None, None)  # palavra generica sozinha


def test_partes_devedores():
    assert ofertas.partes_devedores("SEARA ALIMENTOS LTDA.JBS S.A.JBS N.V.") == \
        ["SEARA ALIMENTOS LTDA.", "JBS S.A.", "JBS N.V."]
    p = ofertas.partes_devedores("Devedora: INTEGRADA COOPERATIVA AGROINDUSTRIAL, sociedade cooperativa")
    assert p[0] == "INTEGRADA COOPERATIVA AGROINDUSTRIAL"
    assert ofertas.partes_devedores("Direcional Engenharia S/A")[0] == "Direcional Engenharia S.A."


# ------------------------------------------------------------ parsing / classificacao
def test_ler_e_classificar():
    cols, linhas = ofertas.ler_160(zip_fixture())
    assert "Numero_Requerimento" in cols and len(linhas) == 5
    tipos = [ofertas.tipo_de(r["Valor_Mobiliario"]) for r in linhas]
    assert tipos == ["DEBENTURE", "DEBENTURE", "CRA", None, "DEBENTURE"]
    o = ofertas.normalizar_oferta(linhas[0])
    assert o["numero_requerimento"] == "17244" and o["cnpj_emissor"] == "08213823000107"
    assert o["emissao"] == 2 and o["data_registro"] == "2024-10-16"
    assert o["indexador"] is None and o["remuneracao"] is None  # a fonte nao traz
    assert ofertas.normalizar_oferta(linhas[3]) is None  # FIDC fora do escopo
    assert not ofertas.na_janela(o, HOJE)  # 2024-09: 25 meses antes
    assert ofertas.na_janela(ofertas.normalizar_oferta(linhas[1]), HOJE)
    assert not ofertas.na_janela(ofertas.normalizar_oferta(linhas[4]), HOJE)  # 2023


def test_hash_muda_com_status():
    o = ofertas.normalizar_oferta(ofertas.ler_160(zip_fixture())[1][1])
    h = ofertas.hash_oferta(o)
    assert h == ofertas.hash_oferta(dict(o, no_universo=True))
    assert h != ofertas.hash_oferta(dict(o, status="Oferta Encerrada"))


# ------------------------------------------------------------ primario vs secundario
@pytest.fixture
def serie(tmp_path, monkeypatch):
    d = tmp_path / "serie"
    d.mkdir()
    (d / "2026-08-25.csv").write_text("codigo,spread_bps,spread_bruto_bps,pu,taxa\nZXEI21,0,0,1,8.75\n")
    monkeypatch.setattr(ofertas, "DIR_SERIE", d)
    ofertas._serie_cache.clear()
    return d


def test_comparar(serie):
    oferta = {"tipo": "DEBENTURE", "numero_requerimento": "27643", "data_registro": "2026-08-26",
              "data_requerimento": "2026-08-26", "cnpj_emissor": "40082532000130",
              "nome_emissor": "ZX RENOVAVEIS S/A", "emissao": 1}
    snd = [{"codigo": "ZXEI31", "cnpj_emissor": "40082532000130", "numero_emissao": "1",
            "data_emissao": "2026-08-26", "vencimento": "2041-06-27", "familia": "IPCA",
            "taxa_juros": "10.0", "percentual_indexador": "", "remuneracao": "IPCA + 10%"},
           {"codigo": "ZXEI21", "cnpj_emissor": "40082532000130", "numero_emissao": "2",
            "familia": "IPCA", "data_emissao": "2024-01-01"},
           {"codigo": "ZXEI11", "cnpj_emissor": "40082532000130", "numero_emissao": "3",
            "familia": "IPCA", "data_emissao": "2023-01-01"}]
    papeis = [{"codigo": "ZXEI21", "familia": "IPCA", "duration_anos": 6.0},
              {"codigo": "ZXEI11", "familia": "IPCA", "duration_anos": 1.0}]
    l = ofertas.comparar_primario_secundario([oferta], snd, papeis)
    assert len(l) == 1
    r = l[0]
    assert r["codigo_primario"] == "ZXEI31" and r["duration_ref_fonte"] == "prazo_ate_vencimento"
    assert r["codigo_secundario"] == "ZXEI21"  # duration mais proxima
    assert r["taxa_secundario"] == 8.75 and r["data_taxa_secundario"] == "2026-08-25"  # pregao anterior
    assert r["diferenca_bps"] == pytest.approx(125.0)


def test_comparar_sem_snd_e_sem_taxa(serie):
    o = {"tipo": "DEBENTURE", "numero_requerimento": "1", "data_registro": "2026-08-26",
         "cnpj_emissor": "1", "nome_emissor": "X", "emissao": 9}
    r = ofertas.comparar_primario_secundario([o], [], [])[0]
    assert r.get("diferenca_bps") is None and "fora do universo" in r["observacao"]


def test_rodar_ponta_a_ponta(tmp_path, monkeypatch, serie):
    raiz = tmp_path
    (raiz / "dados" / "snd").mkdir(parents=True)
    with open(raiz / "dados" / "snd" / "caracteristicas.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["codigo", "cnpj_emissor", "emissor", "numero_emissao",
                                          "data_emissao", "vencimento", "familia", "taxa_juros",
                                          "percentual_indexador", "remuneracao"])
        w.writeheader()
        for cod, em, de in (("ZXEI31", "1", "2026-08-26"), ("ZXEI21", "2", "2024-01-01")):
            w.writerow({"codigo": cod, "cnpj_emissor": "40082532000130", "emissor": "ZX RENOVAVEIS SA",
                        "numero_emissao": em, "data_emissao": de, "vencimento": "2041-06-27",
                        "familia": "IPCA", "taxa_juros": "10", "percentual_indexador": ""})
    (raiz / "dados" / "ultimo.json").write_text(json.dumps({"papeis": [
        {"codigo": "ZXEI21", "emissor": "ZX RENOVAVEIS SA", "familia": "IPCA", "duration_anos": 6.0}]}))
    (raiz / "dados" / "cri_cra").mkdir()
    (raiz / "dados" / "cri_cra" / "ultimo.json").write_text(json.dumps({"ativos": [{"emissor": "JBS S.A."}]}))
    monkeypatch.setattr(ofertas, "RAIZ", raiz)
    monkeypatch.setattr(ofertas, "DIR_OUT", raiz / "dados" / "ofertas")
    monkeypatch.setattr(ofertas, "SND_CARAC", raiz / "dados" / "snd" / "caracteristicas.csv")
    monkeypatch.setattr(ofertas, "UNIVERSO", raiz / "dados" / "ultimo.json")
    monkeypatch.setattr(ofertas, "UNIVERSO_CRI", raiz / "dados" / "cri_cra" / "ultimo.json")
    monkeypatch.setattr(ofertas, "baixar_zip", lambda: (zip_fixture(), {"origem": "teste"}))

    s, pvs = ofertas.rodar(HOJE)
    assert s["ofertas_na_janela"] == 2 and s["por_tipo"] == {"DEBENTURE": 1, "CRA": 1}
    assert s["diagnostico"]["descartados"] == {"outro_valor_mobiliario": 1, "fora_janela_24m": 2}
    base = {r["numero_requerimento"]: r for r in csv.DictReader(open(raiz / "dados/ofertas/base.csv", encoding="utf-8"))}
    assert base["27643"]["casamento"] == "cnpj"
    assert base["20502"]["casamento"] == "exato" and base["20502"]["nome_universo"] == "JBS S.A."
    dia = raiz / "dados/ofertas/2026-10-29.csv"
    assert not dia.exists() and "primeira_carga" in s["diagnostico"]  # primeira carga = so a base
    # segundo run no mesmo dado: nada novo
    s2, _ = ofertas.rodar(HOJE)
    assert s2["diagnostico"]["gravados"]["novas_ou_alteradas"] == 0 and not dia.exists()
    # oferta alterada (hash diferente na base anterior) entra no arquivo do dia
    txt = (raiz / "dados/ofertas/base.csv").read_text(encoding="utf-8")
    (raiz / "dados/ofertas/base.csv").write_text(txt.replace(base["20502"]["hash"], "x"), encoding="utf-8")
    s3, _ = ofertas.rodar(HOJE)
    assert s3["diagnostico"]["gravados"]["novas_ou_alteradas"] == 1
    assert [r["numero_requerimento"] for r in csv.DictReader(open(dia, encoding="utf-8"))] == ["20502"]
    zx = [l for l in pvs if l["numero_requerimento"] == "27643"][0]
    assert zx["codigo_primario"] == "ZXEI31" and zx["codigo_secundario"] == "ZXEI21"
    assert zx["diferenca_bps"] == pytest.approx(125.0)
