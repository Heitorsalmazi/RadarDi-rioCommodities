#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BASE NACIONAL DE PEDÁGIOS — montagem e validação
════════════════════════════════════════════════════════════════════════════
Junta o que os providers coletam em `data/pedagios_brasil.json`.

REGRAS QUE ESTE SCRIPT NÃO QUEBRA
────────────────────────────────────────────────────────────────────────────
1. TUDO OU NADA POR PROVIDER. Se a ANTT responder e a ARTESP cair, a parte
   federal é atualizada e a paulista ANTERIOR é preservada intacta. O que não
   acontece nunca é gravar meia base.

2. NÃO GRAVA SE NADA MUDOU. Comparação ignora o carimbo de tempo; senão todo
   dia haveria commit para registrar que nada aconteceu.

3. NÃO COLETA TARIFA. O custo virou praças × eixos × tarifa única, definida na
   interface. Este script entrega apenas ONDE ficam as praças — que é o que
   permite contá-las na rota.

4. NÃO INVENTA COORDENADA. A praça paulista é localizada pela sede do
   município do nome — aproximação declarada em `fontes.localizacao`, que o
   motor de rota confirma exigindo também a ref da rodovia.

USO
    python3 scripts/build_pedagios.py
    python3 scripts/build_pedagios.py --dry-run
    python3 scripts/build_pedagios.py --so-antt
