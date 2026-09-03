#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ATUALIZAÇÃO DA MATRIZ LOGÍSTICA DA REPOSIÇÃO
════════════════════════════════════════════════════════════════════════════
Mantém as coordenadas de origem em `data/logistica_reposicao.json`, usando
apenas fontes gratuitas:

  data/municipios_brasil.json  coordenada da sede   fonte primária, local
  Nominatim (OpenStreetMap)    geocodificação       só se a base não tiver
  OSRM público                 rota                 só se houver destino fixo

Custo de API: R$ 0. Nenhuma chave, nenhum crédito, nenhuma cobrança por
consulta.

O DESTINO É ESCOLHIDO NO NAVEGADOR, NÃO AQUI
───────────────────────────────────────────────────────────────────────────
Desde que o destino virou dinâmico — o operador escolhe UF e município na
tela —, não existe um destino único para pré-calcular. Pré-calcular rota
para 5.571 destinos × 15 origens seriam 83 mil consultas a um serviço
público gratuito, o que é abuso, não engenharia. As rotas passaram a ser
calculadas sob demanda no navegador e guardadas em cache local.

Sobrou para este script a parte que de fato é estável: a coordenada da sede
de cada praça de origem. E essa agora sai da base local de municípios,
ancorada no código IBGE — nenhuma chamada externa no caminho normal.

TRÊS REGRAS QUE O SCRIPT NÃO QUEBRA
───────────────────────────────────────────────────────────────────────────
1. NÃO INVENTA DADO. Sem resposta confiável, o campo continua `null`.
   Zero seria pior que ausência: zero entra na conta e produz uma decisão
   econômica errada em silêncio.

2. NÃO APAGA DADO BOM. Se a consulta falhar e já houver uma distância
   válida gravada, ela permanece. Uma instabilidade de trinta segundos no
   OSRM não pode zerar uma matriz que levou meses para ser montada.

3. NÃO SOBRECARREGA SERVIÇO PÚBLICO. Nominatim pede no máximo uma
   requisição por segundo e um User-Agent identificável; as duas coisas são
   respeitadas. Coordenada já conhecida não é consultada de novo — as
   cidades não mudam de lugar.

USO
    python3 scripts/update_logistica_reposicao.py
    python3 scripts/update_logistica_reposicao.py --forcar-geocodificacao
    python3 scripts/update_logistica_reposicao.py --somente MS/Campo\\ Grande
