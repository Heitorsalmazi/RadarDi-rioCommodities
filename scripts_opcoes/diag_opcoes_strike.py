"""
diag_opcoes_strike.py — Por que 712 séries vieram sem strike?
==============================================================
Diagnóstico read-only. NÃO grava nada, NÃO toca em planilha nenhuma.
Só baixa o cadastro da B3 e mostra como o campo ExrcPric está preenchido.

    python diag_opcoes_strike.py
    python diag_opcoes_strike.py --data 2026-09-02
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from collectors import opcoes_b3 as ob
from coleta_opcoes_b3 import ultimo_pregao


def main():
    ap = argparse.ArgumentParser(description="Diagnóstico do strike (read-only)")
    ap.add_argument("--data")
    args = ap.parse_args()
    d = date.fromisoformat(args.data) if args.data else ultimo_pregao()

    print("=" * 72)
    print(f"DIAGNÓSTICO DO STRIKE — pregão de {d:%d/%m/%Y}")
    print("Read-only: nada é gravado.")
    print("=" * 72)

    bruto = ob._baixar_csv(ob.ARQ_CADASTRO, d)
    texto = bruto.decode("latin-1", "replace")
    linhas = texto.splitlines()
    pular = 0 if linhas[0].startswith("RptDt") else 1
    cabecalho = linhas[pular].split(";")
    print(f"\n1. LAYOUT")
    print(f"   linha 0: {linhas[0][:70]}")
    print(f"   colunas no cabeçalho: {len(cabecalho)}")
    print(f"   posição de ExrcPric  : "
          f"{cabecalho.index('ExrcPric') if 'ExrcPric' in cabecalho else 'AUSENTE'}")

    larguras = Counter(l.count(";") + 1 for l in linhas[pular + 1:] if l.strip())
    print(f"\n2. LARGURA DAS LINHAS DE DADO (campos por linha)")
    for larg, n in larguras.most_common(5):
        marca = "  <- igual ao cabeçalho" if larg == len(cabecalho) else \
                "  <- DIVERGE do cabeçalho"
        print(f"   {larg} campos: {n:,} linha(s){marca}")

    df = ob._ler_csv(bruto, ob.ARQ_CADASTRO)
    opc = df[(df["SgmtNm"].str.strip().str.upper() == "AGRIBUSINESS") &
             (df["MktNm"].str.strip().str.upper() == "OPTIONS ON FUTURE") &
             (df["Asst"].isin(ob.MERCADOS))].copy()
    print(f"\n3. SÉRIES AGRO NO CADASTRO: {len(opc):,}")

    opc["_strike_num"] = ob._num(opc["ExrcPric"])
    sem = opc[opc["_strike_num"].isna()]
    print(f"   sem strike numérico: {len(sem):,}")

    if sem.empty:
        print("\n   Nada faltando — o problema é outro.")
        return 0

    print(f"\n4. QUEM ESTÁ SEM STRIKE")
    print(f"   {'ativo':<6} {'tipo':<6} {'sem':>6} {'total':>6}  vencimentos afetados")
    for asst in sorted(opc["Asst"].unique()):
        for tp in sorted(opc["OptnTp"].dropna().unique()):
            sub = opc[(opc["Asst"] == asst) & (opc["OptnTp"] == tp)]
            s = sub[sub["_strike_num"].isna()]
            if len(s):
                vencs = sorted(s["XprtnDt"].dropna().unique())[:4]
                print(f"   {asst:<6} {tp:<6} {len(s):>6} {len(sub):>6}  "
                      f"{', '.join(vencs)}")

    print(f"\n5. O QUE VEIO NO CAMPO ExrcPric DESSAS LINHAS")
    vals = Counter(sem["ExrcPric"].fillna("<vazio/NaN>").astype(str))
    for v, n in vals.most_common(8):
        print(f"   {n:>6}x  {v!r}")

    print(f"\n6. E NAS QUE DERAM CERTO (para comparar)")
    ok = opc[opc["_strike_num"].notna()]
    vals_ok = Counter(ok["ExrcPric"].astype(str))
    for v, n in list(vals_ok.most_common(6)):
        print(f"   {n:>6}x  {v!r}")

    print(f"\n7. LINHAS BRUTAS DAS PRIMEIRAS 3 SEM STRIKE")
    alvo = set(sem["TckrSymb"].head(3))
    achadas = 0
    for l in linhas[pular + 1:]:
        campos = l.split(";")
        if len(campos) > 1 and campos[1] in alvo:
            print(f"\n   {campos[1]}  ({len(campos)} campos)")
            print(f"   {l[:300]}")
            for i in (2, 5, 7, 19, 35, 36):
                nome = cabecalho[i] if i < len(cabecalho) else "?"
                val = campos[i] if i < len(campos) else "<fora da linha>"
                print(f"      [{i:>2}] {nome:<12} = {val!r}")
            achadas += 1
            if achadas >= 3:
                break

    print(f"\n8. LINHA BRUTA DE UMA QUE DEU CERTO")
    alvo_ok = ok["TckrSymb"].iloc[0]
    for l in linhas[pular + 1:]:
        campos = l.split(";")
        if len(campos) > 1 and campos[1] == alvo_ok:
            print(f"\n   {alvo_ok}  ({len(campos)} campos)")
            print(f"   {l[:300]}")
            for i in (2, 5, 7, 19, 35, 36):
                nome = cabecalho[i] if i < len(cabecalho) else "?"
                print(f"      [{i:>2}] {nome:<12} = {campos[i]!r}")
            break

    print(f"\n9. HÁ OUTRA COLUNA COM CARA DE STRIKE?")
    # Se o strike migrou de coluna, ela vai estar preenchida justamente
    # onde ExrcPric está vazio — e com números na faixa dos preços.
    for col in df.columns:
        if col in ("ExrcPric", "TckrSymb", "ISIN"):
            continue
        v = ob._num(sem[col]) if col in sem.columns else None
        if v is None or v.isna().all():
            continue
        preenchidas = v.notna().sum()
        if preenchidas >= len(sem) * 0.9:
            print(f"   {col:<18} {preenchidas:>6} preenchida(s)  "
                  f"faixa {v.min():.2f} a {v.max():.2f}")

    print("\n" + "=" * 72)
    print("Mande esta saída inteira que eu ajusto o coletor.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
