#!/usr/bin/env python3
"""
Economiza AI — Comparador de Preços Inteligente
=========================================
Motor que considera:
  • Preço do produto
  • Frete (quando disponível)
  • Desconto / Cashback
  • Condições de pagamento (parcelas)

E atua como consultora de IA:
  Responde perguntas com base APENAS nos produtos realmente encontrados.
"""

import sys
import re
import urllib.parse
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown
from rich import box

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─────────────────────────────────────────────
#  MODELO DE DADOS
# ─────────────────────────────────────────────

@dataclass
class Oferta:
    name: str
    price: float
    price_text: str
    store: str
    link: str
    rating: str = "—"
    is_new: bool = True
    installments: str = ""          # "10x de R$ 529,00 sem juros"
    cashback: str = ""              # "6% de volta"
    shipping_text: str = "A calcular"
    shipping_cost: Optional[float] = None  # 0.0 = grátis, None = desconhecido
    discount_pct: Optional[float] = None

    @property
    def total_estimado(self) -> float:
        """Preço + frete (quando conhecido)."""
        if self.shipping_cost is not None:
            return self.price + self.shipping_cost
        return self.price

    @property
    def total_text(self) -> str:
        if self.shipping_cost is None:
            return f"{self.price_text} + frete a calcular"
        if self.shipping_cost == 0:
            return f"{self.price_text} + R$ 0 frete"
        return f"{self.price_text} + R$ {self.shipping_cost:.2f} frete".replace(".", ",")

    def resumo_completo(self) -> str:
        parts = [self.total_text]
        if self.installments:
            parts.append(self.installments)
        if self.cashback:
            parts.append(self.cashback)
        return " · ".join(parts)


# ─────────────────────────────────────────────
#  UTILITÁRIOS
# ─────────────────────────────────────────────

def parse_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    cleaned = re.sub(r"[R$\s]", "", price_str)
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_installments(text: str) -> str:
    """Extrai '10x de R$ 529,00' ou '6x de R$ 740,74 sem juros'."""
    m = re.search(r"(\d+\s*x\s*de\s*R\$\s*[\d.,]+)(?:\s*sem\s*juros)?", text, re.I)
    if m:
        result = m.group(0).strip()
        if "sem juros" in text.lower() and "sem juros" not in result.lower():
            result += " sem juros"
        return result
    return ""


def extract_cashback(text: str) -> str:
    """Extrai '6% de volta' ou 'Super Cashback'."""
    m = re.search(r"(\d+%\s*de\s*volta)", text, re.I)
    if m:
        return m.group(1)
    if "super cashback" in text.lower() or "cashback" in text.lower():
        return "Cashback disponível"
    return ""


def is_new_product(name: str, text: str) -> bool:
    lower = (name + " " + text).lower()
    return not any(w in lower for w in ["usado", "seminovo", "recondicionado", "open box"])


# ─────────────────────────────────────────────
#  MOTOR DE BUSCA (Zoom)
# ─────────────────────────────────────────────

