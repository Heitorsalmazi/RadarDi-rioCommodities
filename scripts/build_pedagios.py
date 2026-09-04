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

3. TARIFA VENCIDA NÃO VIRA NULA. Ela fica, com `vigencia.status` dizendo que
   não foi confirmada. Apagar perderia a única referência disponível; fingir
   que é vigente seria pior ainda.

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
    """Deriva os campos do schema 2 que a interface consome.

    POR QUE ISTO EXISTE: a base v2 foi montada à mão na Fase 4 e o script
    gravava v1. Cada execução do workflow apagava os campos que o motor de
    status usa, e a interface passava a chamar de "sem tarifa" tudo o que na
    verdade era ausência de campo. Derivar aqui fecha esse buraco: o script
    volta a ser a única fonte do arquivo.

    Nada aqui inventa tarifa. Só nomeia o que já está — ou não está — no dado.
    """
    for p in pracas:
        t = p.get("tarifas") or {}
        valores = [(t.get(k) or {}).get("valor") for k in ("3e", "6e", "9e")]
        tem = any(v is not None for v in valores)

        # TRÊS COISAS DIFERENTES, e cada uma tem consequência própria:
        #   pracaConhecida — a praça consta da fonte oficial. Sempre verdadeira
        #                    aqui: se não constasse, não estaria nesta lista.
        #   posicionavel   — tem coordenada para casar com a geometria da rota.
        #                    As 12 rampas do Rodoanel não têm, e nem por isso
        #                    deixam de existir.
        #   tarifaDisponivel — a concessionária publica valor.
        # Colapsar as três num campo só foi o que fez a interface chamar de
        # "sem tarifa" praça que era só sem coordenada.
        p["pracaConhecida"] = True
        p["posicionavel"] = p.get("latitude") is not None and p.get("longitude") is not None
        p["tarifaDisponivel"] = tem

        if not tem:
            if p.get("regulador") == "ANTT":
                p["motivoSemTarifa"] = ("SEM_CATEGORIA_PUBLICADA"
                                        if (p.get("fontes") or {}).get("tarifa")
                                        else "SEM_FONTE_TARIFARIA")
            else:
                p["motivoSemTarifa"] = "SEM_FONTE_TARIFARIA"
        else:
            p["motivoSemTarifa"] = None

        # Tarifa unitária por eixo: só a ARTESP publica essa regra. No federal
        # a categoria é fechada, então NÃO se divide o valor de 6 eixos por 6.
        if p.get("regulador") == "ARTESP":
            v6 = (t.get("6e") or {}).get("valor")
            p["tarifaPorEixo"] = round(v6 / 6, 6) if v6 else None
        else:
            p["tarifaPorEixo"] = None

        if p.get("sentido") in (None, ""):
            p["sentido"] = None
            p.setdefault("fonteSentido", "SEM_FONTE_SENTIDO")
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
    locais = P.antt_localizacoes(diag)
    concs = sorted({x["concessionaria"] for x in locais})
    tarifas = {}
    for c in concs:
        t = P.antt_tarifas(c, diag)
        if t:
            tarifas[c] = t
    diag["antt_concessionarias_com_tarifa"] = sorted(tarifas)

    out = []
    for x in locais:
        t = tarifas.get(x["concessionaria"])
        v3 = v6 = None
        if t:
            if t["colunas"]:
                pref = x["nome"].split("-")[0].strip()
                if pref in t["colunas"]:
                    i = t["colunas"].index(pref)
                    v3 = (t["3e"] or [None] * 99)[i] if t["3e"] else None
                    v6 = (t["6e"] or [None] * 99)[i] if t["6e"] else None
            else:
                v3, v6 = t["3e"], t["6e"]
        out.append({
            "id": _id("ANTT", x["concessionaria"], x["nome"]),
            "regulador": "ANTT", "concessionaria": x["concessionaria"], "nome": x["nome"],
            "tipo": x["tipo"], "rodovia": x["rodovia"], "uf": x["uf"], "km": x["km"],
            "municipio": x["municipio"],
            "latitude": x["latitude"], "longitude": x["longitude"],
            "sentido": x["sentido"],
            "fonteSentido": "dataset ANTT praca-de-pedagio",
            "situacao": "ativo",
            "tarifas": {
                "3e": {"valor": v3, "metodo": "categoria_rodagem_dupla" if v3 else None},
                "6e": {"valor": v6, "metodo": "categoria_rodagem_dupla" if v6 else None},
                # A tabela federal termina em 8 eixos e não publica regra de
                # excedente. Somar 8e + 1e porque a série parece linear seria
                # inventar norma.
                "9e": {"valor": None, "metodo": "sem_regra_oficial"},
            },
            "vigencia": {"dataBase": None,
                         "status": "vigente" if v3 else "tarifa_ausente",
                         "ato": None},
            "fontes": {"localizacao": P.ANTT_CKAN,
                       "tarifa": t["url"] if t else None},
        })
    return out


