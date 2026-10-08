# Economiza AI

Comparador de preços com IA focado em encontrar e destacar menores ofertas dentro do próprio aplicativo.

## Como funciona

1. O cliente informa o produto.
2. O servidor consulta fontes de preços estruturadas em paralelo:
   - Mercado Livre API (oficial, gratuita) + fallback HTML
   - KaBuM!
   - Magazine Luiza
   - Zoom (só descoberta de preço/loja — nunca como destino final)
3. Só entram ofertas com **URL unitária de produto validada** (nunca página de busca/categoria).
4. Resultados são deduplicados por link, ranqueados e cacheados (~18 min).
5. O botão **Ver oferta** abre direto a página do produto na loja.

**Importante:** o aplicativo não faz pesquisa web genérica em Google, Bing, DuckDuckGo ou Yahoo e não redireciona a pesquisa do usuário para esses buscadores.

## Render

O projeto inclui `Dockerfile`, `render.yaml`, `requirements.txt` e `start_servidor.sh`.

- Porta: definida pela variável `PORT` do Render.
- Health check: `/api/health`.
- Endpoint de comparação: `/api/search?q=...`.
- Endpoint do agente: `/api/agent?q=...`.
- `/api/web-search` permanece apenas como resposta 410 para impedir chamadas antigas; ele não executa pesquisa.

## GitHub

Suba o conteúdo da pasta `economiza_ai_github/` para o repositório.

## Variáveis opcionais

- `ALLOWED_ORIGIN`: origem permitida para CORS.
- `ADMIN_API_TOKEN`: protege endpoints administrativos de aprendizado/checkpoint.

## Versão

`2026-10-08-r8` — multi-fonte + só links de produto + cache em memória.
