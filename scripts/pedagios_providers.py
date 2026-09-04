#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PROVIDERS DE PEDÁGIO — ANTT (federal) e ARTESP (São Paulo)
════════════════════════════════════════════════════════════════════════════
Cada provider devolve uma lista de praças/pórticos no mesmo formato. Nenhum
deles escreve arquivo: quem grava é `build_pedagios.py`, depois de validar o
conjunto inteiro. Assim uma fonte instável não deixa a base pela metade.

POR QUE DOIS PROVIDERS E NÃO UMA FUNÇÃO SÓ
────────────────────────────────────────────────────────────────────────────
As duas esferas cobram de formas diferentes. A federal publica categoria
fechada por número de eixos e rodagem; a paulista publica tarifa unitária
por eixo. Espremer as duas num parser genérico produziria um que não entende
direito nenhuma das duas.

O QUE NENHUM DOS DOIS FAZ
────────────────────────────────────────────────────────────────────────────
Não inventa tarifa. Não deriva 9 eixos de 8 por parecer linear. Não trata
documento antigo como vigente. Quando falta dado, o campo fica `None` e o
status diz por quê.
"""

import io
import json
import re
import unicodedata
import urllib.request
import zipfile
from datetime import date

UA = "RadarDiarioCommodities/1.0 (github.com/Heitorsalmazi/RadarDi-rioCommodities)"
TIMEOUT = 90

# ── ANTT ────────────────────────────────────────────────────────────────
ANTT_CKAN = "https://dados.antt.gov.br/api/3/action/package_show?id=praca-de-pedagio"
ANTT_CONCESSOES = ("https://www.gov.br/antt/pt-br/assuntos/rodovias/"
                   "concessionarias/lista-de-concessoes")
ANTT_SENTIDO = {"Crescente": "CRESCENTE", "Decrescente": "DECRESCENTE",
                "Crescente/Decrescente": "AMBOS"}

# ── ARTESP ──────────────────────────────────────────────────────────────
ARTESP_CKAN = "https://dadosabertos.artesp.sp.gov.br/api/3/action/package_show?id=pedagio"
ARTESP_PEDAGIOS = "https://www.artesp.sp.gov.br/artesp/setor-regulado/rodovia/pedagios"
ARTESP_ANCORA_ATUAL = "Valor Atual das Tarifas"
ARTESP_ANCORA_HIST = "Histórico de Tarifas (Contratos Vigentes)"


def _get(url, aceita="text/html"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": aceita})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"\s+", " ", sem_acento(str(s or "")).lower()).strip()


def num_br(txt):
    """'12,90' → 12.9 · '1.016,33' → 1016.33 · 'R$/km' → None."""
    s = re.sub(r"[^\d,\.\-]", "", str(txt or ""))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _texto(frag):
    t = re.sub(r"<[^>]+>", " ", frag or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&ordm;", "º"), ("&#186;", "º")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _tabela(html_tabela):
    """Matriz de textos com colspan expandido."""
    linhas = []
    for tr in re.findall(r"<tr\b.*?</tr>", html_tabela, re.S | re.I):
        L = []
        for cel in re.findall(r"<t[hd]\b[^>]*>.*?</t[hd]>", tr, re.S | re.I):
            cs = re.search(r'colspan\s*=\s*["\']?(\d+)', cel, re.I)
            L.extend([_texto(cel)] * (int(cs.group(1)) if cs else 1))
        if L:
            linhas.append(L)
    return linhas


def slug_concessionaria(nome):
    """Nome do dataset → slug do site. `PANTANAL` no dataset, `motiva-pantanal`
    no site: a diferença é real e precisa de mapa explícito, não de palpite."""
    especiais = {
        "PANTANAL": "motiva-pantanal",
        "AUTOPISTA REGIS BITTENCOURT": "autopista-regis-bittencourt",
        "AUTOPISTA FLUMINENSE": "autopista-fluminense",
        "AUTOPISTA LITORAL SUL": "autopista-litoral-sul",
        "AUTOPISTA PLANALTO SUL": "autopista-planalto-sul",
        "MOTIVA MINAS SP": "motiva-minas-sp",
        "MOTIVA PARANÁ": "pr-vias",
        "ECOVIAS DO CERRADO": "ecovias-do-cerrado",
        "ECOVIAS MINAS GOIÁS": "eco050",
        "ECOVIAS DO ARAGUAIA": "ecovias-araguaia",
        "ECOVIAS CAPIXABA": "ecovias-capixaba",
        "ECOVIAS RIO MINAS": "ecoriominas",
        "ECOVIAS PONTE": "ecoponte",
        "WAY 262": "way-262",
        "TRANSBRASILIANA": "Transbrasiliana",
    }
    if nome in especiais:
        return especiais[nome]
    return re.sub(r"[^a-z0-9]+", "-", norm(nome)).strip("-")


# ════════════════════════════════════════════════════════════════════════
#  ANTT
# ════════════════════════════════════════════════════════════════════════
def antt_localizacoes(diag):
    """277 praças ativas, com coordenada e sentido. Recurso descoberto pelo
    CKAN — o nome do arquivo carrega o mês e muda sozinho."""
    pkg = json.loads(_get(ANTT_CKAN, "application/json").decode("utf-8"))["result"]
    rec = next((r for r in pkg["resources"] if (r.get("format") or "").upper() == "JSON"), None)
    if not rec:
        raise RuntimeError("dataset ANTT sem recurso JSON")
    diag["antt_recurso"] = rec["id"]
    diag["antt_recurso_url"] = rec["url"]
    bruto = json.loads(_get(rec["url"], "application/json").decode("utf-8"))
    linhas = bruto.get("praca-de-pedagio") or []
    out, vistos = [], set()
    for x in linhas:
        if norm(x.get("situacao")) != "ativo":
            continue
        try:
            la, lo = float(x["latitude"]), float(x["longitude"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (-34 < la < 6 and -74 < lo < -28):
            continue
        chave = (x.get("concessionaria"), x.get("praca_de_pedagio"), round(la, 5), round(lo, 5))
        if chave in vistos:      # RIOSP publica vários pórticos na mesma coordenada
            continue
        vistos.add(chave)
        nome = (x.get("praca_de_pedagio") or "").strip()
        out.append({
            "concessionaria": (x.get("concessionaria") or "").strip(),
            "nome": nome,
            "rodovia": (x.get("rodovia") or "").strip(),
            "uf": (x.get("uf") or "").strip(),
            "km": num_br(x.get("km_m")),
            "municipio": (x.get("municipio") or "").strip(),
            "sentido": ANTT_SENTIDO.get(x.get("sentido"), (x.get("sentido") or "").upper()),
            "latitude": round(la, 6), "longitude": round(lo, 6),
            "tipo": "portico_free_flow" if "free" in norm(nome) else "praca",
        })
    diag["antt_pracas"] = len(out)
    return out


def antt_tarifas(concessionaria, diag):
    """Tarifa 3e e 6e com RODAGEM DUPLA.

    A tabela federal tem duas linhas de 3 eixos: categoria 3 (rodagem simples,
    automóvel com semirreboque) e categoria 4 (rodagem dupla, caminhão). Casar
    só por "3 eixos" pega a errada e erra ~50% para menos.

    9 eixos: a tabela termina em 8 e não publica regra de eixo excedente.
    Devolve None — derivar por linearidade aparente seria inventar."""
    url = f"{ANTT_CONCESSOES}/{slug_concessionaria(concessionaria)}/tarifas-de-pedagio"
    try:
        html = _get(url).decode("utf-8", errors="replace")
    except Exception as e:
        diag.setdefault("antt_tarifa_falhou", []).append(f"{concessionaria}: {e}")
        return None

    tabs = re.findall(r"<table\b.*?</table>", html, re.S | re.I)
    tab = next((t for t in tabs if "categoria de veiculo" in norm(_texto(t))), None)
    if not tab:
        diag.setdefault("antt_sem_tabela", []).append(concessionaria)
        return None

    linhas = _tabela(tab)
    # Linha de nomes de praça: só existe quando a concessão tarifa por praça.
    colunas = None
    if len(linhas) > 1 and not re.fullmatch(r"\d+", (linhas[1][0] or "").strip()):
        cand = [c for c in linhas[1] if c.strip()]
        if cand and all(len(c) <= 12 for c in cand):
            colunas = cand

    def linha(eixos):
        for r in linhas:
            if len(r) > 4 and (r[2] or "").strip() == str(eixos) and "dupla" in norm(r[3]):
                return r
        return None

    res = {"colunas": colunas, "3e": None, "6e": None, "9e": None,
           "metodo": "categoria_rodagem_dupla", "url": url}
    for k, n in (("3e", 3), ("6e", 6)):
        r = linha(n)
        if r:
            vals = [num_br(v) for v in r[5:] if num_br(v) is not None]
            res[k] = vals if colunas else (vals[0] if vals else None)
    # Regra de eixo excedente: só se a página publicar explicitamente.
    if re.search(r"eixo\s+excedente|eixo\s+adicional|acima\s+de\s+8\s+eixos", norm(html)):
        diag.setdefault("antt_regra_excedente", []).append(concessionaria)
    return res


# ════════════════════════════════════════════════════════════════════════
#  ARTESP
# ════════════════════════════════════════════════════════════════════════
def _artesp_href(html, texto_ancora):
    """Descobre o documento pelo TEXTO da âncora, nunca pelo UUID.

    O DAM do CMS paulista exige autenticação para listar a coleção, então não
    dá para enumerar. Mas o link está no HTML servido, sem JavaScript: quando
    a ARTESP publica novo reajuste o UUID muda e o texto continua o mesmo."""
    alvo = norm(texto_ancora)
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        if alvo in norm(_texto(m.group(2))):
            href = m.group(1)
            return href if href.startswith("http") else "https://www.artesp.sp.gov.br" + href
    return None


def _pdf_texto(binario):
    """Texto do PDF. Usa pdfplumber quando disponível; senão, extrai os
    streams FlateDecode com a stdlib. O PDF da ARTESP tem camada de texto —
    verificado —, então não é preciso OCR."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(binario)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    import zlib
    partes = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", binario, re.S):
        try:
            d = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        for t in re.findall(rb"\((?:\\.|[^\\()])*\)", d):
            partes.append(t[1:-1].replace(b"\\(", b"(").replace(b"\\)", b")"))
    return b" ".join(partes).decode("latin-1", errors="replace")


