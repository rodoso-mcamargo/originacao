# -*- coding: utf-8 -*-
"""Relatorios anuais de agente fiduciario (Oliveira Trust, Pentagono, Vortx) dos CRI/CRA do painel.

Fontes (todas publicas, sem login):
  Oliveira Trust  JSON  https://services-ft.oliveiratrust.com.br/app/v1/titulos/documentos
                        ?search=Relatório Anual&tipo_ativo=cri|cra&ano=AAAA&page=N&limit=100
                  (mesmo endpoint da pagina /investidor/documentos do site)
                  arquivo https://services-ft.oliveiratrust.com.br/app/v1/titulos/documentos/download?uid[]=<uid>
  Pentagono       HTML  POST https://www.pentagonotrustee.com.br/RelatoriosAnuais  (Prod=CRI|CRA, Ano=AAAA)
                  arquivo /Docs/RelatAnuais/Relatorio_Anual_<PROD>_<ANO>/<nome>.pdf
  Vortx           busca https://www.vortx.com.br/investidor/dcm?busca=<codigo IF>  (payload RSC da pagina)
                  JSON  https://apis.vortx.com.br/vxsite/api/operacao/<id>/relatorios-anuais

Universo: os CRI/CRA de dados/cri_cra/ultimo.json. Vortx casa pelo codigo IF (mesmo codigo
da ANBIMA). Oliveira Trust e Pentagono publicam por securitizadora + emissao + serie; a
emissao vem do casamento feito pelo fnet_cricra (dados/fnet_cricra/universo.csv). Codigo
sem emissao conhecida fica de fora nesses dois agentes.

Debentures: dependem de dados/snd/caracteristicas.csv (agente de cada emissao). Enquanto
esse arquivo nao existir na main, ficam de fora (registrado no diagnostico).

Do relatorio so guardamos metadados: agente, emissao, serie, ano, url, flag de termos de
alerta e um trecho de ate 300 caracteres. Nenhum PDF e gravado.

Saida: dados/agente_fiduciario/relatorios.csv (chave agente+url) e ultimo.json.
Uso:
  python agente_fiduciario.py                # anos: corrente e anterior
  python agente_fiduciario.py --anos 2024 2025
  python agente_fiduciario.py --inspecionar  # descreve as fontes, nao grava
"""
from __future__ import annotations

import socket

_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _gai(h, p, socket.AF_INET, *a, **k)

import argparse, csv, datetime as dt, html, io, json, logging, os, pathlib, re, sys, time, traceback
import urllib.parse
from collections import Counter

import requests

import fnet_cricra as fc

log = logging.getLogger("agente_fiduciario")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

UA = fc.UA
PAUSA = 1.1
TIMEOUT = 90
DIR_OUT = pathlib.Path("dados/agente_fiduciario")
UNI_FNET = pathlib.Path("dados/fnet_cricra/universo.csv")
SND = pathlib.Path("dados/snd/caracteristicas.csv")

OT_LISTA = "https://services-ft.oliveiratrust.com.br/app/v1/titulos/documentos"
OT_DOWNLOAD = "https://services-ft.oliveiratrust.com.br/app/v1/titulos/documentos/download?uid[]={uid}"
PT_BASE = "https://www.pentagonotrustee.com.br"
PT_LISTA = PT_BASE + "/RelatoriosAnuais"
VX_BUSCA = "https://www.vortx.com.br/investidor/dcm"
VX_OPERACAO = "https://www.vortx.com.br/investidor/dcm/operacao?id={id}"
VX_RELATORIOS = "https://apis.vortx.com.br/vxsite/api/operacao/{id}/relatorios-anuais"

TERMOS = re.compile(r"inadimpl\w*|descumpri\w*|nao atendid\w*|vencimento antecipado|waivers?")
NEGACAO = re.compile(r"(?:nao (?:houve|ha|foi|foram|ocorreu|ocorreram|identificamos|verificamos|constatamos|"
                     r"tem|existe|existem|temos|tivemos)|inexist\w*|sem (?:registro|ocorrencia)|ausencia de|"
                     r"nenhum\w*)[^.;]{0,140}$")
