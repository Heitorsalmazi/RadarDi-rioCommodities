"""
collectors/opcoes_b3.py — Cadeia de opções agropecuárias da B3
================================================================
Captura TODAS as séries de opções sobre futuros agro de um pregão: BGI
(boi gordo), CCM (milho), SJC (mini soja CME) e SOY (soja FOB Santos),
PUT e CALL, com e sem negócio.

FONTE — API DE ARQUIVOS DA B3, EM DUAS ETAPAS
----------------------------------------------
    GET /api/download/requestname?fileName=<NOME>&date=AAAA-MM-DD
        -> {"token": "...", "file": {"name": "...", "extension": ".csv"}}
    GET /api/download/?token=<TOKEN>
        -> CSV separado por ponto-e-vírgula

Três arquivos por pregão, cada um com um papel:

    InstrumentsConsolidatedFile      cadastro   tipo, strike, vencimento
    TradeInformationConsolidatedFile negociação preços, referência, volume
    DerivativesOpenPositionFile      posição    contratos em aberto

A junção dos três pelo ticker é o que produz a cadeia completa. Sozinho,
o de negociação traz apenas as séries que tiveram fato relevante no dia.

O CADASTRO MANDA, O TICKER NÃO
-------------------------------
PUT/CALL vem de OptnTp; strike de ExrcPric; vencimento de XprtnDt. O
ticker (BGIF27C034000 = ativo + mês + ano + C/P + strike×100) é guardado
como identificador, mas nunca interpretado: se a B3 mudar a convenção, o
cadastro continua correto e o parse do ticker estaria errado em silêncio.

PARTICULARIDADE QUE MUDA TUDO
------------------------------
Em 02/09/2026 havia 1.872 séries agro e apenas 40 com negócio. A cadeia
existe por causa do RefPric (preço de referência), que NÃO é executável.
Por isso toda linha carrega origem_premio_calculo dizendo de onde veio o
prêmio usado nos cálculos — misturar preço negociado com referência numa
mesma coluna produziria estatística sem sentido lá na frente.

O QUE ESTA FONTE NÃO TEM
-------------------------
Bid e ask. Os arquivos públicos não trazem melhor compra nem melhor
venda. As colunas existem no esquema, sempre vazias, para o dia em que
houver fonte. Nada é estimado a partir de RefPric — e é por isso que
premio_executavel fica ausente em toda a base: sem livro de ofertas,
não dá para afirmar a que preço daria para operar.

LIGAÇÃO OPÇÃO -> FUTURO
------------------------
Pela RAIZ DO TICKER (SJCX26C001100 -> SJCX26), nunca pela data. A raiz
designa o CONTRATO FUTURO OBJETO, não o mês em que a opção vence.

O SJC tem opções SERIAIS: a opção de outubro tem como objeto o futuro de
novembro, a de dezembro o de janeiro, e assim por diante. O X de SJCX26 é
o contrato de novembro; a opção correspondente vence em 23/10. Casar por
igualdade de data é errado por construção — funcionava em BGI e CCM só
porque lá as opções são regulares e vencem junto com o futuro.

As colunas tipo_vencimento_opcao e mes_futuro_objeto deixam essa
distinção explícita para quem consumir a base. A convenção é declarada
POR PRODUTO em CONVENCAO_VENCIMENTO: o que vale para SJC não se presume
para os demais, e produto sem convenção conhecida fica INDETERMINADO.

UNIDADES
--------
Dentro de cada mercado, preço do futuro, strike e prêmio estão na MESMA
unidade, então moneyness e piso/teto saem coerentes sem conversão:

    BGI  BRL/arroba              CCM  BRL/saca de 60 kg
    SJC  USD/saca de 60 kg       SOY  USD/tonelada (contrato de 34 t)

O SJC já vem convertido do Mini-Sized Soybean do CME pela B3 (fator
0,0220462): um AdjstdQt de 28,8856 é US$ 28,8856 por saca, cerca de 1.310
cents/bushel na referência CME. Não converter de novo.

A unidade vem da ESPECIFICAÇÃO DO PRODUTO, não do campo TradgCcy do
cadastro — que no SOY traz BRL nas opções e USD no futuro, uma
inconsistência do próprio arquivo da B3.

FRAGILIDADE CONHECIDA
---------------------
A interface web de arquivos.b3.com.br foi desativada em 31/03/2026; a API
segue respondendo, mas é infraestrutura legada. O substituto (/bdi/) só
guarda 21 dias contra ~6,8 anos desta. Daí a verificação de layout a cada
execução: se as colunas mudarem, a coleta falha alto em vez de gravar
lixo.
"""

from __future__ import annotations

import gzip
import io
import json
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

VERSAO_COLETOR = "1.4.0"

API = "https://arquivos.b3.com.br/api/download"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

ARQ_CADASTRO = "InstrumentsConsolidatedFile"
ARQ_NEGOCIACAO = "TradeInformationConsolidatedFile"
ARQ_POSICAO = "DerivativesOpenPositionFile"

PASTA_BRUTO = Path(__file__).parent.parent / "temp" / "opcoes_b3"

# Mercados capturados. A chave é o campo Asst do cadastro da B3.
# SJC é o contrato de soja com cadeia real (109 calls / 109 puts em
# 02/09/2026). SOY existe e é mantido por completude, mas tinha um único
# strike, sem negócio e sem posição em aberto.
MERCADOS = {
    "BGI": "BOI GORDO",
    "CCM": "MILHO",
    "SJC": "SOJA",
    "SOY": "SOJA FOB",
}