"""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ARQ = os.path.join(RAIZ, "data", "pedagios_brasil.json")
ARQ_MUN = os.path.join(RAIZ, "data", "municipios_brasil.json")

import pedagios_providers as P   # noqa: E402

# Praças paulistas cujo nome não é o município. Explícito de propósito: um
# "casamento aproximado" silencioso colocaria a praça na cidade errada.
ALIAS_SP = {
    "STA CRUZ PALMEIRAS": "SANTA CRUZ DAS PALMEIRAS",
    "S. J. DA BOA VISTA": "SAO JOAO DA BOA VISTA",
    "ESP. SANTO DO PINHAL": "ESPIRITO SANTO DO PINHAL",
    "STA CRUZ DO RIO PARDO": "SANTA CRUZ DO RIO PARDO",
    "PRESIDENTE BERNADES": "PRESIDENTE BERNARDES",
    "MORRO DO ALTO (TATUI)": "TATUI",
    "MORRO DO ALTO (ITAPETININGA)": "ITAPETININGA",
    "GRAMADAO": "ITAPEVA",
    "PAULINIA A": "PAULINIA", "PAULINIA B": "PAULINIA",
    "PERUS": "SAO PAULO",
    "RIACHO GRANDE": "SAO BERNARDO DO CAMPO",
    "BATISTINI": "SAO BERNARDO DO CAMPO",
    "PIRATININGA": "SAO BERNARDO DO CAMPO",
    "DIADEMA": "DIADEMA", "ELDORADO": "DIADEMA",
}


def maiusc(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").upper().strip()


def _id(reg, conc, nome):
    return re.sub(r"[^A-Z0-9_]+", "_", f"{reg}_{maiusc(conc)}_{maiusc(nome)}")[:90]


def carregar_municipios_sp():
    with open(ARQ_MUN, encoding="utf-8") as f:
        mun = json.load(f)["municipios"]
    return {maiusc(m[1]): m for m in mun.get("SP", [])}


def geocodificar_sp(nome, indice):
    n = maiusc(nome).replace("(BLOQUEIO)", "").strip()
    n = ALIAS_SP.get(n, n)
    m = indice.get(n)
    return (m[2], m[3], m[0]) if m else (None, None, None)


def enriquecer(pracas):
    """Único campo derivado que sobrou: se a praça tem coordenada para casar
    com a geometria da rota. As 12 rampas do Rodoanel não têm — existem, mas
    não podem ser contadas automaticamente."""
    for p in pracas:
        p["posicionavel"] = p.get("latitude") is not None and p.get("longitude") is not None
        if p.get("sentido") in (None, ""):
            p["sentido"] = None
    return pracas

def corredores(pracas):
    """`UF|RODOVIA` de toda rodovia onde existe praça comprovada.

    Serve para o motor rebaixar uma rota SÓ quando há evidência de cobrança no
    trecho — e não sempre que a rota toca uma estadual qualquer."""
    fora = set()
    for p in pracas:
        uf, rod = (p.get("uf") or "").strip().upper(), (p.get("rodovia") or "").strip().upper()
        if uf and rod:
            fora.add(f"{uf}|{rod}")
    return sorted(fora)


def versao(pracas):
    """Impressão digital do conteúdo, para a interface invalidar cache sozinha.
    Não entra data: carimbo de tempo geraria commit diário sem mudança real."""
    corpo = json.dumps(pracas, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(corpo.encode("utf-8")).hexdigest()[:16]


def montar_antt(diag):
    """Só LOCALIZAÇÃO. As 36 páginas de tarifa do gov.br saíram: o custo passou
    a ser praças × eixos × tarifa única, e buscá-las era 8 min de workflow para
    um dado que 262 das 375 praças nem publicam."""
    locais = P.antt_localizacoes(diag)
    return [{
        "id": _id("ANTT", x["concessionaria"], x["nome"]),
        "regulador": "ANTT", "concessionaria": x["concessionaria"], "nome": x["nome"],
        "tipo": x["tipo"], "rodovia": x["rodovia"], "uf": x["uf"], "km": x["km"],
        "municipio": x["municipio"],
        "latitude": x["latitude"], "longitude": x["longitude"],
        "sentido": x["sentido"],
        "situacao": "ativo",
    } for x in locais]

def montar_artesp(diag):
    """Só LOCALIZAÇÃO. O PDF da ARTESP continua sendo lido — é dele que sai a
    lista das 98 praças com rodovia e km. Só as colunas de tarifa deixaram de
    ser aproveitadas."""
    pracas, _vig = P.artesp_coletar(diag)
    idx = carregar_municipios_sp()
    out, sem_geo = [], []
    for x in pracas:
        la, lo, ibge = geocodificar_sp(x["nome"], idx)
        if la is None:
            sem_geo.append(x["nome"])
        out.append({
            "id": _id("ARTESP", x["rodovia"], x["nome"]),
            "regulador": "ARTESP", "concessionaria": None, "nome": x["nome"],
            "tipo": x["tipo"], "rodovia": x["rodovia"], "uf": "SP", "km": x["km"],
            "latitude": la, "longitude": lo, "codigoIbgeMunicipio": ibge,
            "sentido": x["sentido"], "cobranca": x["cobranca"],
            "situacao": "ativo",
        })
    diag["artesp_sem_geocodificacao"] = sem_geo
    return out

def validar(pracas, rotulo):
    if not pracas:
        return False, f"{rotulo}: nenhuma praça coletada"
    sem_coord = [p for p in pracas if p["latitude"] is None or p["longitude"] is None]
    if len(sem_coord) > len(pracas) * 0.2:
        return False, f"{rotulo}: {len(sem_coord)} de {len(pracas)} sem coordenada"
    for p in pracas:
        if not p.get("nome") or not p.get("rodovia"):
            return False, f"{rotulo}: {p.get('id')} sem nome ou rodovia"
    return True, "ok"


def sem_carimbo(doc):
    d = json.loads(json.dumps(doc))
    d.pop("geradoEm", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--so-antt", action="store_true")
    ap.add_argument("--so-artesp", action="store_true")
    args = ap.parse_args()

    diag = {}
    antigo = None
    if os.path.exists(ARQ):
        try:
            with open(ARQ, encoding="utf-8") as f:
                antigo = json.load(f)
        except Exception:
            antigo = None

    def anteriores(reg):
        return [p for p in ((antigo or {}).get("pracas") or []) if p.get("regulador") == reg]

    # ── ANTT ────────────────────────────────────────────────────────────
    antt, cov_antt = anteriores("ANTT"), "preservado"
    if not args.so_artesp:
        try:
            novo = montar_antt(diag)
            ok, motivo = validar(novo, "ANTT")
            if ok:
                antt, cov_antt = novo, "ok"
            else:
                diag["antt_reprovou"] = motivo
                cov_antt = "erro_validacao"
        except Exception as e:
            diag["antt_falhou"] = str(e)[:200]
            cov_antt = "fonte_indisponivel"
    print(f"ANTT   : {len(antt)} praças · {cov_antt}")

    # ── ARTESP ──────────────────────────────────────────────────────────
    artesp, cov_artesp = anteriores("ARTESP"), "preservado"
    if not args.so_antt:
        try:
            novo = montar_artesp(diag)
            ok, motivo = validar(novo, "ARTESP")
            if ok:
                artesp, cov_artesp = novo, "ok"
            else:
                diag["artesp_reprovou"] = motivo
                cov_artesp = "erro_validacao"
        except Exception as e:
            diag["artesp_falhou"] = str(e)[:200]
            cov_artesp = "fonte_indisponivel"
    print(f"ARTESP : {len(artesp)} praças · {cov_artesp}")

    if not antt and not artesp:
        print("Nenhum provider entregou dados e não há base anterior. Nada gravado.")
        return 1

    pracas = enriquecer(antt + artesp)

    doc = {
        "schemaVersion": 3,
        "_leiaMe": ("LOCALIZAÇÃO das praças e pórticos de pedágio. NÃO guarda tarifa: o custo é "
                    "estimado na interface por praças × eixos × R$ 10,00/eixo/praça. Este arquivo "
                    "existe para uma coisa só — permitir CONTAR quantas praças a rota cruza, "
                    "cruzando estas coordenadas com a geometria do OSRM (que não devolve pedágio). "
                    "A coordenada das praças paulistas é a sede do município do nome; o motor "
                    "confirma exigindo também a ref da rodovia na rota."),
        "geradoEm": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {"ANTT": cov_antt, "ARTESP": cov_artesp},
        "providers": {"ANTT": {"pracas": len(antt)}, "ARTESP": {"pracas": len(artesp)}},
        "diagnostico": diag,
        "jurisdicoesComProvider": ["FEDERAL", "SP"],
        "corredoresConhecidos": corredores(pracas),
        "datasetVersion": versao(pracas),
        "pracas": pracas,
    }

    if antigo and sem_carimbo(antigo) == sem_carimbo(doc):
        print("Base sem alterações. Nenhum commit necessário.")
        return 0
    if args.dry_run:
        print("(dry-run: nada gravado)")
        return 0
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"BASE ATUALIZADA: {len(doc['pracas'])} praças")
    return 0


if __name__ == "__main__":
    sys.exit(main())
