#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COEFICIENTES DA ANTT — DESCOBERTA E EXTRAÇÃO AUTOMÁTICAS
════════════════════════════════════════════════════════════════════════════
Mantém, em `data/logistica_reposicao.json`, os coeficientes vigentes do piso
mínimo de frete:

    CCD  custo de deslocamento, R$ por quilômetro
    CC   custo de carga e descarga, R$ por operação

para 3, 6 e 9 eixos, na TABELA A (Transporte Rodoviário de Carga Lotação),
linha Carga Geral.

FONTE — E POR QUE ESTA
────────────────────────────────────────────────────────────────────────────
A versão CONSOLIDADA da Resolução ANTT nº 5.867/2020 no ANTTLegis. É ela que
carrega o ANEXO II com a tabela de coeficientes, e é ela que o próprio texto
oficial aponta como o lugar atualizado pelas revisões:

    "O Anexo II desta Resolução, que contém os coeficientes de pisos mínimos
     de frete, é atualizado por meio das revisões ordinárias e
     extraordinárias."

A página institucional do gov.br sobre a Política de Pisos Mínimos NÃO serve:
ela é texto explicativo e não contém nenhuma tabela de coeficientes. Uma
versão anterior deste script apontava para lá e teria falhado para sempre,
em silêncio.

O documento consolidado já incorpora a redação vigente, venha ela de
Resolução (revisão ordinária) ou de Portaria SUROC (revisão extraordinária,
disparada quando o diesel S10 oscila mais de 5%). Por isso não é preciso
caçar cada Portaria: basta ler o consolidado e registrar qual ato deu a
redação atual.