SEGMENTO = "AGRIBUSINESS"
MERCADO_OPCOES = "OPTIONS ON FUTURE"
MERCADO_FUTUROS = "FUTURE"

# Código de mês dos contratos futuros, convenção de bolsa.
# É o que a letra da raiz do ticker designa: SJCX26 é o contrato de
# NOVEMBRO de 2026, ainda que ele liquide em 29/10 na B3.
MES_CODIGO = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
              "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

# Unidade em que preço, strike e prêmio são cotados. Como as três estão
# na mesma unidade dentro de cada mercado, moneyness e piso/teto saem
# coerentes sem conversão nenhuma.
#
# SJC: USD por saca de 60 kg. O contrato deriva do Mini-Sized Soybean do
# CME pelo fator 0,0220462, e a B3 já publica o resultado convertido.
# Um AdjstdQt de 28,8856 é US$ 28,8856/saca, equivalente a ~1.310 cents
# por bushel no CME. NÃO converter de novo — o número já está pronto.
#
# SOY: USD por tonelada métrica, contrato de 34 t, referência Platts.
# O cadastro bruto traz TradgCcy=BRL nas opções e USD no futuro — uma
# inconsistência do próprio arquivo. A unidade econômica do contrato vem
# da ESPECIFICAÇÃO DO PRODUTO, não do campo de moeda do cadastro.
UNIDADE_PRECO = {
    "BGI": "BRL/arroba",
    "CCM": "BRL/saca_60kg",
    "SJC": "USD/saca_60kg",
    "SOY": "USD/t",
}

# Produto liberado para estatística, ou ainda em verificação.
#
# SOY está PENDENTE por um motivo específico e limitado: o cadastro traz
# TradgCcy=BRL nas opções e USD no futuro — é o único produto agro com
# essa divergência. O campo não entra em cálculo nenhum, e a checagem de
# paridade put-call mostra que os números estão coerentes:
#
#     CALL K=47 -> 483,14   PUT K=47 -> 0,01   futuro -> 537,60
#     C - P = 483,13   e   F - K = 490,60   -> razão 0,985
#     44 dias até o vencimento -> desconto implícito de 13,6% a.a.
#
# Ou seja: strike, prêmio e futuro estão todos em USD/t, e o moneyness de
# -91% é uma CALL legitimamente muito dentro do dinheiro, não erro de
# escala. Ainda assim o produto fica fora da estatística até a divergência
# do TradgCcy ser explicada na fonte — as 2 séries de SOY não têm negócio
# nem posição em aberto, então nada se perde com a espera.
#
# Para liberar: trocar "PENDENTE" por "OK" aqui. Nada mais.
STATUS_VALIDACAO_PRODUTO = {
    "BGI": "OK",
    "CCM": "OK",
    "SJC": "OK",
    "SOY": "PENDENTE",
}

# De onde vem o preço do futuro objeto.
FONTE_PRECO_OBJETO = {
    "BGI": "B3_DIRETO",
    "CCM": "B3_DIRETO",
    "SJC": "SJC_B3_CME_CONVERTIDO",
    "SOY": "SOY_B3_PLATTS",
}

# Convenção de vencimento das opções, POR PRODUTO.
#
# Isto é deliberadamente uma tabela por produto, e não uma regra geral.
# Diferença entre o mês da opção e o mês do futuro objeto significa
# "serial" no SJC, mas não se pode presumir que valha para todo produto
# da B3 — cada contrato tem sua especificação, e generalizar a partir de
# um caso é como o erro que já cometemos aqui.
#
#   "SERIAL_E_REGULAR"  o produto tem os dois tipos; a classificação sai
#                       da comparação entre os meses, convenção confirmada
#                       na especificação da B3
#   "REGULAR"           o produto só tem opções regulares; a opção vence
#                       junto com o futuro objeto
#
# Produto fora desta tabela fica INDETERMINADO: melhor admitir que não se
# sabe do que rotular por analogia.
CONVENCAO_VENCIMENTO = {
    # Opções seriais confirmadas: fev->mar, abr->mai, jun->jul,
    # out->nov, dez->jan. Em 02/09/2026 as cinco séries listadas eram
    # todas seriais.
    "SJC": "SERIAL_E_REGULAR",
    "BGI": "REGULAR",
    "CCM": "REGULAR",
    "SOY": "REGULAR",
}

# Colunas que precisam existir. Se alguma sumir, o layout mudou e a
# execução para — nunca grava com coluna faltando.
OBRIGATORIAS = {
    ARQ_CADASTRO: ["RptDt", "TckrSymb", "Asst", "AsstDesc", "SgmtNm", "MktNm",
                   "XprtnDt", "XprtnCd", "OptnTp", "ExrcPric", "TradgCcy"],
    ARQ_NEGOCIACAO: ["RptDt", "TckrSymb", "SgmtNm", "MinPric", "MaxPric",
                     "TradAvrgPric", "LastPric", "AdjstdQt", "RefPric",
                     "TradQty", "FinInstrmQty", "NtlFinVol"],
    ARQ_POSICAO: ["RptDt", "TckrSymb", "Asst", "OpnIntrst"],
}

TENTATIVAS = 4
ESPERA_BASE = 4.0

_log_fn = None


class FalhaColeta(Exception):
    """Não foi possível obter dado confiável — o histórico não é tocado."""


class LayoutMudou(FalhaColeta):
    """A estrutura do arquivo da B3 mudou. Parar antes de gravar lixo."""