# Rodovia + praça + km + sentido + tarifa passeio + tarifa comercial por eixo.
_RX_HIST = re.compile(
    r"(SP-\d{3})\s+([A-ZÀ-Ú0-9ºª()\.\-\s/']{2,45}?)\s(\d{3}\+\d{3})\s+"
    r"COBRAN[ÇC]A\s+(BIDIRECIONAL|UNIDIRECIONAL)\s+TARIFA\s+\w+\s+"
    r"R\$\s*([\d,\.]+)\s+R\$\s*([\d,\.]+)")
_RX_ATUAL = re.compile(
    r"(SP-\d{3})\s+([A-ZÀ-Ú0-9ºª()\.\-\s/']{2,45}?)\s(\d{3}\+\d{3})\s+"
    r"R\$\s*([\d,\.]+)\s+R\$\s*([\d,\.]+)")


def artesp_coletar(diag):
    """Devolve (pracas, vigencia).

    Duas fontes, papéis distintos e declarados:
      Valor Atual das Tarifas  → valor corrente
      Histórico (Vigentes)     → sentido de cobrança e série temporal

    A vigência sai da comparação entre a data-base dos documentos e a data de
    hoje, POR CONCESSÃO — não por uma regra única de "reajuste em julho"."""
    html = _get(ARTESP_PEDAGIOS).decode("utf-8", errors="replace")
    diag["artesp_pagina"] = ARTESP_PEDAGIOS

    href_atual = _artesp_href(html, ARTESP_ANCORA_ATUAL)
    href_hist = _artesp_href(html, ARTESP_ANCORA_HIST)
    if not href_atual and not href_hist:
        raise RuntimeError("nenhuma âncora de tarifa encontrada na página da ARTESP")
    diag["artesp_href_atual"] = href_atual
    diag["artesp_href_historico"] = href_hist

    txt_hist = _pdf_texto(_get(href_hist, "application/pdf")) if href_hist else ""
    txt_atual = _pdf_texto(_get(href_atual, "application/pdf")) if href_atual else ""

    def datas(t):
        return sorted({d for d in re.findall(r"\d{2}/\d{2}/\d{4}", t or "")},
                      key=lambda s: (s[6:], s[3:5], s[:2]), reverse=True)

    d_hist, d_atual = datas(txt_hist), datas(txt_atual)
    base = (d_atual or d_hist or [None])[0]
    diag["artesp_datas_atual"] = d_atual[:3]
    diag["artesp_datas_historico"] = d_hist[:3]

    # Sentido vem do histórico; valor, do Valor Atual quando disponível.
    sentido = {}
    for sp, nome, km, cobr, _p, _c in _RX_HIST.findall(re.sub(r"\s+", " ", txt_hist)):
        sentido[(sp, nome.strip(), km)] = cobr

    linhas = _RX_HIST.findall(re.sub(r"\s+", " ", txt_hist))
    fonte_valor = "historico"
    atuais = _RX_ATUAL.findall(re.sub(r"\s+", " ", txt_atual))
    if atuais:
        fonte_valor = "valor_atual"

    pracas = []
    vistos = set()
    fonte_iter = ([(sp, n, km, c) for sp, n, km, _p, c in atuais] if atuais
                  else [(sp, n, km, c) for sp, n, km, _cb, _p, c in linhas])
    for sp, nome, km, comercial in fonte_iter:
        nome = nome.strip()
        chave = (sp, nome, km)
        if chave in vistos:
            continue
        vistos.add(chave)
        v = num_br(comercial)
        if v is None or v <= 0:
            continue
        cobr = sentido.get(chave)
        pracas.append({
            "rodovia": sp, "nome": nome, "km": km, "uf": "SP",
            "tarifaPorEixo": v,
            "cobranca": cobr,
            "sentido": ("AMBOS" if cobr == "BIDIRECIONAL"
                        else "UNICO_NAO_ESPECIFICADO" if cobr else None),
            "tipo": "portico_free_flow" if "pap" in norm(nome) or "portico" in norm(nome) else "praca",
        })

    vigencia = _artesp_vigencia(base, diag)
    diag["artesp_pracas"] = len(pracas)
    diag["artesp_fonte_valor"] = fonte_valor
    return pracas, vigencia


def _artesp_vigencia(data_base, diag):
    """Decide o status da tarifa paulista.

    NÃO assume "reajuste todo 1º de julho". O que faz é comparar a data-base
    publicada com hoje e, se houver distância suficiente para ter havido
    revisão sem que o documento tenha mudado, marcar como não confirmada.
    Marcar é diferente de apagar: o valor continua na base, rotulado."""
    if not data_base:
        return {"dataBase": None, "status": "indeterminado",
                "motivo": "documento sem data-base legível"}
    dd, mm, aa = (int(x) for x in data_base.split("/"))
    base = date(aa, mm, dd)
    hoje = date.today()
    meses = (hoje.year - base.year) * 12 + (hoje.month - base.month)
    diag["artesp_data_base"] = base.isoformat()
    diag["artesp_meses_desde_base"] = meses
    if meses <= 12:
        return {"dataBase": base.isoformat(), "status": "vigente",
                "motivo": f"data-base há {meses} meses, dentro do ciclo de revisão"}
    return {"dataBase": base.isoformat(), "status": "desatualizado_nao_confirmado",
            "motivo": (f"data-base há {meses} meses e nenhum documento mais recente foi "
                       "publicado na página oficial; valor mantido como último oficial conhecido")}