def search_zoom(product: str, max_results: int = 12) -> List[Oferta]:
    query = urllib.parse.quote_plus(product)
    url = f"https://www.zoom.com.br/search?q={query}"

    console.print(f"\n[cyan]🔍 Buscando:[/cyan] [bold]{product}[/bold]")
    console.print(f"[dim]{url}[/dim]\n")

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[red]Erro ao acessar o Zoom: {e}[/red]")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select('article[class*="OrqProductCard"]')

    results: List[Oferta] = []
    keywords = [k.lower() for k in product.split() if len(k) > 2]

    for card in cards[: max_results * 3]:
        full_text = card.get_text(separator=" | ", strip=True)

        # Nome
        name_el = card.select_one('[class*="Name_OrqProductCard_Name"]')
        name = name_el.get_text(strip=True) if name_el else "Nome não encontrado"

        # Preço principal (ignora parcelas)
        price_text = None
        for s in card.find_all(string=re.compile(r"^R\$\s*[\d.,]+$")):
            parent_text = s.parent.get_text() if s.parent else ""
            if "x de" in parent_text.lower():
                continue
            price_text = s.strip()
            break
        if not price_text:
            matches = re.findall(r"R\$\s*[\d.,]+", full_text)
            for m in matches:
                if "x de" not in full_text.lower().split(m)[0][-25:]:
                    price_text = m
                    break

        price_value = parse_price(price_text) if price_text else None
        if price_value is None:
            continue

        # Loja
        store = "—"
        via = card.find(string=re.compile(r"Via\s+.+"))
        if via:
            store = re.sub(r"^Via\s+", "", via.strip()).strip()
            store = re.split(r"\s{2,}|\|", store)[0].strip()

        # Link
        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        link = ("https://www.zoom.com.br" + href) if href.startswith("/") else href

        # Avaliação
        rating = "—"
        rating_match = re.search(r"(\d(?:\.\d)?)\s*\(\d+\)", full_text)
        if rating_match:
            rating = rating_match.group(1)

        # Parcelas e cashback
        installments = extract_installments(full_text)
        cashback = extract_cashback(full_text)

        # Frete (na listagem do Zoom quase nunca aparece valor exato)
        shipping_text = "A calcular"
        shipping_cost = None
        if "frete grátis" in full_text.lower() or "frete gratis" in full_text.lower():
            shipping_text = "Grátis"
            shipping_cost = 0.0

        # Novo ou usado
        is_new = is_new_product(name, full_text)

        # Relevância mais rigorosa
        name_lower = name.lower()
        relevance = sum(1 for k in keywords if k in name_lower)
        # Bônus se contém a frase principal (ex: "iphone 16")
        main_phrase = " ".join(keywords[:2]) if len(keywords) >= 2 else (keywords[0] if keywords else "")
        if main_phrase and main_phrase in name_lower:
            relevance += 3
        if is_new:
            relevance += 1
        # Penaliza produtos que claramente não são o buscado (ex: C85 quando busca iPhone)
        if keywords and relevance < len(keywords):
            # Se tem menos da metade das palavras-chave, descarta depois
            pass

        oferta = Oferta(
            name=name,
            price=price_value,
            price_text=price_text or "—",
            store=store,
            link=link,
            rating=rating,
            is_new=is_new,
            installments=installments,
            cashback=cashback,
            shipping_text=shipping_text,
            shipping_cost=shipping_cost,
        )
        oferta._relevance = relevance  # type: ignore
        results.append(oferta)

    # Filtra de forma mais inteligente
    if keywords:
        # Exige que pelo menos as 2 primeiras palavras-chave importantes estejam presentes
        # (ex: "iphone" e "16" para "iPhone 16 128GB")
        important = keywords[:2] if len(keywords) >= 2 else keywords
        filtered = []
        for r in results:
            name_l = r.name.lower()
            if all(k in name_l for k in important):
                filtered.append(r)
        # Se filtrou demais, relaxa para maioria das keywords
        if len(filtered) >= 3:
            results = filtered
        else:
            min_rel = max(2, (len(keywords) + 1) // 2)
            results = [r for r in results if getattr(r, "_relevance", 0) >= min_rel]

    # Ordena: relevância desc → total estimado asc
    results.sort(key=lambda o: (-getattr(o, "_relevance", 0), o.total_estimado))
    return results[:max_results]


# ─────────────────────────────────────────────
#  MELHOR OFERTA (lógica inteligente)
# ─────────────────────────────────────────────

def escolher_melhor_oferta(ofertas: List[Oferta]) -> Optional[Oferta]:
    """
    Escolhe a melhor oferta considerando:
    1. Preço + frete (quando conhecido)
    2. Preferência por produto novo
    3. Preferência por frete grátis / conhecido
    4. Existência de cashback e parcelas sem juros
    """
    if not ofertas:
        return None

    def score(o: Oferta) -> Tuple:
        # Quanto menor o score, melhor
        total = o.total_estimado
        novo = 0 if o.is_new else 50          # penaliza usado
        frete_conhecido = 0 if o.shipping_cost is not None else 5
        frete_gratis = -3 if o.shipping_cost == 0 else 0
        tem_cashback = -2 if o.cashback else 0
        sem_juros = -2 if "sem juros" in o.installments.lower() else 0
        return (total + novo + frete_conhecido + frete_gratis + tem_cashback + sem_juros,)

    return min(ofertas, key=score)


# ─────────────────────────────────────────────
#  IA CONSULTORA (baseada apenas nos resultados reais)
# ─────────────────────────────────────────────

def ia_consultora(pergunta: str, ofertas: List[Oferta], melhor: Optional[Oferta]) -> str:
    """
    Consultora independente e educada.
    Responde com base APENAS nos produtos realmente encontrados.
    Não inventa preços/produtos. Recusa ofensas com educação.
    """
    p = (pergunta or "").lower().strip()
    if not p:
        return "Pode me perguntar qualquer coisa sobre as ofertas que encontrei 🙂"

    # Bloqueio educado de ofensas
    ofensas = [
        "idiota", "burro", "estupido", "estúpido", "lixo", "merda", "porra",
        "caralho", "puta", "viado", "fdp", "filho da puta", "otario", "otário",
        "inútil", "inutil", "vai se foder", "vsf", "cuzão", "cuzao",
    ]
    if any(o in p for o in ofensas):
        return (
            "Prefiro manter a conversa respeitosa 😊 "
            "Posso te ajudar a escolher a melhor oferta. "
            "O que você gostaria de saber sobre os produtos?"
        )

    if not ofertas:
        return "Ainda não tenho resultados de busca. Faça uma pesquisa primeiro que eu te ajudo com base nas ofertas reais."

    def total_txt(o: Oferta) -> str:
        return o.total_text

    def lista(arr, maxn=6) -> str:
        lines = []
        for o in arr[:maxn]:
            extra = " (usado)" if not o.is_new else ""
            lines.append(f"• **{o.name}** — {o.price_text} na {o.store}{extra}")
        return "\n".join(lines)

    cores = [
        "preto", "preta", "branco", "branca", "azul", "verde", "vermelho", "vermelha",
        "rosa", "dourado", "prata", "cinza", "roxo", "amarelo", "titânio", "titanio",
        "natural", "meia-noite", "estelar", "ultramarino", "coral", "lavanda",
    ]
    armazenamento = ["64gb", "128gb", "256gb", "512gb", "1tb", "64 gb", "128 gb", "256 gb", "512 gb"]

    # Saudação
    if any(p.startswith(s) for s in ["oi", "olá", "ola", "hey", "eae", "bom dia", "boa tarde", "boa noite"]):
        return (
            f"Olá! 😊 Estou aqui para te ajudar a escolher entre as **{len(ofertas)} ofertas** "
            "que encontrei. Pode perguntar sobre preço, frete, cor, armazenamento, parcelas, "
            "ou pedir minha recomendação!"
        )

    # Agradecimento
    if any(w in p for w in ["obrigad", "valeu", "thanks", "muito bom", "perfeito", "show"]):
        return "Por nada! Se precisar de mais alguma análise das ofertas, é só chamar 😊"

    # Recomendação
    if any(w in p for w in [
        "compraria", "recomenda", "recomendação", "melhor opção", "qual escolher",
        "vale a pena", "o que você acha", "me indica", "me ajuda a escolher", "qual pegar",
    ]):
        if not melhor:
            return "Não consegui determinar uma melhor oferta com os dados atuais."
        linhas = [
            "Com base nas ofertas reais que encontrei, eu escolheria esta:",
            "",
            f"🏆 **{melhor.name}**",
            f"• Loja: **{melhor.store}**",
            f"• {total_txt(melhor)}",
        ]
        if melhor.installments:
            linhas.append(f"• Parcelas: **{melhor.installments}**")
        if melhor.cashback:
            linhas.append(f"• Cashback: **{melhor.cashback}**")
        linhas.append(f"• Condição: **{'Novo' if melhor.is_new else 'Usado'}**")
        if melhor.rating and melhor.rating != "—":
            linhas.append(f"• Avaliação: **{melhor.rating}**")
        linhas.append("")
        linhas.append("**Por quê?**")
        motivos = []
        if melhor.is_new:
            motivos.append("é produto novo")
        else:
            motivos.append("tem o melhor custo-benefício mesmo sendo usado")
        if melhor.shipping_cost == 0:
            motivos.append("tem frete grátis")
        if melhor.cashback:
            motivos.append(f"oferece {melhor.cashback}")
        if "sem juros" in melhor.installments.lower():
            motivos.append("permite parcelar sem juros")
        if not motivos:
            motivos.append("apresenta o menor custo total entre as opções analisadas")
        linhas.append("Porque " + ", ".join(motivos) + ".")
        linhas.append("")
        linhas.append(f"[Ver esta oferta]({melhor.link})")
        return "\n".join(linhas)

    # Mais barato
    if any(w in p for w in ["mais barato", "menor preço", "mais baixo", "mais em conta"]):
        barato = min(ofertas, key=lambda o: o.price)
        return (
            f"O **menor preço** entre as ofertas encontradas é:\n\n"
            f"**{barato.name}**\n"
            f"• {total_txt(barato)}\n"
            f"• Loja: {barato.store}\n"
            f"• {'Novo' if barato.is_new else 'Usado'}\n\n"
            f"[Ver oferta]({barato.link})"
        )

    # Cor
    if any(w in p for w in ["cor", "cores", "colorido", "que cor", "qual cor"]):
        encontrados = []
        for o in ofertas:
            nome = o.name.lower()
            for c in cores:
                if c in nome:
                    encontrados.append((c, o))
        if encontrados:
            por_cor = {}
            for c, o in encontrados:
                por_cor.setdefault(c, []).append(o)
            linhas = ["Nas ofertas que encontrei, identifiquei estas cores nos nomes dos produtos:", ""]
            for c, items in por_cor.items():
                linhas.append(f"**{c.capitalize()}**")
                for o in items[:3]:
                    linhas.append(f"• {o.name} — {o.price_text} ({o.store})")
                linhas.append("")
            linhas.append("Se a cor não aparecer no nome, geralmente você consegue escolher na página da loja.")
            return "\n".join(linhas)
        return (
            "Nas ofertas listadas não consegui identificar a cor pelo nome do produto. "
            "Normalmente a cor é selecionada dentro da página da loja. "
            "Quer que eu te indique a melhor oferta para você conferir lá?"
        )

    # Armazenamento
    if any(w in p for w in ["armazenamento", "memória", "memoria", "quantos gb", "capacidade", "128gb", "256gb", "512gb"]):
        encontrados = []
        for o in ofertas:
            nome = o.name.lower().replace(" ", "")
            for a in armazenamento:
                if a.replace(" ", "") in nome:
                    encontrados.append((a.upper(), o))
        if encontrados:
            linhas = ["Capacidades que identifiquei nas ofertas:", ""]
            for cap, o in encontrados[:8]:
                linhas.append(f"• **{cap}** — {o.name} ({o.price_text})")
            return "\n".join(linhas)
        return "Não consegui extrair a capacidade de armazenamento só pelo nome das ofertas. Essa informação costuma estar clara na página do produto."

    # Frete
    if any(w in p for w in ["frete", "entrega", "envio"]):
        gratis = [o for o in ofertas if o.shipping_cost == 0]
        if gratis:
            return (
                f"Encontrei **{len(gratis)}** oferta(s) com **frete grátis**:\n\n"
                + lista(gratis)
                + "\n\nNas demais o frete é calculado no carrinho da loja."
            )
        return (
            "Nenhuma das ofertas mostrou frete grátis de forma explícita na listagem. "
            "O valor do frete normalmente aparece quando você informa o CEP na página da loja."
        )

    # Parcelamento
    if any(w in p for w in ["parcela", "parcelar", "parcelamento", "x de", "sem juros", "à vista", "pix", "cartão"]):
        com = [o for o in ofertas if o.installments]
        if com:
            linhas = ["Condições de pagamento que encontrei:", ""]
            for o in com[:7]:
                linhas.append(f"• **{o.name}**")
                linhas.append(f"  {o.installments} — {o.store}")
                linhas.append("")
            return "\n".join(linhas)
        return "As ofertas não trouxeram detalhes de parcelamento na listagem. Essa informação costuma aparecer na página do produto."

    # Usado / Novo
    if any(w in p for w in ["usado", "seminovo", "recondicionado"]):
        usados = [o for o in ofertas if not o.is_new]
        if usados:
            return "Opções **usadas/seminovas** encontradas:\n\n" + lista(usados)
        return "Não encontrei opções usadas nesta busca — todas as ofertas listadas parecem ser de produtos novos."

    if "novo" in p or "lacrado" in p:
        novos = [o for o in ofertas if o.is_new]
        if novos:
            return "Opções **novas** encontradas:\n\n" + lista(novos)
        return "Não encontrei opções novas nesta busca."

    # Lojas
    if any(w in p for w in ["loja", "onde comprar", "qual site", "magazine", "amazon", "casas bahia"]):
        lojas = {}
        for o in ofertas:
            lojas.setdefault(o.store, []).append(o)
        linhas = ["Lojas que aparecem nos resultados:", ""]
        for loja, items in lojas.items():
            mais_barato = min(items, key=lambda x: x.price)
            linhas.append(
                f"• **{loja}** — a partir de {mais_barato.price_text} "
                f"({len(items)} oferta{'s' if len(items) > 1 else ''})"
            )
        return "\n".join(linhas)

    # Cashback
    if any(w in p for w in ["cashback", "desconto", "promoção", "promocao"]):
        com_cash = [o for o in ofertas if o.cashback]
        if com_cash:
            linhas = ["Ofertas com cashback/desconto identificado:", ""]
            for o in com_cash:
                linhas.append(f"• **{o.name}** — {o.cashback} ({o.store})")
            return "\n".join(linhas)
        return "Não identifiquei cashback explícito nas ofertas listadas. Vale conferir cupons na página da loja."

    # Avaliação
    if any(w in p for w in ["avaliação", "avaliacao", "nota", "estrela", "review"]):
        com_nota = [o for o in ofertas if o.rating and o.rating != "—"]
        if com_nota:
            ordenado = sorted(com_nota, key=lambda o: float(o.rating), reverse=True)
            linhas = ["Avaliações que encontrei (maior nota primeiro):", ""]
            for o in ordenado[:6]:
                linhas.append(f"• ★ **{o.rating}** — {o.name} ({o.store})")
            return "\n".join(linhas)
        return "Poucas ofertas trouxeram nota visível na listagem. As avaliações detalhadas ficam na página do produto."

    # Quantas
    if any(w in p for w in ["quantas", "quantos", "lista", "todas", "mostrar tudo"]):
        return (
            f"Encontrei **{len(ofertas)} ofertas** nesta busca. "
            "A melhor está destacada acima. Quer que eu filtre por algum critério "
            "(preço, frete grátis, só novos…)?"
        )

    # Ajuda
    if any(w in p for w in ["o que você", "como funciona", "me ajuda", "help", "ajuda", "o que posso perguntar"]):
        return (
            "Posso te ajudar com base **somente nas ofertas reais** que encontrei. Exemplos:\n\n"
            "• Qual dessas você compraria?\n"
            "• Qual o mais barato?\n"
            "• Tem frete grátis?\n"
            "• Quais cores aparecem?\n"
            "• Como está o parcelamento?\n"
            "• Tem opção usada?\n"
            "• Qual loja está mais barata?\n"
            "• Tem cashback?\n\n"
            "Pergunte do seu jeito — eu entendo linguagem natural 🙂"
        )

    # Menção a produto específico
    for o in ofertas:
        palavras = [w for w in o.name.lower().split() if len(w) > 3]
        if any(w in p for w in palavras):
            linhas = [
                f"Sobre **{o.name}**:",
                "",
                f"• Preço: {total_txt(o)}",
                f"• Loja: {o.store}",
                f"• Condição: {'Novo' if o.is_new else 'Usado'}",
            ]
            if o.installments:
                linhas.append(f"• Parcelas: {o.installments}")
            if o.cashback:
                linhas.append(f"• Cashback: {o.cashback}")
            if o.rating and o.rating != "—":
                linhas.append(f"• Nota: {o.rating}")
            linhas.append("")
            linhas.append(f"[Ver esta oferta]({o.link})")
            return "\n".join(linhas)

    # Fallback educado
    return (
        f"Entendi sua pergunta. Com base nas **{len(ofertas)} ofertas** que tenho aqui, "
        "posso detalhar preço, frete, parcelas, cores (quando aparecem no nome), lojas "
        "e te dar uma recomendação.\n\n"
        "Tente algo como:\n"
        "• “Qual dessas você compraria?”\n"
        "• “Tem na cor preta?”\n"
        "• “Qual tem frete grátis?”\n"
        "• “Me mostra as opções usadas”\n\n"
        "Estou aqui para ajudar de forma clara e objetiva 🙂"
    )




# ─────────────────────────────────────────────
#  EXIBIÇÃO
# ─────────────────────────────────────────────

def display_results(product: str, ofertas: List[Oferta], melhor: Optional[Oferta]) -> None:
    if not ofertas:
        console.print("[yellow]Nenhum resultado encontrado. Tente outro termo.[/yellow]")
        return

    table = Table(
        title=f"💰 Economiza AI — {product}",
        box=box.ROUNDED,
        show_lines=True,
        title_style="bold green",
    )
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Produto", style="cyan", max_width=38)
    table.add_column("Preço + Frete", style="bold green", justify="right", max_width=22)
    table.add_column("Parcelas / Cashback", max_width=28)
    table.add_column("Loja", style="yellow")
    table.add_column("Cond.", justify="center")

    for i, o in enumerate(ofertas, 1):
        extra = []
        if o.installments:
            extra.append(o.installments)
        if o.cashback:
            extra.append(o.cashback)
        extra_text = " · ".join(extra) if extra else "—"

        is_best = melhor and o.name == melhor.name and o.price == melhor.price
        marker = "🏆 " if is_best else ""

        table.add_row(
            str(i),
            marker + o.name[:36],
            o.total_text,
            extra_text[:28],
            o.store[:16],
            "Novo" if o.is_new else "Usado",
        )

    console.print(table)

    if melhor:
        console.print(
            Panel(
                f"[bold green]🏆 MELHOR OFERTA[/bold green]\n\n"
                f"[cyan]{melhor.name}[/cyan]\n"
                f"[bold]{melhor.total_text}[/bold]\n"
                f"Loja: [yellow]{melhor.store}[/yellow]\n"
                + (f"Parcelas: {melhor.installments}\n" if melhor.installments else "")
                + (f"Cashback: {melhor.cashback}\n" if melhor.cashback else "")
                + f"Condição: {'Novo' if melhor.is_new else 'Usado'} · Nota: {melhor.rating}\n\n"
                f"[dim]{melhor.link}[/dim]",
                title="Recomendação do Economiza AI",
                border_style="green",
            )
        )


def main():
    console.print(
        Panel(
            "[bold green]Economiza AI[/bold green] — Comparador Inteligente\n\n"
            "Analisa [bold]preço + frete + desconto + condições de pagamento[/bold]\n"
            "e atua como consultora de IA (baseada só nos resultados reais).\n\n"
            "[dim]Fonte: Zoom.com.br · Uso pessoal/educacional[/dim]",
            title="🤖 Economiza AI",
            border_style="green",
        )
    )

    if len(sys.argv) > 1:
        product = " ".join(sys.argv[1:])
    else:
        product = Prompt.ask("\n[bold]Digite o nome do produto[/bold]")

    if not product.strip():
        console.print("[red]Nenhum produto informado.[/red]")
        sys.exit(1)

    ofertas = search_zoom(product.strip())
    melhor = escolher_melhor_oferta(ofertas)
    display_results(product.strip(), ofertas, melhor)

    # ── Modo consultora ──
    if ofertas:
        console.print("\n[bold cyan]💬 Consultora Economiza AI[/bold cyan]")
        console.print("[dim]Pergunte algo (ex: \"Qual dessas você compraria?\") ou digite 'sair'[/dim]\n")

        while True:
            try:
                pergunta = Prompt.ask("[bold]Você[/bold]")
            except (KeyboardInterrupt, EOFError):
                break
            if pergunta.lower().strip() in ("sair", "exit", "quit", "q"):
                break
            resposta = ia_consultora(pergunta, ofertas, melhor)
            console.print(Panel(Markdown(resposta), title="🤖 Economiza AI", border_style="cyan"))
            console.print()


if __name__ == "__main__":
    main()