def _log(m, n="info"):
    if _log_fn:
        _log_fn(m, n)
    else:
        print({"info": "  ", "warning": "! ", "error": "X "}.get(n, "  ") + m,
              flush=True)


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _http(url: str, timeout: int = 120) -> bytes:
    """
    GET com backoff.

    O Cloudflare da B3 devolve 504 de forma intermitente em pedidos
    perfeitamente válidos — repetir resolve. Sem isso, uma coleta diária
    falharia sem motivo algumas vezes por semana.
    """
    espera = ESPERA_BASE
    ultimo = None
    for n in range(1, TENTATIVAS + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ctx()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            ultimo = f"HTTP {e.code}"
            if e.code == 400:
                raise FalhaColeta("HTTP 400 — data sem pregão ou fora do "
                                  "histórico disponível")
            if e.code in (429, 500, 502, 503, 504) and n < TENTATIVAS:
                pausa = espera + random.uniform(0, 2)
                _log(f"{ultimo} — nova tentativa em {pausa:.0f}s "
                     f"({n}/{TENTATIVAS})", "warning")
                time.sleep(pausa)
                espera *= 2
                continue
            break
        except Exception as e:
            ultimo = type(e).__name__
            if n < TENTATIVAS:
                pausa = espera + random.uniform(0, 2)
                _log(f"{ultimo} — nova tentativa em {pausa:.0f}s "
                     f"({n}/{TENTATIVAS})", "warning")
                time.sleep(pausa)
                espera *= 2
                continue
    raise FalhaColeta(f"{url.split('?')[0]} — {ultimo}")


def _baixar_csv(nome: str, d: date) -> bytes:
    """Resolve o token e baixa o CSV do arquivo pedido."""
    url = f"{API}/requestname?fileName={nome}&date={d.isoformat()}"
    bruto = _http(url, timeout=60)

    if not bruto.strip():
        raise FalhaColeta(f"{nome} {d}: resposta vazia na etapa do token")
    try:
        meta = json.loads(bruto)
    except json.JSONDecodeError:
        # Página de erro em HTML nunca deve ser confundida com dado
        trecho = bruto[:120].decode("utf-8", "ignore")
        raise LayoutMudou(f"{nome}: esperava JSON, veio outra coisa: {trecho}")

    token = meta.get("token")
    if not token:
        raise FalhaColeta(f"{nome} {d}: resposta sem token — {meta}")

    dados = _http(f"{API}/?token={token}", timeout=300)
    if dados[:15].lstrip().startswith(b"<"):
        raise LayoutMudou(f"{nome}: o download devolveu HTML, não CSV")
    return dados


def _ler_csv(dados: bytes, nome: str) -> pd.DataFrame:
    """
    Converte o CSV da B3 em DataFrame.

    Dois formatos convivem: a maioria dos arquivos abre com a linha
    'Status do Arquivo: Final' e traz o cabeçalho na linha 2; o de posições
    em aberto começa direto no cabeçalho. Detectar em vez de fixar evita
    ler os nomes das colunas como se fossem dado.
    """
    texto = dados.decode("latin-1", "replace")
    linhas = texto.splitlines()
    if not linhas:
        raise FalhaColeta(f"{nome}: arquivo vazio")

    pular = 0
    status = ""
    if not linhas[0].startswith("RptDt"):
        status = linhas[0].strip()
        pular = 1
        if "Final" not in status and "final" not in status:
            _log(f"{nome}: status do arquivo é '{status}' — o dado pode "
                 f"ainda estar sendo publicado", "warning")

    df = pd.read_csv(io.StringIO("\n".join(linhas[pular:])), sep=";",
                     dtype=str, keep_default_na=False, na_values=[""],
                     low_memory=False)
    df.attrs["status_arquivo"] = status

    faltando = [c for c in OBRIGATORIAS[nome] if c not in df.columns]
    if faltando:
        raise LayoutMudou(
            f"{nome}: colunas ausentes {faltando}. "
            f"Recebidas: {list(df.columns)[:20]}")
    return df


def _num(serie: pd.Series) -> pd.Series:
    """
    Texto da B3 para número.

    Decimal é vírgula e milhar é ponto: '340,5' e '1.234,5'. Vazio
    permanece ausente — NaN é 'não houve', que é informação, e virar zero
    apagaria isso.

    O desvio de saída só vale para coluna JÁ numérica. A versão anterior
    testava 'dtype != object', o que parece equivalente e não é: no pandas
    3 o dtype=str do read_csv devolve StringDtype, não object. O atalho
    disparava, a vírgula nunca era trocada, e o resultado foi silencioso e
    traiçoeiro — strikes inteiros ('340') convertiam, meio-strikes
    ('340,5') viravam NaN. Numa cadeia de opções isso derruba justamente
    metade das séries.
    """
    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_numeric(serie, errors="coerce")
    limpo = (serie.astype(str).str.strip()
             .str.replace(".", "", regex=False)
             .str.replace(",", ".", regex=False))
    limpo = limpo.where(~limpo.isin(["", "nan", "None", "<NA>", "-"]))
    return pd.to_numeric(limpo, errors="coerce")


def _guardar_bruto(d: date, cad: pd.DataFrame, neg: pd.DataFrame,
                   pos: pd.DataFrame) -> Path:
    """
    Arquiva o recorte agro dos três arquivos, comprimido.

    Guardar os CSV inteiros seria ~750 MB por ano para preservar linhas
    que nunca vamos olhar. O recorte AGRIBUSINESS tem alguns milhares de
    linhas e permite reprocessar qualquer pregão sem rebaixar da B3 —
    que é para o que serve o bruto.
    """
    PASTA_BRUTO.mkdir(parents=True, exist_ok=True)
    destino = PASTA_BRUTO / f"opcoes_b3_{d.isoformat()}.csv.gz"
    partes = []
    for rotulo, df in (("CADASTRO", cad), ("NEGOCIACAO", neg),
                       ("POSICAO", pos)):
        partes.append(f"### {rotulo} ### {len(df)} linha(s)")
        partes.append(df.to_csv(sep=";", index=False))
    with gzip.open(destino, "wt", encoding="utf-8") as f:
        f.write("\n".join(partes))
    return destino


def coletar(d: date, log=None, guardar_bruto: bool = True) -> dict:
    """
    Cadeia de opções agro de um pregão.

    Returns:
        {"data": date, "opcoes": DataFrame, "futuros": DataFrame,
         "bruto": Path|None, "status_arquivo": str}

    Raises:
        LayoutMudou: estrutura da B3 diferente do esperado
        FalhaColeta: pregão indisponível ou erro de rede
    """
    global _log_fn
    if log:
        _log_fn = log

    _log(f"[b3] baixando os 3 arquivos de {d:%d/%m/%Y}")
    cad = _ler_csv(_baixar_csv(ARQ_CADASTRO, d), ARQ_CADASTRO)
    neg = _ler_csv(_baixar_csv(ARQ_NEGOCIACAO, d), ARQ_NEGOCIACAO)
    try:
        pos = _ler_csv(_baixar_csv(ARQ_POSICAO, d), ARQ_POSICAO)
    except FalhaColeta as e:
        # Posição em aberto é enriquecimento, não identidade: sem ela a
        # cadeia continua correta, só perde o open interest.
        _log(f"[b3] posições em aberto indisponíveis ({e}) — "
             f"contratos_abertos ficará vazio", "warning")
        pos = pd.DataFrame(columns=OBRIGATORIAS[ARQ_POSICAO])

    status = cad.attrs.get("status_arquivo", "")

    # ── recorte agro ─────────────────────────────────────────────────────
    agro = cad[cad["SgmtNm"].str.strip().str.upper() == SEGMENTO]
    opc = agro[agro["MktNm"].str.strip().str.upper() == MERCADO_OPCOES].copy()
    fut = agro[agro["MktNm"].str.strip().str.upper() == MERCADO_FUTUROS].copy()
    opc = opc[opc["Asst"].isin(MERCADOS)].copy()
    fut = fut[fut["Asst"].isin(MERCADOS)].copy()

    if opc.empty:
        raise FalhaColeta(
            f"{d}: nenhuma opção agro no cadastro. Segmentos presentes: "
            f"{sorted(cad['SgmtNm'].dropna().unique())[:8]}")

    neg_i = neg.drop_duplicates("TckrSymb").set_index("TckrSymb")
    pos_i = (pos.drop_duplicates("TckrSymb").set_index("TckrSymb")
             if not pos.empty else pd.DataFrame())

    bruto = None
    if guardar_bruto:
        bruto = _guardar_bruto(
            d, pd.concat([opc, fut]),
            neg[neg["TckrSymb"].isin(set(opc["TckrSymb"]) | set(fut["TckrSymb"]))],
            pos[pos["TckrSymb"].isin(set(opc["TckrSymb"]) | set(fut["TckrSymb"]))]
            if not pos.empty else pos)

    futuros = _montar_futuros(d, fut, neg_i, pos_i)
    opcoes = _montar_opcoes(d, opc, neg_i, pos_i, futuros)

    for asst, nome in MERCADOS.items():
        sub = opcoes[opcoes["mercado_codigo"] == asst]
        if sub.empty:
            _log(f"[b3] {asst} ({nome}): nenhuma série listada", "warning")
        else:
            c = (sub["tipo_opcao"] == "CALL").sum()
            p = (sub["tipo_opcao"] == "PUT").sum()
            _log(f"[b3] {asst} ({nome}): {p} PUT, {c} CALL, "
                 f"{int(sub['tem_negocio'].sum())} com negócio")

    return {"data": d, "opcoes": opcoes, "futuros": futuros,
            "bruto": bruto, "status_arquivo": status}


def _montar_futuros(d, fut, neg_i, pos_i) -> pd.DataFrame:
    """Futuros agro do pregão, com ajuste e último preço."""
    if fut.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "data": d.isoformat(),
        "mercado_codigo": fut["Asst"].values,
        "mercado": fut["Asst"].map(MERCADOS).values,
        "ticker_futuro": fut["TckrSymb"].values,
        "vencimento_codigo": fut["XprtnCd"].values,
        "data_vencimento": fut["XprtnDt"].values,
        "moeda": fut["TradgCcy"].values,
    })
    n = fut["TckrSymb"].map(lambda t: neg_i.loc[t] if t in neg_i.index else None)
    for col, orig in (("ajuste", "AdjstdQt"), ("ultimo_preco", "LastPric"),
                      ("preco_referencia", "RefPric"),
                      ("volume_financeiro", "NtlFinVol"),
                      ("quantidade_contratos", "FinInstrmQty")):
        out[col] = _num(pd.Series(
            [r[orig] if r is not None else None for r in n], index=out.index))
    if not pos_i.empty:
        out["contratos_abertos"] = _num(pd.Series(
            [pos_i.loc[t, "OpnIntrst"] if t in pos_i.index else None
             for t in fut["TckrSymb"]], index=out.index))
    else:
        out["contratos_abertos"] = pd.NA
    return out.sort_values(["mercado_codigo", "data_vencimento"])