TRÊS ARMADILHAS REAIS DESTE DOCUMENTO
────────────────────────────────────────────────────────────────────────────
1. "TABELA A" aparece ANTES no corpo do texto ("obtidos na Tabela A do ANEXO
   II"), muito longe da tabela. Pegar a primeira ocorrência e depois o
   primeiro <table> devolveria a tabela errada. Só o TÍTULO conta, e o
   título traz a descrição da operação junto.

2. A linha 11 da tabela é "Perigosa (carga geral)". Casar "carga geral" por
   substring pega essa linha e devolve números plausíveis e errados. A
   comparação é do texto INTEIRO da célula.

3. Perto do título da Tabela A há menção à Resolução 6.076/2026, que não é o
   ato da Tabela A. O ato é lido na janela entre o título e a tabela, não no
   documento inteiro.

O QUE ESTE SCRIPT NÃO FAZ
────────────────────────────────────────────────────────────────────────────
Não grava nada parcialmente. Extrai os seis números e o ato para a memória,
valida o conjunto, e só então escreve.

Não transforma ausência em zero. CCD zero faria o frete virar só o CC, e a
conta fecharia sem reclamar.

Não carimba data no arquivo versionado quando nada mudou. Carimbar geraria
um commit por dia — e um deploy por dia — para registrar que nada aconteceu.
A data da consulta fica no log do Actions.

USO
    python3 scripts/update_antt.py
    python3 scripts/update_antt.py --fixture testes/fixtures/antt_real_5867_consolidada.html
    python3 scripts/update_antt.py --dry-run
"""

import argparse
import html as _html
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQ = os.path.join(RAIZ, "data", "logistica_reposicao.json")

UA = "RadarDiarioCommodities/1.0 (github.com/Heitorsalmazi/RadarDi-rioCommodities)"
TIMEOUT = 60

# Versão consolidada da Resolução 5.867/2020 no ANTTLegis.
FONTE = ("https://anttlegis.antt.gov.br/action/ActionDatalegis.php"
         "?acao=abrirTextoAto&tipo=RES&numeroAto=00005867&seqAto=000"
         "&valorAno=2020&orgao=DG/ANTT/MI&codTipo=&desItem=&desItemFim="
         "&cod_menu=5408&cod_modulo=161&pesquisa=true")

EIXOS_ALVO = {"3e": 3, "6e": 6, "9e": 9}
CAPACIDADE_CAB = {"3e": 25, "6e": 70, "9e": 110}   # parâmetro do projeto, não da ANTT

TIPO_ATO = {"RES": "Resolução", "POR": "Portaria", "PRT": "Portaria"}

# O documento precisa conter TODOS estes marcadores para ser lido.
MARCADORES = [
    "5.867",
    "anexo ii",
    "tabela a",
    "carga geral",
    "deslocamento (ccd)",
    "carga e descarga (cc)",
]


# ── Texto ────────────────────────────────────────────────────────────────
def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def norm(s):
    """Minúsculas, sem acento, espaços colapsados. Para COMPARAR, não para exibir."""
    return re.sub(r"\s+", " ", sem_acento(str(s or "")).lower()).strip()


def texto_de(fragmento_html):
    """Tira tags e resolve entidades. `&nbsp;` vira espaço comum."""
    t = re.sub(r"<[^>]+>", " ", fragmento_html or "")
    t = _html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


# ── Números brasileiros ──────────────────────────────────────────────────
def num_br(txt):
    """Converte número no formato brasileiro para float.

        '5,0977'    → 5.0977
        '541,86'    → 541.86
        '1.016,33'  → 1016.33      ponto é separador de milhar
        'R$/km'     → None
        ''          → None

    Devolve None para qualquer coisa que não seja um número limpo. Aceitar
    lixo aqui é o caminho mais curto para um coeficiente errado."""
    s = (txt or "").strip()
    if not s:
        return None
    # Só dígitos, ponto, vírgula e sinal. Um "R$" ou "km" colado é descartado.
    s = re.sub(r"[^\d,\.\-]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s:
        # Vírgula é o decimal; ponto, se houver, é milhar.
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        # 1.016.330 — só milhares, sem decimal.
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


# ── Tabela ───────────────────────────────────────────────────────────────
def linhas_da_tabela(tabela_html):
    """Matriz de textos, com colspan expandido.

    Sem expandir colspan, o cabeçalho mesclado ('Número de eixos carregados',
    colspan=7) desloca todas as colunas seguintes e o mapa coluna→eixo sai
    errado por um número plausível de casas."""
    matriz = []
    for tr in re.findall(r"<tr\b.*?</tr>", tabela_html, re.S | re.I):
        linha = []
        for m in re.finditer(r"<t[hd]\b([^>]*)>(.*?)</t[hd]>", tr, re.S | re.I):
            attrs, conteudo = m.group(1), m.group(2)
            cs = re.search(r'colspan\s*=\s*["\']?(\d+)', attrs, re.I)
            n = int(cs.group(1)) if cs else 1
            texto = texto_de(conteudo)
            linha.extend([texto] * max(1, n))
        if linha:
            matriz.append(linha)
    return matriz


def mapa_de_eixos(matriz):
    """{numero_de_eixos: indice_da_coluna}, lido do cabeçalho REAL.

    Nada de 'a coluna de 6 eixos é a sexta'. A ANTT publica 2,3,4,5,6,7,9 —
    note que 8 não existe —, e uma mudança futura nessa lista tem de ser
    seguida, não adivinhada."""
    for linha in matriz[:6]:
        mapa = {}
        for i, celula in enumerate(linha):
            t = celula.strip()
            if re.fullmatch(r"\d{1,2}", t):
                mapa[int(t)] = i
        if len(mapa) >= 5:
            return mapa
    return {}


def achar_tabela_a(html):
    """Devolve (html_da_tabela, janela_do_titulo) ou (None, None).

    O título é 'TABELA A' SEGUIDO da descrição da operação. A menção solta a
    'Tabela A' no corpo do texto não casa com isso."""
    plano = sem_acento(html).upper()
    m = re.search(r"TABELA\s+A\s*[-–—]\s*TRANSPORTE\s+RODOVIARIO\s+DE\s+CARGA\s+LOTACAO", plano)
    if not m:
        return None, None
    i = m.start()
    ini = html.lower().find("<table", i)
    if ini < 0:
        return None, None
    fim = html.lower().find("</table>", ini)
    if fim < 0:
        return None, None
    return html[ini:fim + 8], html[i:ini]


def achar_ato(janela):
    """Ato que deu a redação vigente à Tabela A.

    Lido SÓ na janela entre o título e a tabela. O documento inteiro cita
    dezenas de atos, incluindo um logo acima que não tem relação com esta
    tabela."""
    ato = {"tipo": None, "numero": None, "ano": None, "data": None}

    # Forma estruturada: LinkTexto('RES','00006084','000','2026',...)
    m = re.search(r"LinkTexto\(\s*'([A-Z]{3})'\s*,\s*'0*(\d+)'\s*,\s*'\d+'\s*,\s*'(20\d\d)'",
                  janela)
    if m:
        ato["tipo"] = TIPO_ATO.get(m.group(1), m.group(1))
        ato["numero"] = m.group(2)
        ato["ano"] = int(m.group(3))

    # Forma textual, que traz a data por extenso.
    t = texto_de(janela)
    m2 = re.search(
        r"(RESOLU\w*|PORTARIA)[^0-9]{0,60}?(\d[\d\.]{1,8})\s*,?\s*DE\s+(\d{1,2})\s+DE\s+"
        r"(JANEIRO|FEVEREIRO|MAR\w O|MAR\w?O|ABRIL|MAIO|JUNHO|JULHO|AGOSTO|SETEMBRO|OUTUBRO|NOVEMBRO|DEZEMBRO)"
        r"\s+DE\s+(20\d\d)", t, re.I)
    if m2:
        MES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
               "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
               "outubro": 10, "novembro": 11, "dezembro": 12}
        mes = MES.get(norm(m2.group(4)))
        if mes:
            ato["data"] = f"{int(m2.group(5)):04d}-{mes:02d}-{int(m2.group(3)):02d}"
        if not ato["numero"]:
            ato["numero"] = m2.group(2).replace(".", "")
            ato["ano"] = int(m2.group(5))
            ato["tipo"] = "Resolução" if norm(m2.group(1)).startswith("resolu") else "Portaria"
    return ato if ato["numero"] else None


def extrair(html):
    """Devolve (coeficientes, ato, diagnostico). Não grava nada."""
    tabela, janela = achar_tabela_a(html)
    if not tabela:
        return None, None, "título da TABELA A não localizado"

    matriz = linhas_da_tabela(tabela)
    if not matriz:
        return None, None, "TABELA A sem linhas"

    col = mapa_de_eixos(matriz)
    faltam = [n for n in EIXOS_ALVO.values() if n not in col]
    if faltam:
        return None, None, f"cabeçalho sem as colunas de eixos {faltam}"

    # IGUALDADE, NÃO CONTINÊNCIA. "Perigosa (carga geral)" é outra linha.
    i_ccd = None
    for i, linha in enumerate(matriz):
        if any(norm(c) == "carga geral" for c in linha) and \
           any("deslocamento" in norm(c) for c in linha):
            i_ccd = i
            break
    if i_ccd is None:
        return None, None, "linha 'Carga Geral / Deslocamento (CCD)' não localizada"

    if i_ccd + 1 >= len(matriz):
        return None, None, "linha de Carga e descarga (CC) não existe após o CCD"
    linha_cc = matriz[i_ccd + 1]
    if not any("carga e descarga" in norm(c) for c in linha_cc):
        return None, None, "linha seguinte ao CCD não é 'Carga e descarga (CC)'"

    linha_ccd = matriz[i_ccd]
    coefs = {}
    for chave, eixos in EIXOS_ALVO.items():
        i = col[eixos]
        ccd = num_br(linha_ccd[i]) if i < len(linha_ccd) else None
        cc = num_br(linha_cc[i]) if i < len(linha_cc) else None
        coefs[chave] = {"ccd": ccd, "cc": cc}

    return coefs, achar_ato(janela or ""), "ok"


def validar(coefs, ato):
    """Tudo ou nada. Um único item ausente reprova o conjunto inteiro."""
    if not coefs or len(coefs) != 3:
        return False, "faltam configurações de eixos"
    for k in ("3e", "6e", "9e"):
        c = coefs.get(k) or {}
        for campo in ("ccd", "cc"):
            v = c.get(campo)
            if v is None:
                return False, f"{k}.{campo} ausente"
            if not isinstance(v, (int, float)) or v != v:
                return False, f"{k}.{campo} não é número finito"
            if v <= 0:
                return False, f"{k}.{campo} = {v}, deveria ser positivo"
    # Caminhão maior custa mais por km. Se inverter, a tabela foi lida errado.
    if not (coefs["3e"]["ccd"] < coefs["6e"]["ccd"] < coefs["9e"]["ccd"]):
        return False, "CCD não cresce de 3 para 6 para 9 eixos — leitura suspeita"
    if not (coefs["3e"]["cc"] < coefs["9e"]["cc"]):
        return False, "CC de 9 eixos não é maior que o de 3 — leitura suspeita"
    if not ato or not ato.get("numero"):
        return False, "ato que deu a redação vigente não identificado"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=None, help="HTML local, para teste offline")
    ap.add_argument("--dry-run", action="store_true", help="não grava o JSON")
    # Quando se lê uma cópia verbatim do documento oficial (por exemplo, salva
    # de um ambiente com rede), a procedência real não pode virar `null`: o
    # dado veio daquela URL, e o JSON tem de dizer isso.
    ap.add_argument("--fonte-url", default=None,
                    help="registra esta URL como procedência ao usar --fixture")
    args = ap.parse_args()

    agora = datetime.now(timezone.utc)
    print(f"ANTT — verificação em {agora.strftime('%Y-%m-%d %H:%M:%SZ')}")

    if not os.path.exists(ARQ):
        print(f"ERRO: {ARQ} não existe.")
        return 1
    with open(ARQ, encoding="utf-8") as f:
        doc = json.load(f)
    antt = doc.setdefault("antt", {})
    veic = antt.setdefault("veiculos", {})
    ato_guardado = antt.get("ato") or {}
    print(f"Ato armazenado: {ato_guardado.get('tipo')} {ato_guardado.get('numero')}/{ato_guardado.get('ano')}")

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            html = f.read()
        origem = "fixture: " + os.path.basename(args.fixture)
        print(f"Fonte: {origem}")
    else:
        print(f"Fonte: {FONTE}")
        try:
            req = urllib.request.Request(FONTE, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                status = getattr(r, "status", 200)
                html = r.read().decode("utf-8", errors="replace")
            print(f"HTTP {status} · {len(html)} bytes")
        except Exception as e:
            print(f"FALHA ao baixar: {e}")
            print("Coeficientes anteriores preservados. Nada foi gravado.")
            return 1
        origem = FONTE

    tn = norm(html)
    if "nao esta disponivel" in tn:
        print("ANTTLegis respondeu 'ato não disponível no momento'.")
        print("Coeficientes anteriores preservados. Nada foi gravado.")
        return 1
    faltando = [m for m in MARCADORES if m not in tn]
    if faltando:
        print(f"Documento sem os marcadores esperados: {faltando}")
        print("O layout pode ter mudado. Coeficientes anteriores preservados.")
        return 1

    coefs, ato, diag = extrair(html)
    if not coefs:
        print(f"Extração falhou: {diag}")
        print("Coeficientes anteriores preservados. Nada foi gravado.")
        return 1

    print("Tabela A · Carga Geral")
    for k in ("3e", "6e", "9e"):
        print(f"  {EIXOS_ALVO[k]} eixos: CCD {coefs[k]['ccd']}  CC {coefs[k]['cc']}")
    print(f"Ato vigente da Tabela A: {ato}")

    ok, motivo = validar(coefs, ato)
    if not ok:
        print(f"VALIDAÇÃO REPROVOU: {motivo}")
        print("Coeficientes anteriores preservados. Nada foi gravado.")
        return 1

    # ── O que conta como mudança ─────────────────────────────────────────
    mudou_coef = any(
        (veic.get(k) or {}).get("ccd") != coefs[k]["ccd"] or
        (veic.get(k) or {}).get("cc") != coefs[k]["cc"]
        for k in EIXOS_ALVO
    )
    mudou_ato = (
        str(ato_guardado.get("numero")) != str(ato.get("numero")) or
        ato_guardado.get("ano") != ato.get("ano") or
        ato_guardado.get("tipo") != ato.get("tipo")
    )

    if not (mudou_coef or mudou_ato):
        # SEM ESCRITA. Gravar só para carimbar a data geraria um commit por
        # dia e um deploy por dia dizendo que nada aconteceu.
        print("Status: SEM ALTERAÇÃO — coeficientes e ato idênticos aos vigentes")
        print("Arquivo não modificado. Nenhum commit será necessário.")
        return 0

    for k, n in EIXOS_ALVO.items():
        v = veic.setdefault(k, {})
        v["eixos"] = n
        v["capacidadeCab"] = v.get("capacidadeCab", CAPACIDADE_CAB[k])
        v["ccd"] = coefs[k]["ccd"]
        v["cc"] = coefs[k]["cc"]

    antt["tabela"] = "A"
    antt["operacao"] = "Transporte Rodoviário de Carga Lotação"
    antt["tipoCargaReferencia"] = "Carga Geral"
    antt["ato"] = {
        "tipo": ato.get("tipo"),
        "numero": ato.get("numero"),
        "ano": ato.get("ano"),
        "data": ato.get("data"),
        "consolidadoEm": "Resolução ANTT nº 5.867/2020, Anexo II",
        "url": (args.fonte_url or None) if args.fixture else origem,
    }
    antt["ultimaAtualizacao"] = agora.strftime("%Y-%m-%dT%H:%M:%SZ")
    antt.pop("ultimaVerificacao", None)   # a data da consulta vive no log

    hist = doc.setdefault("historicoAntt", [])
    entrada = {
        "ato": f"{ato.get('tipo')} {ato.get('numero')}/{ato.get('ano')}",
        "data": ato.get("data"),
        "registradoEm": agora.strftime("%Y-%m-%d"),
        "3e": coefs["3e"], "6e": coefs["6e"], "9e": coefs["9e"],
    }
    ultimo = hist[-1] if hist else None
    igual_ao_ultimo = bool(ultimo) and all(
        ultimo.get(k) == entrada[k] for k in ("ato", "3e", "6e", "9e"))
    if not igual_ao_ultimo:
        hist.append(entrada)

    print("Status: NOVA TABELA" if mudou_coef else "Status: MESMOS COEFICIENTES, ATO NOVO")
    if args.dry_run:
        print("(dry-run: JSON não gravado)")
        return 0
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("JSON ATUALIZADO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
