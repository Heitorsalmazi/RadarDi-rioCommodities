# Radar Diário de Mercado — BMS Agrobrasil

Painel de acompanhamento de preços e decisão de hedge para pecuária de corte.
Uso interno.

Publicado em: https://heitorsalmazi.github.io/RadarDi-rioCommodities/

---

## Estrutura do repositório

```
index.html                             a aplicação inteira, em um arquivo só
dados.xlsx                             base de cotações (atualizada por workflow)
data/logistica_reposicao.json          matriz logística e coeficientes ANTT
data/municipios_brasil.json            5.571 municípios com código IBGE
scripts/update_logistica_reposicao.py  mantém as coordenadas de origem
scripts/update_municipios.py           regenera a base municipal
scripts/update_antt.py                 lê os coeficientes vigentes da ANTT
.github/workflows/sync-dados.yml       dados.xlsx, a cada 2 horas
.github/workflows/update-logistica.yml base municipal + matriz, mensal
.github/workflows/update-antt.yml      coeficientes ANTT, mensal
```

## Como o site é publicado

GitHub Pages está configurado como **Deploy from a branch → `main` → `/` (root)**.

Qualquer commit em `main` dispara automaticamente o workflow
`pages build and deployment`, que leva de 30 a 90 segundos. Não existe passo
manual de publicação.

**`sync-dados.yml` não publica o site.** Ele só atualiza o `dados.xlsx` e faz
commit em `main`; é o commit que dispara o Pages, não o workflow em si. A
distinção importa: se o Pages parar de atualizar, o problema está no
deployment, não no sync — e vice-versa.

## Como testar localmente

O módulo de rotas precisa de HTTP. Abrir o `index.html` com duplo clique usa o
protocolo `file://`, e aí o navegador bloqueia `fetch` para os arquivos ao
lado. Na raiz do projeto:

```bash
python -m http.server 8000
```

E abrir:

```
http://localhost:8000/
```

Esse é o método preferencial para testar o Preço Colocado da Reposição.

Abrir por `file://` continua servindo para consultar o Radar — as bases vão
embutidas no HTML e o painel funciona —, mas não é ambiente completo: as
distâncias dependem de chamada externa ao OSRM, que pode ser bloqueada a
partir de uma página local.

## Bases externas e cópia embutida

Cada base tem duas vias, nesta ordem de prioridade:

```
    fetch do arquivo em data/          ← preferido: é a versão fresca
              │
         falhou?
              ↓
    bloco embutido no index.html       ← rede de segurança
```

O embutido **nunca sobrescreve** um arquivo externo que carregou bem: a
promessa só cai no fallback quando o `fetch` rejeita ou devolve status de erro.
Qual via foi usada fica registrado em `_origem` (`"repositório"` ou
`"embutida"`) e é anunciado no console.

Os blocos embutidos são `<script type="application/json">` — o navegador não os
executa, apenas os guarda para leitura. Existe exatamente um de cada.

Quando `data/` for atualizado por workflow, o site passa a usar a versão nova
sem precisar reembutir nada no HTML. Reembutir é útil só para quem usa o
arquivo solto, fora de um servidor.

## Caminhos

Todos os recursos usam caminho **relativo** (`./data/...`), nunca absoluto
(`/data/...`). Este é um Project Page: a raiz do domínio é
`heitorsalmazi.github.io/`, não `heitorsalmazi.github.io/RadarDi-rioCommodities/`.
Um caminho começando com `/` procuraria na raiz errada.

## Estado conhecido

Os coeficientes `ccd` e `cc` da ANTT estão `null` em
`data/logistica_reposicao.json` até o `update-antt.yml` rodar pela primeira
vez. Enquanto isso, o módulo de Preço Colocado mostra INDISPONÍVEL em vez de
um número inventado.

Pedágio não é automatizado. Rotas sem pedágio recebem status PARCIAL, e o card
de recomendação diz "melhor estimativa parcial" em vez de "melhor compra
colocada".