# ─────────────────────────────────────────────────────────────────────────────
# Liquidez
# ─────────────────────────────────────────────────────────────────────────────
# A fonte pública não traz bid nem ask, então o critério se apoia no que
# existe: houve negócio, e há posição em aberto. Os limiares ficam aqui,
# nomeados, para poderem ser discutidos e mudados sem caçar número solto
# no meio do código.

MIN_NEGOCIOS = 1        # um negócio já é negócio


def classificar_liquidez(out: pd.DataFrame) -> pd.DataFrame:
    """
    Camada calculada sobre os dados brutos — nunca por cima deles.

    Cada sinal vira uma coluna booleana independente, e só depois eles são
    resumidos num status. Assim você pode ignorar o resumo e usar os
    sinais crus quando quiser.

        tem_negocio            houve negócio efetivo no pregão
        tem_bid / tem_ask      há oferta de compra / venda
        tem_open_interest      há contratos em aberto
        tem_preco_referencia   a B3 publicou preço de referência ou ajuste
        tem_cotacao_executavel há preço com que se poderia operar

    SOBRE tem_cotacao_executavel: os arquivos públicos da B3 NÃO trazem
    livro de ofertas. Sem bid nem ask, não há como afirmar que existe
    preço executável, e o campo fica False em toda a base. Isso não quer
    dizer que a opção não tinha mercado — quer dizer que esta fonte não
    permite saber. A distinção é o ponto: preencher com True por causa do
    preço de referência seria inventar uma certeza que o dado não dá.

    STATUS, por prioridade decrescente:

        1 NEGOCIADA_NO_DIA   houve negócio
        2 COTADA             há bid e/ou ask, mesmo sem negócio
        3 POSICAO_ABERTA     há contratos em aberto, sem mercado observado
        4 SOMENTE_REFERENCIA só o preço teórico da B3
        5 SEM_INFORMACAO     nada relevante

    Posição em aberto NÃO é mercado negociável: são contratos abertos no
    passado, que podem estar parados há semanas. A versão anterior
    chamava isso de NEGOCIAVEL, o que induziria a erro na hora de estimar
    se dá para montar ou desmontar uma proteção.
    """
    n = lambda c: pd.to_numeric(out.get(c), errors="coerce")

    out["tem_negocio"] = (n("numero_negocios").fillna(0) >= MIN_NEGOCIOS) | \
                         (n("quantidade_contratos").fillna(0) > 0)
    out["tem_bid"] = n("melhor_compra_bid").notna()
    out["tem_ask"] = n("melhor_venda_ask").notna()
    out["tem_open_interest"] = n("contratos_abertos").fillna(0) > 0
    out["tem_preco_referencia"] = (n("preco_referencia").notna() |
                                   n("ajuste").notna())
    out["tem_cotacao_executavel"] = out["tem_bid"] | out["tem_ask"]

    out["status_liquidez"] = pd.Series(
        "SEM_INFORMACAO", index=out.index, dtype="object").mask(
        out["tem_preco_referencia"], "SOMENTE_REFERENCIA").mask(
        out["tem_open_interest"], "POSICAO_ABERTA").mask(
        out["tem_cotacao_executavel"], "COTADA").mask(
        out["tem_negocio"], "NEGOCIADA_NO_DIA")

    # Spread só existe com os dois lados; sem fonte, permanece ausente.
    bid, ask = n("melhor_compra_bid"), n("melhor_venda_ask")
    out["spread_bid_ask"] = ask - bid
    meio = (bid + ask) / 2
    out["spread_pct"] = (out["spread_bid_ask"] / meio) * 100

    # PRÊMIO EXECUTÁVEL: só do livro de ofertas. Nunca da referência.
    # Com a fonte atual fica sempre ausente, e é assim que tem que ser —
    # é a diferença entre "custaria isto" e "a B3 calculou isto".
    out["premio_executavel"] = meio.where(bid.notna() & ask.notna())
    out["origem_premio_executavel"] = pd.Series(
        pd.NA, index=out.index, dtype="object").mask(
        out["premio_executavel"].notna(), "MIDPOINT_BID_ASK")
    return out


