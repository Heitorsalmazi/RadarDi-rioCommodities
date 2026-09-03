#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COEFICIENTES DA ANTT — DESCOBERTA E EXTRAÇÃO AUTOMÁTICAS
════════════════════════════════════════════════════════════════════════════
Mantém, em `data/logistica_reposicao.json`, os coeficientes vigentes da
Política Nacional de Pisos Mínimos do Transporte Rodoviário de Cargas:

    CCD  custo de deslocamento, R$ por quilômetro
    CC   custo de carga e descarga, R$ por operação

para 3, 6 e 9 eixos, na Tabela A, operação de Carga Lotação, tipo de carga
Carga Geral.

POR QUE CARGA GERAL
────────────────────────────────────────────────────────────────────────────
A ANTT não publica coeficiente para "bovinos vivos". A tabela tem tipos como
carga geral, granel sólido, frigorificada, perigosa. O transporte de boi em
caminhão-boiadeiro não tem linha própria, e inventar uma seria pior que usar
uma referência declarada. Carga Geral entra como BENCHMARK, e a interface diz
isso na tela.

O QUE ESTE SCRIPT NÃO FAZ
────────────────────────────────────────────────────────────────────────────
Não fixa o número da resolução. A ANTT altera os pisos por Resolução e também
por atualização extraordinária, que pode vir em Portaria. Procurar apenas por
"Resolução nº X" congelaria o Radar na tabela de 2026 sem ninguém perceber.
O script descobre o ato vigente a cada execução.

Não aceita tabela que não passe na validação. Se a ANTT mudar o HTML e o
parser trouxer lixo, o JSON continua com o último conjunto oficialmente
válido. Um coeficiente errado não deixa o frete "aproximado": deixa errado, e
errado com aparência de certo é a pior combinação possível.

Não transforma ausência em zero. CCD zero faria o frete virar só o CC, e a
conta fecharia sem reclamar.

USO
    python3 scripts/update_antt.py
    python3 scripts/update_antt.py --fixture testes/fixtures/antt_exemplo.html
    python3 scripts/update_antt.py --dry-run