def montar_artesp(diag):
    pracas, vig = P.artesp_coletar(diag)
    idx = carregar_municipios_sp()
    out, sem_geo = [], []
    for x in pracas:
        la, lo, ibge = geocodificar_sp(x["nome"], idx)
        if la is None:
            sem_geo.append(x["nome"])
        v = x["tarifaPorEixo"]
        out.append({
            "id": _id("ARTESP", x["rodovia"], x["nome"]),
            "regulador": "ARTESP", "concessionaria": None, "nome": x["nome"],
            "tipo": x["tipo"], "rodovia": x["rodovia"], "uf": "SP", "km": x["km"],
            "latitude": la, "longitude": lo, "codigoIbgeMunicipio": ibge,
            "sentido": x["sentido"], "cobranca": x["cobranca"],
            "fonteSentido": "ARTESP Histórico de Tarifas (Contratos Vigentes)",
            "situacao": "ativo",
            "tarifaPorEixo": v,
            # Regra oficial publicada: a coluna é "COMERCIAL POR EIXO".
            # Multiplicar é aplicar a norma, não inferir.
            "tarifas": {k: {"valor": round(v * n, 2), "metodo": "tarifa_comercial_por_eixo"}
                        for k, n in (("3e", 3), ("6e", 6), ("9e", 9))},
            "vigencia": dict(vig),
            "fontes": {"localizacao": "ARTESP · rodovia+km do documento oficial; coordenada = sede do município (IBGE)",
                       "tarifa": P.ARTESP_PEDAGIOS},
        })
    diag["artesp_sem_geocodificacao"] = sem_geo
    return out, vig


def validar(pracas, rotulo):
    if not pracas:
        return False, f"{rotulo}: nenhuma praça coletada"
    sem_coord = [p for p in pracas if p["latitude"] is None or p["longitude"] is None]
    if len(sem_coord) > len(pracas) * 0.2:
        return False, f"{rotulo}: {len(sem_coord)} de {len(pracas)} sem coordenada"
    for p in pracas:
        for k in ("3e", "6e", "9e"):
            v = (p["tarifas"][k] or {}).get("valor")
            if v is not None and (not isinstance(v, (int, float)) or v != v or v <= 0):
                return False, f"{rotulo}: {p['id']} tem {k} inválido ({v})"
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
    vig = None
    if not args.so_antt:
        try:
            novo, vig = montar_artesp(diag)
            ok, motivo = validar(novo, "ARTESP")
            if ok:
                artesp = novo
                cov_artesp = ("ok" if vig["status"] == "vigente" else "tarifa_desatualizada")
            else:
                diag["artesp_reprovou"] = motivo
                cov_artesp = "erro_validacao"
        except Exception as e:
            diag["artesp_falhou"] = str(e)[:200]
            cov_artesp = "fonte_indisponivel"
    if cov_artesp == "preservado" and artesp:
        st = (artesp[0].get("vigencia") or {}).get("status")
        cov_artesp = "ok" if st == "vigente" else "tarifa_desatualizada"
    print(f"ARTESP : {len(artesp)} praças · {cov_artesp}"
          + (f" · data-base {vig['dataBase']}" if vig else ""))

    if not antt and not artesp:
        print("Nenhum provider entregou dados e não há base anterior. Nada gravado.")
        return 1

    pracas = enriquecer(antt + artesp)

    doc = {
        "schemaVersion": 2,
        "_leiaMe": ("Praças e pórticos de pedágio com tarifa por configuração de eixos. Cada praça "
                    "carrega a própria vigência: tarifa cujo status não seja \"vigente\" NUNCA "
                    "promove a rota a COMPLETO. A coordenada das praças paulistas é a sede do "
                    "município do nome — o motor confirma exigindo também a ref da rodovia na rota."),
        "geradoEm": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {"ANTT": cov_antt, "ARTESP": cov_artesp},
        "providers": {
            "ANTT": {"pracas": len(antt),
                     "observacao": ("Tabela federal termina em 8 eixos e não publica regra de eixo "
                                    "excedente: 9e = null.")},
            "ARTESP": {"pracas": len(artesp),
                       "vigencia": vig or ((artesp[0].get("vigencia") if artesp else None)),
                       "observacao": ("Tarifa comercial publicada POR EIXO; 3e/6e/9e são a regra "
                                      "oficial multiplicada, não inferência.")},
        },
        "diagnostico": diag,
        # Campos que a interface consome para decidir status. Sem eles ela não
        # distingue "praça sem tarifa" de "trecho sem cobertura".
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