def raiz_do_ticker(ticker: str, ativo: str) -> Optional[str]:
    """
    Raiz do ticker da opção, que é o ticker do futuro objeto.

    O padrão da B3 é ATIVO + mês + ano + C/P + strike×100:

        SJCX26 C 001100   ->  raiz SJCX26
        BGIF27 P 034050   ->  raiz BGIF27

    Devolve None se o formato não bater — melhor não associar do que
    associar errado.
    """
    if not ticker or not ativo:
        return None
    m = re.match(rf"^({re.escape(str(ativo))}[FGHJKMNQUVXZ]\d{{2}})[CP]\d+$",
                 str(ticker).strip().upper())
    return m.group(1) if m else None


def ligar_futuro(out: pd.DataFrame, futuros: pd.DataFrame) -> pd.DataFrame:
    """
    Associa cada opção ao seu futuro objeto.

    A ligação é pela RAIZ DO TICKER, não pela data de vencimento.

    A raiz designa o CONTRATO FUTURO OBJETO, e não o mês em que a opção
    vence. É o que torna a regra correta para as opções SERIAIS de SJC,
    em que a opção vence num mês e tem como objeto o futuro do mês
    seguinte:

        opção fevereiro  ->  futuro março     (H)
        opção abril      ->  futuro maio      (K)
        opção junho      ->  futuro julho     (N)
        opção outubro    ->  futuro novembro  (X)
        opção dezembro   ->  futuro janeiro   (F)

    Em 02/09/2026 as cinco séries de SJC listadas eram todas seriais:

        SJCX26  opção vence 23/10/2026  ->  futuro de NOVEMBRO (X26)
        SJCF27  opção vence 23/12/2026  ->  futuro de JANEIRO  (F27)
        SJCH27  opção vence 19/02/2027  ->  futuro de MARÇO    (H27)

    Casar por igualdade de data de vencimento é errado por construção, e
    NÃO deve ser reintroduzido: em BGI e CCM funcionava só porque as
    opções regulares vencem junto com o futuro objeto; nas 218 séries de
    SJC nenhuma casava, e o moneyness ficava vazio justamente no contrato
    em dólar.

    O campo oficial de ativo-objeto (UndrlygTckrSymb1) não serve aqui:
    vem preenchido só nas linhas de futuro, apontando para a taxa de
    dólar. Nas opções, vazio. Por isso a regra é a do ticker, que é
    pública e verificável.

    A raiz derivada é CONFERIDA contra a lista real de futuros do mesmo
    ativo. Se não existir, a opção fica sem futuro e sem moneyness, com
    aviso — nunca uma associação inventada.
    """
    idx = out.index
    if futuros is None or futuros.empty:
        out["ticker_futuro"] = pd.Series(pd.NA, index=idx, dtype="object")
        out["preco_futuro"] = pd.NA
        out["tipo_preco_futuro"] = pd.Series(pd.NA, index=idx, dtype="object")
        return out

    ref = futuros.drop_duplicates("ticker_futuro").set_index("ticker_futuro")
    raizes = [raiz_do_ticker(t, a)
              for t, a in zip(out["ticker_opcao"], out["mercado_codigo"])]
    validas = [r if (r is not None and r in ref.index) else None
               for r in raizes]

    sem = sum(1 for r in validas if r is None)
    if sem:
        exemplos = [t for t, v in zip(out["ticker_opcao"], validas)
                    if v is None][:3]
        _log(f"[b3] {sem} opção(ões) sem futuro correspondente "
             f"(ex.: {', '.join(map(str, exemplos))}) — ficam sem moneyness",
             "warning")

    def campo(col):
        return pd.Series([ref.loc[r, col] if r else None for r in validas],
                         index=idx)

    out["ticker_futuro"] = pd.Series(validas, index=idx, dtype="object")
    aj = pd.to_numeric(campo("ajuste"), errors="coerce")
    ul = pd.to_numeric(campo("ultimo_preco"), errors="coerce")
    rf = pd.to_numeric(campo("preco_referencia"), errors="coerce")
    # Preferência: ajuste > último > referência. A coluna ao lado diz qual
    # foi usado — comparar séries de origens diferentes sem saber disso
    # produz conclusão errada.
    out["preco_futuro"] = aj.fillna(ul).fillna(rf)
    out["tipo_preco_futuro"] = pd.Series(
        pd.NA, index=idx, dtype="object").mask(
        rf.notna(), "REFERENCIA").mask(
        ul.notna(), "ULTIMO").mask(aj.notna(), "AJUSTE")
    return out