"""

import argparse
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
TIMEOUT = 45

# Páginas oficiais. Nenhum blog, nenhuma notícia, nenhuma tabela copiada.
FONTES = [
    "https://www.gov.br/antt/pt-br/assuntos/transporte-rodoviario-de-cargas/politica-nacional-de-pisos-minimos-do-transporte-rodoviario-de-cargas",
    "https://www.gov.br/antt/pt-br/assuntos/transporte-rodoviario-de-cargas/piso-minimo",
]

EIXOS = {"3e": 3, "6e": 6, "9e": 9}

# Termos que precisam estar presentes para a página ser considerada a certa.
# Sem isso, um parser posicional aceitaria qualquer tabela numérica.
MARCADORES = ["tabela a", "carga geral", "ccd", "cc"]


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"\s+", " ", sem_acento(str(s or "")).lower()).strip()


def baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def descobrir_ato(html):
    """Identifica o ato normativo vigente citado na página.

    Aceita Resolução E Portaria: a atualização extraordinária dos coeficientes
    já veio por Portaria mais de uma vez, e um script que só procura Resolução
    perderia essas revisões em silêncio."""
    t = norm(html)
    achados = []
    # O ano pode vir logo depois do número ("6.084/2026") ou separado por
    # "de" e vírgulas ("nº 6.084, de 2026"). As duas grafias aparecem nas
    # páginas da ANTT, e aceitar só a primeira perderia metade dos atos.
    LIGA = r"(?:\s*[/,]?\s*(?:de\s+)?)"
    padroes = [
        (r"resolucao[^0-9]{0,40}(\d[\d\.]{2,8})" + LIGA + r"(20\d\d)", "Resolução"),
        (r"portaria[^0-9]{0,60}(\d[\d\.]{1,8})" + LIGA + r"(20\d\d)", "Portaria"),
    ]
    for rx, tipo in padroes:
        for m in re.finditer(rx, t):
            numero = m.group(1).replace(".", "")
            ano = int(m.group(2))
            achados.append({"tipo": tipo, "numero": numero, "ano": ano})
    if not achados:
        return None
    # O mais recente por ano, e entre os do mesmo ano o de maior número.
    achados.sort(key=lambda a: (a["ano"], int(a["numero"])))
    return achados[-1]


def _num(txt):
    """Converte '3,4567' e '3.4567' para float. Devolve None se não for número."""
    s = re.sub(r"[^\d,\.\-]", "", str(txt or ""))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")     # 1.234,56
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def extrair_tabela(html):
    """Procura, nas tabelas da página, a linha de Carga Geral e os coeficientes
    por número de eixos.

    A busca é SEMÂNTICA, não posicional: encontra a célula pelo texto do
    cabeçalho e da linha. Um parser que confiasse em "linha 12, coluna 4"
    quebraria em silêncio no dia em que a ANTT mexesse no layout, e devolveria
    números plausíveis vindos do lugar errado."""
    tabelas = re.findall(r"<table\b.*?</table>", html, re.S | re.I)
    for tab in tabelas:
        tnorm = norm(re.sub(r"<[^>]+>", " ", tab))
        if "carga geral" not in tnorm:
            continue
        # CCD e CC costumam ser explicados na LEGENDA, fora da tabela. Exigir
        # os dois dentro dela reprovava a tabela certa. A checagem dos
        # marcadores continua, só que sobre a página inteira, em `main`.
        if not re.search(r"\d+\s*eixo", tnorm):
            continue

        linhas = re.findall(r"<tr\b.*?</tr>", tab, re.S | re.I)
        matriz = []
        for tr in linhas:
            celulas = re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, re.S | re.I)
            matriz.append([re.sub(r"<[^>]+>", " ", c).strip() for c in celulas])
        if not matriz:
            continue

        cabecalho = None
        for row in matriz[:5]:
            if any("eixo" in norm(c) for c in row):
                cabecalho = row
                break
        if not cabecalho:
            continue

        # Coluna de cada configuração de eixos.
        col_de = {}
        for i, c in enumerate(cabecalho):
            m = re.search(r"(\d+)\s*eixo", norm(c))
            if m:
                col_de[int(m.group(1))] = i

        linha_geral = None
        for row in matriz:
            if any("carga geral" in norm(c) for c in row):
                linha_geral = row
                break
        if linha_geral is None:
            continue

        out = {}
        for chave, n in EIXOS.items():
            i = col_de.get(n)
            if i is None or i >= len(linha_geral):
                continue
            # A célula pode trazer CCD e CC juntos, separados por barra ou traço.
            nums = [_num(x) for x in re.split(r"[/|\-–—]| e ", linha_geral[i]) if _num(x) is not None]
            if len(nums) >= 2:
                out[chave] = {"ccd": nums[0], "cc": nums[1]}
        if len(out) == 3:
            return out
    return None


def validar(coefs):
    """Todos os seis números precisam existir, ser finitos e ser positivos."""
    if not coefs or len(coefs) != 3:
        return False, "faltam configurações de eixos"
    for k in ("3e", "6e", "9e"):
        c = coefs.get(k) or {}
        for campo in ("ccd", "cc"):
            v = c.get(campo)
            if v is None:
                return False, f"{k}.{campo} ausente"
            if not isinstance(v, (int, float)) or v != v:
                return False, f"{k}.{campo} não é número"
            if v <= 0:
                return False, f"{k}.{campo} = {v}, deveria ser positivo"
    # O CCD de 9 eixos é maior que o de 3: caminhão maior custa mais por km.
    if coefs["9e"]["ccd"] <= coefs["3e"]["ccd"]:
        return False, "CCD de 9 eixos não é maior que o de 3 — tabela suspeita"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=None, help="HTML local, para teste")
    ap.add_argument("--dry-run", action="store_true", help="não grava o JSON")
    args = ap.parse_args()

    print("ANTT — verificando...")

    if not os.path.exists(ARQ):
        print(f"ERRO: {ARQ} não existe.")
        return 1
    with open(ARQ, encoding="utf-8") as f:
        doc = json.load(f)
    antt = doc.setdefault("antt", {})
    ato_atual = (antt.get("ato") or {})
    print(f"Ato armazenado: {ato_atual.get('tipo')} {ato_atual.get('numero')}/{ato_atual.get('ano')}")

    html, origem = None, None
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            html = f.read()
        origem = "fixture: " + args.fixture
    else:
        for url in FONTES:
            try:
                html = baixar(url); origem = url
                print(f"Fonte: {url}")
                break
            except Exception as e:
                print(f"    falhou: {url} — {e}")
    if not html:
        print("Nenhuma fonte oficial respondeu. Coeficientes anteriores preservados.")
        return 1

    tn = norm(html)
    faltando = [m for m in MARCADORES if m not in tn]
    if faltando:
        print(f"Página sem os marcadores esperados: {faltando}")
        print("O layout pode ter mudado. Coeficientes anteriores preservados.")
        return 1

    ato = descobrir_ato(html)
    print(f"Ato encontrado: {ato}")

    coefs = extrair_tabela(html)
    if not coefs:
        print("Não localizei a linha de Carga Geral na Tabela A.")
        print("Coeficientes anteriores preservados.")
        return 1

    ok, motivo = validar(coefs)
    print("Tabela: A · Referência: Carga Geral")
    for k in ("3e", "6e", "9e"):
        c = coefs.get(k, {})
        print(f"  {k}: CCD {c.get('ccd')}  CC {c.get('cc')}")
    if not ok:
        print(f"VALIDAÇÃO REPROVOU: {motivo}")
        print("Coeficientes anteriores preservados.")
        return 1

    veic = antt.setdefault("veiculos", {})
    mudou = False
    for k, n in EIXOS.items():
        v = veic.setdefault(k, {"eixos": n, "capacidadeCab": {"3e":25,"6e":70,"9e":110}[k]})
        if v.get("ccd") != coefs[k]["ccd"] or v.get("cc") != coefs[k]["cc"]:
            mudou = True

    agora = datetime.now(timezone.utc)
    antt["ultimaVerificacao"] = agora.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ATO MAIS RECENTE NÃO É, POR SI, NOVA REFERÊNCIA. A ANTT publica muita
    # coisa; uma Portaria pode tratar de outro assunto e apenas citar a
    # tabela vigente. Se os seis coeficientes são idênticos aos que já estão
    # guardados, nada mudou de fato — trocar o número do ato ali daria a
    # impressão de atualização que não houve.
    if not mudou:
        print("Status: SEM ALTERAÇÃO — coeficientes idênticos aos vigentes")
        if ato and ato_atual.get("numero") != ato.get("numero"):
            print(f"    (ato citado na página é {ato['tipo']} {ato['numero']}/{ato['ano']}, "
                  f"mas não alterou a tabela: referência mantida)")
        if not args.dry_run:
            with open(ARQ, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2); f.write("\n")
        return 0

    for k, n in EIXOS.items():
        veic[k]["eixos"] = n
        veic[k]["ccd"] = coefs[k]["ccd"]
        veic[k]["cc"] = coefs[k]["cc"]

    antt["tabela"] = "A"
    antt["operacao"] = "Transporte Rodoviário de Carga Lotação"
    antt["tipoCargaReferencia"] = "Carga Geral"
    antt["ato"] = {
        "tipo": (ato or {}).get("tipo"),
        "numero": (ato or {}).get("numero"),
        "ano": (ato or {}).get("ano"),
        "dataPublicacao": None,
        "inicioVigencia": None,
        "url": origem if not args.fixture else None,
    }
    antt["ultimaAtualizacao"] = agora.strftime("%Y-%m-%dT%H:%M:%SZ")

    hist = doc.setdefault("historicoAntt", [])
    entrada = {
        "ato": f"{(ato or {}).get('tipo')} {(ato or {}).get('numero')}/{(ato or {}).get('ano')}",
        "registradoEm": agora.strftime("%Y-%m-%d"),
        "3e": coefs["3e"], "6e": coefs["6e"], "9e": coefs["9e"],
    }
    # Só entra no histórico o que for de fato diferente do último registro.
    if not hist or {k: hist[-1].get(k) for k in ("3e","6e","9e")} != {k: entrada[k] for k in ("3e","6e","9e")}:
        hist.append(entrada)

    print("Status: NOVA TABELA DETECTADA")
    if args.dry_run:
        print("(dry-run: JSON não gravado)")
        return 0
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2); f.write("\n")
    print("JSON ATUALIZADO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