ADIMPLENTE = re.compile(r"^[^.;]{0,40}?\badimplente\b|^[^.;]{0,40}\b(?:nao|nenhum)\b")
CANONICO = [("inadimpl", "inadimplemento"), ("descumpri", "descumprimento"), ("nao atendid", "nao atendido"),
            ("vencimento antecipado", "vencimento antecipado"), ("waiver", "waiver")]

COLS = ["agente", "tipo", "codigo", "devedor", "securitizadora", "emissao", "serie", "ano_relatorio",
        "arquivo", "url", "match", "flag_alerta", "flag_sem_negacao", "termos", "trecho", "texto",
        "coletado_em"]


# --------------------------------------------------------------------------------------
# parsing puro (testado)
# --------------------------------------------------------------------------------------
def numeros(s: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", s or "")]


def parse_ot_titulo(titulo: str) -> tuple[list[int], str | None]:
    """'Série 001 335ª Emissão' -> ([1], '335'); '1ª Série 3ª Emissão' -> ([1], '3')."""
    t = fc.sem_acento(titulo or "").lower()
    em = re.search(r"(\d+)\D{0,3}emissao", t)
    se = re.search(r"serie\D{0,3}(\d+)|(\d+)\D{0,3}serie", t)
    series = []
    if se:
        series = [int(se.group(1) or se.group(2))]
    return series, (str(int(em.group(1))) if em else None)


def parse_pt_arquivo(nome: str) -> dict:
    """'BRAZILIAN SECURITIES ... - 302ª, 303ª E 304ª SÉRIES DA 1ª EMISSÃO.pdf'."""
    n = html.unescape(nome or "")
    n = re.sub(r"\.pdf$", "", n, flags=re.I).strip()
    sec, _, resto = n.rpartition(" - ")
    if not sec:
        sec, resto = n, ""
    r = fc.sem_acento(resto).upper()
    em = re.search(r"(\d+)\D{0,3}EMISSAO", r)
    pre = r[: em.start()] if em else r
    series = numeros(re.sub(r"EMISSAO.*", "", pre))
    return {"securitizadora": sec.strip() or None, "series": series,
            "emissao": str(int(em.group(1))) if em else None}


def parse_vx_operacoes(pagina: str) -> list[dict]:
    """Operacoes do payload RSC da busca da Vortx."""
    t = pagina.replace('\\"', '"')
    out = []
    for m in re.finditer(r'"id":(\d+),"nickname":"([^"]*)","ifCode":"([^"]*)","numeroISIN":"([^"]*)"'
                         r'.{0,200}?"issue":"([^"]*)","serie":"([^"]*)"', t):
        out.append({"id": int(m.group(1)), "nickname": m.group(2), "ifCode": m.group(3),
                    "isin": m.group(4) or None, "emissao": m.group(5) or None, "serie": m.group(6) or None})
    return out


def analisa_texto(texto: str) -> dict:
    """Flags de alerta e trecho de ate 300 caracteres em torno da primeira ocorrencia."""
    t = re.sub(r"\s+", " ", texto or "").strip()
    # normaliza caractere a caractere para manter os indices alinhados com o original
    tn = "".join(n if len(n := fc.sem_acento(c).lower()) == 1 else c.lower() for c in t)
    if not tn:
        return {"flag_alerta": None, "flag_sem_negacao": None, "termos": None, "trecho": None}
    achados = list(TERMOS.finditer(tn))
    if not achados:
        return {"flag_alerta": False, "flag_sem_negacao": False, "termos": None, "trecho": None}
    # campo de ficha "Inadimplemento no periodo: Adimplente" tambem e negacao
    sem_neg = [m for m in achados if not NEGACAO.search(tn[max(0, m.start() - 160): m.start()])
               and not ADIMPLENTE.search(tn[m.end(): m.end() + 60])]
    alvo = (sem_neg or achados)[0]
    ini = max(0, alvo.start() - 120)
    trecho = t[ini: ini + 300].strip()
    termos = sorted({nome for m in achados for raiz, nome in CANONICO if m.group(0).startswith(raiz)})
    return {"flag_alerta": True, "flag_sem_negacao": bool(sem_neg), "termos": ";".join(termos),
            "trecho": fc.trecho(trecho)}


# --------------------------------------------------------------------------------------
# universo
# --------------------------------------------------------------------------------------
def carrega_alvos() -> list[dict]:
    """Um alvo por codigo do universo, com emissao/serie/securitizadora do Fundos.NET se houver."""
    uni = fc.carrega_universo()
    emis: dict[str, list[dict]] = {}
    if UNI_FNET.exists():
        with UNI_FNET.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("ambiguo") in ("True", "true", "1"):
                    continue
                emis.setdefault(r["codigo_universo"], []).append(r)
    alvos = []
    for u in uni:
        lst = emis.get(u["codigo"], [])
        e = lst[0] if lst else {}
        alvos.append({
            "codigo": u["codigo"], "tipo": u.get("tipo"), "devedor": u.get("devedor"),
            "securitizadora": u.get("securitizadora"), "_sec": u["_sec"],
            "serie": u["_serie"], "emissao": e.get("emissao") or None,
        })
    return alvos


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class Cliente(fc.Cliente):
    def post(self, url, data):
        espera = PAUSA - (time.monotonic() - self._ult)
        if espera > 0:
            time.sleep(espera)
        for tent in range(3):
            try:
                self.reqs += 1
                r = self.s.post(url, data=data, timeout=TIMEOUT)
                self._ult = time.monotonic()
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except requests.RequestException as e:
                self._ult = time.monotonic()
                log.warning("falha POST %s (%s), tentativa %d", url, e, tent + 1)
                time.sleep(5 * (tent + 1))
        raise RuntimeError(f"sem resposta: {url}")


def texto_de(cli: Cliente, url: str) -> tuple[str | None, str]:
    r = cli.get(url)
    if r.status_code in (401, 403):
        return None, "login"
    r.raise_for_status()
    c = r.content
    if c[:4] == b"%PDF":
        return fc.texto_pdf(c), "pdf"
    return None, "nao_pdf"


# --------------------------------------------------------------------------------------
# coletores
# --------------------------------------------------------------------------------------
def ot_lista(cli: Cliente, tipo: str, ano: int) -> list[dict]:
    out, pag = [], 1
    while True:
        r = cli.get(OT_LISTA, params={"page": pag, "limit": 100, "search": "Relatório Anual",
                                      "tipo_ativo": tipo, "ano": ano})
        r.raise_for_status()
        d = r.json().get("data") or {}
        itens = d.get("data") or []
        out += itens
        if not itens or (d.get("to") or 0) >= (d.get("total") or 0):
            return out
        pag += 1


def casa_sec_em_serie(alvos, sec_nome, emissao, series, tipo):
    k = fc.chave_sec(sec_nome)
    hits = []
    for a in alvos:
        if not a["emissao"] or a["_sec"] != k or str(a["emissao"]) != str(emissao):
            continue
        if tipo and a.get("tipo") and a["tipo"] != tipo:
            continue
        if series and a["serie"] not in series:
            continue
        hits.append(a)
    return hits


def coleta_ot(cli, alvos, anos, diag) -> list[dict]:
    linhas = []
    for tipo in ("cri", "cra"):
        for ano in anos:
            try:
                itens = ot_lista(cli, tipo, ano)
            except Exception as e:
                diag["erros"][f"oliveira {tipo} {ano}"] = str(e)[:200]
                continue
            diag["lidos"]["oliveira_trust"] += len(itens)
            for it in itens:
                series, emissao = parse_ot_titulo(it.get("titulo") or "")
                hits = casa_sec_em_serie(alvos, it.get("nomec") or it.get("nomeg"), emissao, series,
                                         tipo.upper())
                for a in hits:
                    linhas.append({**_base(a), "agente": "Oliveira Trust", "emissao": emissao,
                                   "ano_relatorio": it.get("ano") or ano,
                                   "arquivo": it.get("arquivo_nome"),
                                   "url": OT_DOWNLOAD.format(uid=it["uid"]), "match": "emissao_serie"})
    return linhas


def coleta_pt(cli, alvos, anos, diag) -> list[dict]:
    from bs4 import BeautifulSoup
    linhas = []
    for prod in ("CRI", "CRA"):
        for ano in anos:
            try:
                r = cli.post(PT_LISTA, {"Prod": prod, "Ano": str(ano)})
                r.raise_for_status()
            except Exception as e:
                diag["erros"][f"pentagono {prod} {ano}"] = str(e)[:200]
                continue
            sopa = BeautifulSoup(r.text, "html.parser")
            links = [a for a in sopa.select("table a[href]") if "/Docs/RelatAnuais/" in a["href"]]
            diag["lidos"]["pentagono"] += len(links)
            for a_ in links:
                nome = a_.get_text(strip=True) or a_["href"].rsplit("/", 1)[-1]
                p = parse_pt_arquivo(nome)
                for a in casa_sec_em_serie(alvos, p["securitizadora"], p["emissao"], p["series"], prod):
                    linhas.append({**_base(a), "agente": "Pentágono", "emissao": p["emissao"],
                                   "ano_relatorio": ano, "arquivo": nome,
                                   "url": PT_BASE + urllib.parse.quote(a_["href"], safe="/"),
                                   "match": "emissao_serie"})
    return linhas


def coleta_vx(cli, alvos, anos, diag) -> list[dict]:
    linhas = []
    for a in alvos:
        try:
            r = cli.get(VX_BUSCA, params={"busca": a["codigo"]})
            r.raise_for_status()
            ops = [o for o in parse_vx_operacoes(r.text) if o["ifCode"] == a["codigo"]]
        except Exception as e:
            diag["erros"][f"vortx busca {a['codigo']}"] = str(e)[:200]
            continue
        diag["lidos"]["vortx_buscas"] += 1
        for o in ops:
            try:
                rr = cli.get(VX_RELATORIOS.format(id=o["id"]))
                rr.raise_for_status()
                rels = rr.json().get("data") or []
            except Exception as e:
                diag["erros"][f"vortx relatorios {o['id']}"] = str(e)[:200]
                continue
            diag["lidos"]["vortx_relatorios"] += len(rels)
            for rel in rels:
                if anos and int(rel.get("anoReferencia") or 0) not in anos:
                    continue
                arq_url = rel.get("diretorio") or ""
                arquivo = urllib.parse.unquote(urllib.parse.urlparse(arq_url).path.rsplit("/", 1)[-1]) or None
                linhas.append({**_base(a), "agente": "Vórtx", "emissao": o.get("emissao"),
                               "serie": o.get("serie") or a["serie"],
                               "ano_relatorio": rel.get("anoReferencia"), "arquivo": arquivo,
                               # link assinado expira; guardamos a pagina publica da operacao
                               "url": VX_OPERACAO.format(id=o["id"]), "match": "codigo",
                               "_arquivo_url": arq_url})
    return linhas


def _base(a: dict) -> dict:
    return {"tipo": a.get("tipo"), "codigo": a["codigo"], "devedor": a.get("devedor"),
            "securitizadora": a.get("securitizadora"), "serie": a.get("serie")}


# --------------------------------------------------------------------------------------
# execucao
# --------------------------------------------------------------------------------------
def roda(args) -> int:
    hoje = dt.date.today()
    anos = args.anos or [hoje.year - 1, hoje.year]
    diag = {"lidos": Counter(), "gravados": 0, "descartados": Counter(), "erros": {}}
    if not SND.exists():
        diag["descartados"]["debentures (dados/snd/caracteristicas.csv ausente)"] = 1
    alvos = carrega_alvos()
    if args.limite:
        alvos = alvos[: args.limite]
    com_emissao = sum(1 for a in alvos if a["emissao"])
    log.info("%d codigos no universo, %d com emissao conhecida", len(alvos), com_emissao)
    cli = Cliente()
    linhas = []
    for nome, fn in (("oliveira", coleta_ot), ("pentagono", coleta_pt), ("vortx", coleta_vx)):
        if args.agentes and nome not in args.agentes:
            continue
        try:
            got = fn(cli, alvos, anos, diag)
            log.info("%s: %d relatorios casados", nome, len(got))
            linhas += got
        except Exception as e:
            diag["erros"][nome] = f"{type(e).__name__}: {e}"[:300]

    caminho = DIR_OUT / "relatorios.csv"
    antigos = {}
    if caminho.exists():
        with caminho.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                antigos[(r["agente"], r["codigo"], r["url"], r["arquivo"])] = r
    agora = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cache_txt: dict[str, tuple] = {}
    for l in linhas:
        k = (l["agente"], l["codigo"], l["url"], l.get("arquivo") or "")
        velho = antigos.get(k)
        if velho and velho.get("texto") in ("pdf",) and not os.environ.get("AF_RELER"):
            for c in ("flag_alerta", "flag_sem_negacao", "termos", "trecho", "texto", "coletado_em"):
                l[c] = velho.get(c)
            continue
        src = l.pop("_arquivo_url", None) or l["url"]
        try:
            if src not in cache_txt:
                cache_txt[src] = texto_de(cli, src)
            txt, tipo_txt = cache_txt[src]
        except Exception as e:
            txt, tipo_txt = None, "erro"
            diag["erros"][f"arquivo {l['agente']} {l.get('arquivo')}"] = str(e)[:200]
        l["texto"] = tipo_txt
        l.update(analisa_texto(txt) if txt else {"flag_alerta": None, "flag_sem_negacao": None,
                                                  "termos": None, "trecho": None})
        l["coletado_em"] = agora
    for l in linhas:
        l.pop("_arquivo_url", None)
        antigos[(l["agente"], l["codigo"], l["url"], l.get("arquivo") or "")] = l
    todas = sorted(antigos.values(), key=lambda r: (str(r.get("ano_relatorio")), r["agente"], r["codigo"]),
                   reverse=True)
    fc.grava_csv(caminho, todas, COLS)
    diag["gravados"] = len(linhas)
    resumo = {
        "fonte": "Relatorios anuais de agente fiduciario (Oliveira Trust, Pentagono, Vortx)",
        "anos": anos, "gerado_em": agora,
        "codigos_universo": len(alvos), "codigos_com_emissao": com_emissao,
        "relatorios_execucao": len(linhas), "relatorios_base": len(todas),
        "por_agente": dict(Counter(l["agente"] for l in linhas)),
        "com_flag_alerta": sum(1 for l in linhas if str(l.get("flag_alerta")) == "True"),
        "com_flag_sem_negacao": sum(1 for l in linhas if str(l.get("flag_sem_negacao")) == "True"),
        "requisicoes": cli.reqs,
        "diagnostico": {**diag, "lidos": dict(diag["lidos"]), "descartados": dict(diag["descartados"])},
    }
    (DIR_OUT / "ultimo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"## Agente fiduciario - relatorios anuais {anos}", "",
          f"- Codigos no universo: {len(alvos)} (com emissao conhecida: {com_emissao})",
          f"- Relatorios casados nesta execucao: **{len(linhas)}** `{resumo['por_agente']}`",
          f"- Com termo de alerta: {resumo['com_flag_alerta']} (sem negacao proxima: "
          f"{resumo['com_flag_sem_negacao']})",
          f"- Diagnostico: `{json.dumps(resumo['diagnostico'], ensure_ascii=False)[:1500]}`", ""]
    if linhas:
        md += ["| agente | codigo | devedor | ano | alerta | termos |", "|---|---|---|---|---|---|"]
        for l in linhas[:10]:
            md.append(f"| {l['agente']} | {l['codigo']} | {l.get('devedor')} | {l.get('ano_relatorio')} | "
                      f"{l.get('flag_sem_negacao')} | {l.get('termos')} |")
    fc._summary("\n".join(md) + "\n")
    return 0


def inspeciona(args) -> int:
    cli = Cliente()
    ano = (args.anos or [dt.date.today().year - 1])[0]
    out = [f"## Inspecao agentes fiduciarios (ano {ano})", ""]
    try:
        r = cli.get(OT_LISTA, params={"page": 1, "limit": 3, "search": "Relatório Anual",
                                      "tipo_ativo": "cri", "ano": ano})
        d = r.json().get("data") or {}
        out.append(f"- Oliveira Trust: HTTP {r.status_code}, total={d.get('total')}, "
                   f"colunas={sorted((d.get('data') or [{}])[0].keys())}")
        for it in (d.get("data") or [])[:3]:
            out.append(f"  - {it.get('arquivo_nome')} | {it.get('titulo')} | {it.get('nomec')} | {it.get('ano')}")
    except Exception as e:
        out.append(f"- Oliveira Trust: ERRO {e}")
    try:
        from bs4 import BeautifulSoup
        r = cli.post(PT_LISTA, {"Prod": "CRI", "Ano": str(ano)})
        links = [a for a in BeautifulSoup(r.text, "html.parser").select("table a[href]")
                 if "/Docs/RelatAnuais/" in a["href"]]
        out.append(f"- Pentagono: HTTP {r.status_code}, {len(links)} arquivos CRI")
        for a in links[:3]:
            out.append(f"  - {a.get_text(strip=True)} -> {parse_pt_arquivo(a.get_text(strip=True))}")
    except Exception as e:
        out.append(f"- Pentagono: ERRO {e}")
    try:
        alvos = carrega_alvos()
        cod = next((a["codigo"] for a in alvos if a["codigo"].startswith("CRA")), "CRA022008NB")
        r = cli.get(VX_BUSCA, params={"busca": cod})
        ops = parse_vx_operacoes(r.text)
        out.append(f"- Vortx: busca {cod} HTTP {r.status_code}, operacoes={ops[:3]}")
        if ops:
            rr = cli.get(VX_RELATORIOS.format(id=ops[0]["id"]))
            rels = rr.json().get("data") or []
            out.append(f"  - relatorios-anuais HTTP {rr.status_code}: "
                       f"{[(x.get('anoReferencia'), x.get('descricaoOperacao')) for x in rels[:3]]}")
        out.append(f"- Universo: {len(alvos)} codigos, {sum(1 for a in alvos if a['emissao'])} com emissao")
    except Exception as e:
        out.append(f"- Vortx: ERRO {e}")
    out.append(f"- Debentures: {'dados/snd/caracteristicas.csv presente' if SND.exists() else 'caracteristicas.csv ausente, puladas'}")
    fc._summary("\n".join(out) + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inspecionar", action="store_true")
    ap.add_argument("--anos", type=int, nargs="*", help="anos de referencia (padrao: anterior e corrente)")
    ap.add_argument("--agentes", nargs="*", choices=["oliveira", "pentagono", "vortx"])
    ap.add_argument("--limite", type=int, help="so os N primeiros codigos (teste)")
    args = ap.parse_args(argv)
    try:
        return inspeciona(args) if args.inspecionar else roda(args)
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        fc._summary(f"## agente_fiduciario: erro nao tratado\n\n```\n{tb[-3000:]}\n```\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