def _montar_opcoes(d, opc, neg_i, pos_i, futuros) -> pd.DataFrame:
    """
    Uma linha por série de opção do pregão.

    Ordem das decisões:
      1. identidade vem do cadastro (nunca do ticker)
      2. cotação vem da negociação, ausente quando não houve
      3. futuro correspondente entra pelo par mercado + vencimento
      4. derivados são calculados por último, em colunas próprias
    """
    idx = opc.index
    out = pd.DataFrame(index=idx)

    # ── identidade: tudo de campo oficial ────────────────────────────────
    out["data"] = d.isoformat()
    out["mercado"] = opc["Asst"].map(MERCADOS)
    out["mercado_codigo"] = opc["Asst"]
    out["tipo_opcao"] = (opc["OptnTp"].str.strip().str.upper()
                         .map({"CALL": "CALL", "PUT": "PUT"}))
    out["ticker_opcao"] = opc["TckrSymb"]
    out["vencimento_codigo"] = opc["XprtnCd"]
    out["data_vencimento"] = opc["XprtnDt"]
    out["strike"] = _num(opc["ExrcPric"])
    out["estilo"] = opc.get("OptnStyle")
    out["moeda"] = opc.get("TradgCcy")
    out["multiplicador"] = _num(opc.get("CtrctMltplr", pd.Series(index=idx)))
    out["isin"] = opc.get("ISIN")

    # vencimento legível: DEZ/26, para você filtrar no Excel sem decorar codigo
    dv = pd.to_datetime(out["data_vencimento"], errors="coerce")
    MES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
           "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
    out["vencimento"] = dv.map(
        lambda x: f"{MES[x.month-1]}/{x.year % 100:02d}" if pd.notna(x) else None)

    # ── cotação: ausente continua ausente ────────────────────────────────
    linhas = [neg_i.loc[t] if t in neg_i.index else None
              for t in opc["TckrSymb"]]
    def campo(orig):
        return _num(pd.Series([r[orig] if r is not None else None
                               for r in linhas], index=idx))
    out["preco_abertura"] = pd.NA          # a fonte pública não publica
    out["preco_minimo"] = campo("MinPric")
    out["preco_maximo"] = campo("MaxPric")
    out["preco_medio"] = campo("TradAvrgPric")
    out["ultimo_preco"] = campo("LastPric")
    out["preco_referencia"] = campo("RefPric")
    out["ajuste"] = campo("AdjstdQt")
    out["variacao_pct"] = campo("OscnPctg") if "OscnPctg" in neg_i.columns else pd.NA

    # Sem fonte pública de livro de ofertas. As colunas existem para o dia
    # em que houver — jamais preenchidas por estimativa.
    out["melhor_compra_bid"] = pd.NA
    out["melhor_venda_ask"] = pd.NA
    out["spread_bid_ask"] = pd.NA
    out["spread_pct"] = pd.NA

    out["numero_negocios"] = campo("TradQty")
    out["quantidade_contratos"] = campo("FinInstrmQty")
    out["volume_financeiro"] = campo("NtlFinVol")
    out["contratos_abertos"] = (
        _num(pd.Series([pos_i.loc[t, "OpnIntrst"] if t in pos_i.index else None
                        for t in opc["TckrSymb"]], index=idx))
        if not pos_i.empty else pd.Series(pd.NA, index=idx))

    out = ligar_futuro(out, futuros)
    out = classificar_liquidez(out)
    return _derivados(out, d).reset_index(drop=True)