"""

import json
import os
import sys
import time
import argparse
import urllib.parse
import urllib.request
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQ = os.path.join(RAIZ, "data", "logistica_reposicao.json")
ARQ_MUN = os.path.join(RAIZ, "data", "municipios_brasil.json")

UA = "RadarDiarioCommodities/1.0 (github.com/Heitorsalmazi/RadarDi-rioCommodities)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"

PAUSA_NOMINATIM = 1.1   # segundos entre chamadas: o limite público é 1/s
PAUSA_OSRM = 0.4
TIMEOUT = 30

log = []


def diga(msg):
    print(msg, flush=True)
    log.append(msg)


def pedir_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def geocodificar(nome):
    """Devolve (lat, lon) ou None. Nunca levanta exceção para o chamador."""
    url = NOMINATIM + "?" + urllib.parse.urlencode({
        "q": nome, "format": "json", "limit": 1, "countrycodes": "br",
    })
    try:
        dados = pedir_json(url)
    except Exception as e:
        diga(f"    geocodificação falhou: {nome} — {e}")
        return None
    finally:
        time.sleep(PAUSA_NOMINATIM)
    if not dados:
        diga(f"    sem resultado no Nominatim: {nome}")
        return None
    try:
        return (float(dados[0]["lat"]), float(dados[0]["lon"]))
    except (KeyError, ValueError, TypeError):
        diga(f"    resposta inesperada do Nominatim: {nome}")
        return None


def distancia_rodoviaria(orig, dest):
    """Quilômetros por estrada, ou None. `orig`/`dest` são (lat, lon).

    OSRM recebe as coordenadas na ordem lon,lat — trocá-las produz uma rota
    plausível e completamente errada, então a ordem está explícita aqui."""
    url = (f"{OSRM}/{orig[1]},{orig[0]};{dest[1]},{dest[0]}"
           "?overview=false&alternatives=false&steps=false")
    try:
        dados = pedir_json(url)
    except Exception as e:
        diga(f"    OSRM falhou: {e}")
        return None
    finally:
        time.sleep(PAUSA_OSRM)
    if dados.get("code") != "Ok" or not dados.get("routes"):
        diga(f"    OSRM sem rota: code={dados.get('code')}")
        return None
    try:
        return round(dados["routes"][0]["distance"] / 1000.0, 1)
    except (KeyError, TypeError):
        return None


def coord_valida(o):
    return bool(o) and o.get("latitude") is not None and o.get("longitude") is not None


def carregar_base_municipal():
    """Índice {codigoIbge: (nome, uf, lat, lon)}. Vazio se a base não existir."""
    try:
        with open(ARQ_MUN, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        diga(f"Base municipal indisponível ({e}). Caio para o Nominatim quando precisar.")
        return {}
    idx = {}
    for uf, lista in (doc.get("municipios") or {}).items():
        for cod, nome, lat, lon in lista:
            idx[int(cod)] = (nome, uf, lat, lon)
    return idx


def coord_da_base(origem, idx):
    """Coordenada pelo código IBGE. O código é o vínculo estável: o nome do
    município pode mudar de grafia entre revisões da base, o código não.
    Devolve (lat, lon) ou None — nunca uma coordenada de outro município."""
    cod = origem.get("codigoIbge")
    if cod is None:
        return None
    reg = idx.get(int(cod))
    if not reg:
        diga(f"    código IBGE {cod} não está na base municipal")
        return None
    return (reg[2], reg[3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forcar-geocodificacao", action="store_true",
                    help="reconsulta coordenadas mesmo quando já existem")
    ap.add_argument("--somente", default=None, help="atualiza uma única chave de praça")
    args = ap.parse_args()

    if not os.path.exists(ARQ):
        diga(f"ERRO: {ARQ} não existe. Ele é versionado no repositório.")
        return 1

    with open(ARQ, encoding="utf-8") as f:
        doc = json.load(f)

    antes = json.dumps(doc, ensure_ascii=False, sort_keys=True)

    # NÃO EXISTE MAIS DESTINO NO ARQUIVO. Ele é escolhido na tela; aqui só
    # cuidamos das origens. O bloco `destino` foi removido do JSON justamente
    # para não sobrar um campo inerte que alguém volte a preencher por engano.
    destino = {}
    diga("Sem destino fixo, por desenho: o operador escolhe UF e município na tela")
    diga("e a rota é calculada lá, sob demanda, com cache. Aqui só atualizo as")
    diga("coordenadas de origem das praças.")

    idx_mun = carregar_base_municipal()
    if idx_mun:
        diga(f"Base municipal: {len(idx_mun)} municípios indexados por código IBGE.")

    rotas = doc.get("rotas") or {}
    alvos = [args.somente] if args.somente else list(rotas.keys())
    ok_geo = ok_rota = pulados = falhas = 0

    for chave in alvos:
        r = rotas.get(chave)
        if r is None:
            diga(f"[{chave}] não existe no JSON — ignorada")
            continue

        if r.get("tipoOrigem") != "municipio" or not r.get("origemRota"):
            pulados += 1
            continue

        diga(f"[{chave}] {r['origemRota']['nome']}")
        orig = r["origemRota"]

        if args.forcar_geocodificacao or not coord_valida(orig):
            # BASE LOCAL PRIMEIRO. Ela já tem a sede de todo município
            # brasileiro; chamar o Nominatim para descobrir onde fica Cuiabá
            # seria gastar um serviço público com uma pergunta já respondida
            # dentro do repositório.
            c = coord_da_base(orig, idx_mun)
            fonte = "base municipal (IBGE)"
            if not c:
                c = geocodificar(orig["nome"])
                fonte = "Nominatim"
            if c:
                antes_c = (orig.get("latitude"), orig.get("longitude"))
                orig["latitude"], orig["longitude"] = c
                if antes_c != c:
                    ok_geo += 1
                diga(f"    coordenada: {c[0]:.5f}, {c[1]:.5f} · {fonte}")
            else:
                falhas += 1
                # Preserva o que houver. Nunca zera.
                continue
        else:
            diga("    coordenada já conhecida, mantida")

        if not coord_valida(destino):
            continue

        km = distancia_rodoviaria((orig["latitude"], orig["longitude"]),
                                  (destino["latitude"], destino["longitude"]))
        if km is None:
            falhas += 1
            if r.get("distanciaKm") is not None:
                diga(f"    consulta falhou; distância anterior mantida: {r['distanciaKm']} km")
            continue

        r["distanciaKm"] = km
        r["fonteRota"] = "OSRM público sobre OpenStreetMap"
        r["atualizacao"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ok_rota += 1
        diga(f"    distância: {km} km")

    # CARIMBO SÓ QUANDO HOUVE ATUALIZAÇÃO DE VERDADE. Uma execução que
    # falhou em todas as consultas não "atualizou" nada, e gravar a data
    # mesmo assim faria a interface anunciar dado fresco onde não há.
    if ok_geo or ok_rota:
        doc["ultimaAtualizacao"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    depois = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    mudou = antes != depois

    if mudou:
        with open(ARQ, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")

    diga("")
    diga(f"geocodificadas: {ok_geo} · rotas: {ok_rota} · regionais puladas: {pulados} · falhas: {falhas}")
    diga(f"arquivo {'atualizado' if mudou else 'sem mudança'}")
    # Falha de rede não derruba o workflow: o dado antigo continua válido.
    return 0


if __name__ == "__main__":
    sys.exit(main())
