#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BASE DE MUNICÍPIOS BRASILEIROS
════════════════════════════════════════════════════════════════════════════
Gera `data/municipios_brasil.json` com código IBGE, nome, UF e a coordenada
da sede de cada município. É essa base que alimenta os seletores de destino
do módulo Preço Colocado: o operador escolhe UF e cidade, e a coordenada sai
daqui, sem consulta externa nenhuma na hora do uso.

FONTE
  CSV público construído sobre a base de municípios do IBGE, com o código
  oficial de cada um. O código IBGE é o identificador estável do destino:
  nomes de município mudam de grafia, o código não.

POR QUE UM ARQUIVO ESTÁTICO E NÃO UMA API
  Geocodificar a cada tecla digitada seria abusar do Nominatim, ficaria
  lento e quebraria sem rede. Municípios brasileiros mudam uma vez a cada
  vários anos; um arquivo no repositório resolve para sempre e custa um
  fetch de 230 KB.

O QUE O SCRIPT NÃO FAZ
  Não inventa coordenada. Se o download falhar ou vier incompleto, o arquivo
  atual é preservado inteiro — uma base parcial seria pior que uma base
  antiga, porque a cidade faltando viraria um destino inexistente.

USO
    python3 scripts/update_municipios.py
"""

import json
import os
import sys
import csv
import io
import datetime
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQ = os.path.join(RAIZ, "data", "municipios_brasil.json")

FONTE_URL = ("https://raw.githubusercontent.com/kelvins/"
             "municipios-brasileiros/main/csv/municipios.csv")
FONTE_NOME = ("kelvins/municipios-brasileiros — CSV público construído sobre "
              "a base de municípios do IBGE")

UA = "RadarDiarioCommodities/1.0 (github.com/Heitorsalmazi/RadarDi-rioCommodities)"

# Códigos de UF do IBGE. Fixos desde 1988; não mudam.
UF_POR_CODIGO = {
    11:'RO', 12:'AC', 13:'AM', 14:'RR', 15:'PA', 16:'AP', 17:'TO',
    21:'MA', 22:'PI', 23:'CE', 24:'RN', 25:'PB', 26:'PE', 27:'AL', 28:'SE', 29:'BA',
    31:'MG', 32:'ES', 33:'RJ', 35:'SP',
    41:'PR', 42:'SC', 43:'RS',
    50:'MS', 51:'MT', 52:'GO', 53:'DF',
}

# O Brasil cabe nesta caixa, ilhas oceânicas incluídas. Serve para descartar
# coordenada corrompida sem precisar validar uma a uma.
LAT_MIN, LAT_MAX = -34.0, 6.0
LON_MIN, LON_MAX = -74.0, -28.0

MINIMO_ACEITAVEL = 5500   # o país tem 5.570; abaixo disso o download veio truncado


def main():
    print(f"Baixando {FONTE_URL}")
    try:
        req = urllib.request.Request(FONTE_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            bruto = r.read().decode("utf-8")
    except Exception as e:
        print(f"ERRO: download falhou — {e}")
        print("Base atual preservada. Nada foi alterado.")
        return 1

    linhas = list(csv.DictReader(io.StringIO(bruto)))
    print(f"{len(linhas)} linhas no CSV")

    por_uf = {}
    descartados = []
    for r in linhas:
        try:
            cod = int(r["codigo_ibge"])
            nome = (r["nome"] or "").strip()
            lat = float(r["latitude"])
            lon = float(r["longitude"])
            uf = UF_POR_CODIGO.get(int(r["codigo_uf"]))
        except (KeyError, TypeError, ValueError):
            descartados.append(str(r)[:60]); continue
        if not uf or not nome:
            descartados.append(str(r)[:60]); continue
        if not (LAT_MIN < lat < LAT_MAX) or not (LON_MIN < lon < LON_MAX):
            descartados.append(f"{nome}/{uf} fora da caixa"); continue
        por_uf.setdefault(uf, []).append([cod, nome, round(lat, 4), round(lon, 4)])

    total = sum(len(v) for v in por_uf.values())
    print(f"{total} municípios em {len(por_uf)} UFs · {len(descartados)} descartados")
    for d in descartados[:5]:
        print(f"    descartado: {d}")

    if total < MINIMO_ACEITAVEL:
        print(f"ERRO: só {total} municípios, abaixo do mínimo de {MINIMO_ACEITAVEL}.")
        print("Download provavelmente truncado. Base atual preservada.")
        return 1

    for uf in por_uf:
        por_uf[uf].sort(key=lambda x: x[1])

    doc = {
        "schemaVersion": 1,
        "_leiaMe": ("Municípios brasileiros com código IBGE e coordenada da sede. "
                    "Formato compacto: cada UF traz uma lista de "
                    "[codigoIbge, nome, latitude, longitude]. A coordenada é o PONTO "
                    "DE REFERÊNCIA DO MUNICÍPIO, não a porteira de uma fazenda — a "
                    "distância calculada chega à cidade, não ao destino final da viagem."),
        "fonte": FONTE_NOME,
        "fonteUrl": FONTE_URL,
        "dataAtualizacao": datetime.date.today().isoformat(),
        "total": total,
        "municipios": dict(sorted(por_uf.items())),
    }

    antigo = None
    if os.path.exists(ARQ):
        try:
            with open(ARQ, encoding="utf-8") as f:
                antigo = json.load(f)
        except Exception:
            antigo = None

    if antigo and antigo.get("municipios") == doc["municipios"]:
        print("Base sem alterações. Nenhum commit necessário.")
        return 0

    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"Base atualizada: {ARQ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