def recalcular_derivados(opcoes: pd.DataFrame, futuros: pd.DataFrame,
                         log=None) -> pd.DataFrame:
    """
    Recalcula as colunas DERIVADAS de um histórico já gravado.

    Serve para corrigir a base sem rebaixar nada da B3: os dados brutos
    vêm do Parquet e só as colunas calculadas são refeitas. As colunas
    originais — preços, volume, posição, cadastro — não são tocadas.

    Idempotente: rodar duas vezes produz exatamente o mesmo resultado.
    """
    global _log_fn
    if log:
        _log_fn = log
    if opcoes is None or opcoes.empty:
        return opcoes

    partes = []
    for dia, grupo in opcoes.groupby("data", sort=True):
        g = grupo.copy()
        f = (futuros[futuros["data"] == dia]
             if futuros is not None and not futuros.empty else pd.DataFrame())
        g = ligar_futuro(g, f)
        g = classificar_liquidez(g)
        g = _derivados(g, date.fromisoformat(str(dia)[:10]))
        partes.append(g)
    return pd.concat(partes, ignore_index=True)


def _derivados(out: pd.DataFrame, d: date) -> pd.DataFrame:
    """
    Campos calculados — sempre em coluna nova, nunca por cima do original.
    """
    dv = pd.to_datetime(out["data_vencimento"], errors="coerce")
    out["dias_ate_vencimento"] = (dv - pd.Timestamp(d)).dt.days

    pf = pd.to_numeric(out["preco_futuro"], errors="coerce")
    st = pd.to_numeric(out["strike"], errors="coerce")
    out["distancia_strike_futuro"] = st - pf
    out["moneyness_pct"] = ((st / pf) - 1) * 100

    # ── prêmio usado nos indicativos ─────────────────────────────────────
    # Ordem: último negócio > preço médio > referência. A origem vai gravada
    # em coluna própria: dois prêmios de origens diferentes não são
    # comparáveis, e a base tem que deixar isso explícito.
    ult = pd.to_numeric(out["ultimo_preco"], errors="coerce")
    med = pd.to_numeric(out["preco_medio"], errors="coerce")
    ref = pd.to_numeric(out["preco_referencia"], errors="coerce")
    out["premio_calculo"] = ult.fillna(med).fillna(ref)
    out["origem_premio_calculo"] = pd.Series(
        pd.NA, index=out.index, dtype="object").mask(
        ref.notna(), "REFERENCIA").mask(
        med.notna(), "PRECO_MEDIO").mask(ult.notna(), "ULTIMO_NEGOCIO")

    pr = out["premio_calculo"]
    ehput = out["tipo_opcao"] == "PUT"
    out["piso_put"] = (st - pr).where(ehput)
    out["teto_call"] = (st + pr).where(~ehput)

    # ── metadados de unidade e de vencimento ─────────────────────────────
    # Preço do futuro, strike e prêmio estão na MESMA unidade dentro de
    # cada mercado, então moneyness e piso/teto são coerentes sem
    # conversão. A coluna existe para o Radar não precisar deduzir isso.
    out["unidade_preco"] = out["mercado_codigo"].map(UNIDADE_PRECO)
    out["fonte_preco_objeto"] = out["mercado_codigo"].map(FONTE_PRECO_OBJETO)
    # Produto sem validação concluída continua sendo COLETADO e GRAVADO —
    # só não entra em estatística. Excluir da coleta criaria buraco no
    # histórico que não se recupera depois.
    out["status_validacao_produto"] = out["mercado_codigo"].map(
        STATUS_VALIDACAO_PRODUTO).fillna("PENDENTE")

    # Mês do FUTURO OBJETO, lido da letra da raiz do ticker. Distinto do
    # mês em que a opção vence — e é exatamente essa distinção que as
    # opções seriais tornam necessária.
    letra = out["ticker_futuro"].astype(str).str.extract(
        r"[A-Z]{3}([FGHJKMNQUVXZ])\d{2}$", expand=False)
    ano2 = out["ticker_futuro"].astype(str).str.extract(
        r"[A-Z]{3}[FGHJKMNQUVXZ](\d{2})$", expand=False)
    mes_fut = letra.map(MES_CODIGO)
    out["mes_futuro_objeto"] = [
        f"{int(m):02d}/20{a}" if pd.notna(m) and pd.notna(a) else None
        for m, a in zip(mes_fut, ano2)]

    # REGULAR: a opção vence no mesmo mês do contrato objeto.
    # SERIAL : vence antes, sobre o futuro de um mês posterior.
    #
    # A regra é aplicada POR PRODUTO, conforme CONVENCAO_VENCIMENTO. Num
    # produto declarado só-regular, uma divergência de mês não vira
    # "SERIAL" por conta própria: vira INDETERMINADO com aviso, porque
    # significaria que a B3 mudou a especificação e isso precisa ser
    # verificado, não deduzido.
    mes_opc = dv.dt.month
    ano_opc = dv.dt.year
    ano_fut = pd.to_numeric(ano2, errors="coerce") + 2000
    conhecido = mes_fut.notna() & mes_opc.notna()
    mesmo_mes = (mes_fut == mes_opc) & (ano_fut == ano_opc)
    convencao = out["mercado_codigo"].map(CONVENCAO_VENCIMENTO)

    tipo = pd.Series(pd.NA, index=out.index, dtype="object")
    # produto com as duas convenções: a comparação de meses decide
    tem_serial = convencao == "SERIAL_E_REGULAR"
    tipo = tipo.mask(tem_serial & conhecido & mesmo_mes, "REGULAR")
    tipo = tipo.mask(tem_serial & conhecido & ~mesmo_mes, "SERIAL")
    # produto declarado só-regular: confirma, e estranha se divergir
    so_regular = convencao == "REGULAR"
    tipo = tipo.mask(so_regular & conhecido & mesmo_mes, "REGULAR")
    divergente = so_regular & conhecido & ~mesmo_mes
    tipo = tipo.mask(divergente, "INDETERMINADO")
    # produto sem convenção declarada: não rotular por analogia
    tipo = tipo.mask(convencao.isna() & conhecido, "INDETERMINADO")
    out["tipo_vencimento_opcao"] = tipo

    if divergente.any():
        exemplos = out.loc[divergente, "ticker_opcao"].head(3).tolist()
        _log(f"[b3] {int(divergente.sum())} opção(ões) de produto declarado "
             f"REGULAR vencem em mês diferente do futuro objeto "
             f"(ex.: {', '.join(map(str, exemplos))}). Marcadas como "
             f"INDETERMINADO — confira a especificação do contrato.",
             "warning")

    out["fonte"] = "B3 arquivos.b3.com.br"
    out["versao_coletor"] = VERSAO_COLETOR

    # data_captura marca quando o dado foi BAIXADO da B3, não quando os
    # derivados foram recalculados. Reprocessar não pode carimbar por cima:
    # além de apagar a informação de quando aquele pregão entrou, faria a
    # rotina nunca parecer idempotente — cada passada mostraria a base
    # inteira como "alterada".
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "data_captura" in out.columns:
        out["data_captura"] = out["data_captura"].fillna(agora)
    else:
        out["data_captura"] = agora
    return out


# Ordem das colunas na base. Primeira linha do Excel = estes nomes, uma
# linha por registro, sem mescla e sem decoração — o Radar vai ler com
# SheetJS e qualquer enfeite atrapalha.
COLUNAS = [
    "data", "mercado", "mercado_codigo", "tipo_opcao", "ticker_opcao",
    "ticker_futuro", "vencimento", "vencimento_codigo", "data_vencimento",
    "tipo_vencimento_opcao", "mes_futuro_objeto",
    "strike", "estilo", "moeda", "multiplicador", "isin",
    "preco_abertura", "preco_minimo", "preco_maximo", "preco_medio",
    "ultimo_preco", "preco_referencia", "ajuste", "variacao_pct",
    "melhor_compra_bid", "melhor_venda_ask",
    "numero_negocios", "quantidade_contratos", "volume_financeiro",
    "contratos_abertos",
    "preco_futuro", "tipo_preco_futuro", "unidade_preco",
    "fonte_preco_objeto", "status_validacao_produto",
    "dias_ate_vencimento", "distancia_strike_futuro", "moneyness_pct",
    "premio_calculo", "origem_premio_calculo", "piso_put", "teto_call",
    "premio_executavel", "origem_premio_executavel",
    "tem_negocio", "tem_bid", "tem_ask", "tem_open_interest",
    "tem_preco_referencia", "tem_cotacao_executavel",
    "spread_bid_ask", "spread_pct", "status_liquidez",
    "fonte", "versao_coletor", "data_captura",
]

# Chave de unicidade. data + ticker já bastaria — o ticker da B3 codifica
# ativo, vencimento, tipo e strike, e não se repete no mesmo pregão. Os
# demais campos entram por segurança: se algum dia dois registros
# divergirem sob o mesmo ticker, a duplicata aparece em vez de um
# silenciosamente sobrescrever o outro.
CHAVE = ["data", "mercado_codigo", "tipo_opcao", "ticker_opcao"]

COLUNAS_FUTUROS = [
    "data", "mercado", "mercado_codigo", "ticker_futuro",
    "vencimento_codigo", "data_vencimento", "moeda",
    "ajuste", "ultimo_preco", "preco_referencia",
    "quantidade_contratos", "volume_financeiro", "contratos_abertos",
]
