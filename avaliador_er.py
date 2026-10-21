#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avaliador automático de Diagramas ER em PlantUML (cenários em português).

Implementa a ferramenta descrita na proposta de TCC: lê o código PlantUML gerado
por um LLM, verifica se ele compila no PlantUML, converte em estruturas em memória
(entidades, atributos, relacionamentos com cardinalidades), compara com o modelo de
referência (gabarito, também em PlantUML) e atribui uma pontuação percentual.
Também classifica as falhas (omissões, erros de cardinalidade, erros de tipo e
elementos extras/possíveis alucinações) para a análise comparativa entre LLMs e
níveis de detalhamento do prompt.

Baseado nos Algoritmos 1 e 2 de Ramachandran et al. (2025), estendido com
relacionamentos e cardinalidades. Não usa IA para similaridade: os nomes são
comparados por normalização de texto em português + similaridade de strings.

USO
    # um diagrama
    python avaliador_er.py gabarito.puml aluno.puml

    # experimento completo: pasta/<LLM>/<nivel>/*.puml  (várias execuções por combinação)
    python avaliador_er.py gabarito.puml --lote resultados/ --saida experimento

    # opções
    --limiar 0.85            similaridade mínima entre nomes (padrão 0.85)
    --plantuml "CMD"         comando do PlantUML (ex.: "java -jar plantuml.jar");
                             se omitido, procura plantuml no PATH, a variável
                             PLANTUML_CMD, ou plantuml.jar (variável PLANTUML_JAR)
    --sem-compilar           não verifica a compilação
    --pontuar-sem-compilar   pontua mesmo os diagramas que não compilam
    --json                   saída detalhada em JSON (modo de um diagrama)

    O resultado de cada diagrama avaliado é salvo em
    Resultado_do_<nome do arquivo avaliado>.txt, na mesma pasta do arquivo avaliado
    (ex.: resultados/ChatGPT/detalhado/exec1.puml -> Resultado_do_exec1.txt).

PONTUAÇÃO (0 a 100%)
    Cada entidade e cada relacionamento do gabarito é uma "unidade" com peso
    (PESO_ENTIDADE / PESO_RELACIONAMENTO, padrão 1 e 1). A nota é a soma dos pontos
    obtidos dividida pela soma dos pesos, em %.

    Entidade (Algoritmo 2): só pontua se houver entidade correspondente no diagrama
    avaliado. Seus pontos se dividem em (nº de atributos + 1) componentes iguais:
      - 1 componente: tipo da entidade (forte/fraca) igual ao do gabarito;
      - 1 componente por atributo do gabarito encontrado no diagrama avaliado, valendo
        a fração de acertos entre: identificador, multivalorado e composto. Um atributo
        "simples" acerta quando nenhuma das três características foi marcada.
    Relacionamento: só pontua se as duas entidades tiverem correspondência e existir
    relacionamento entre elas. Metade dos pontos é pela identificação do
    relacionamento; a outra metade pela cardinalidade (metade para cada lado).

FORMATO PLANTUML ACEITO
    entity Cliente {                       (ou: entity "Item do Pedido" as ItemPedido)
      * id_cliente : int <<PK>>            identificador: <<PK>>
      --
      telefone : varchar <<multivalorado>> multivalorado: <<multivalorado>> ou <<multi>>
      endereco : varchar
    }
    entity "Item do Pedido" as ItemPedido <<weak>> {    entidade fraca: <<weak>> ou <<fraca>>
      ...
    }
    note right of Cliente                  atributo composto (também vale "note left of")
      endereco: rua, numero, cidade        formato:  atributo_composto: sub1, sub2, ...
    end note
    Cliente ||--o{ Pedido : realiza        relacionamento (notação pé-de-galinha)
    Pedido  ||..|{ ItemPedido : contem     ".." = relacionamento fraco
    <<FK>> também é reconhecido no parser, mas não entra na pontuação (a proposta
    avalia atributos simples, identificadores, multivalorados e compostos).
"""

import argparse
import csv
import difflib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

# ==============================================================================
# 0. CONFIGURAÇÃO DA PONTUAÇÃO
# ==============================================================================

PESO_ENTIDADE = 1.0
PESO_RELACIONAMENTO = 1.0

# Características de atributo avaliadas (nome -> como ler do objeto Atributo).
# Para incluir chave estrangeira, acrescente: "fk": lambda a: a.eh_chave_estrangeira
RESTRICOES_ATRIBUTO: Dict[str, Callable] = {
    "identificador": lambda a: a.eh_chave_primaria,
    "multivalorado": lambda a: a.eh_multivalorado,
    "composto": lambda a: a.eh_composto,
}

# ==============================================================================
# 1. NORMALIZAÇÃO E COMPARAÇÃO DE TEXTO EM PORTUGUÊS
# ==============================================================================


def normalizar_texto(texto: str) -> str:
    """
    Remove acentos, cedilhas e caracteres especiais e converte para minúsculas.
    Exemplo: "Endereço_Cliente" -> "endereco cliente"
    """
    if not texto:
        return ""
    texto = texto.replace("_", " ")
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = re.sub(r"[^a-zA-Z0-9\s]", "", sem_acento)
    return re.sub(r"\s+", " ", limpo).lower().strip()


def similaridade_pt(str1: str, str2: str) -> float:
    """
    Similaridade entre 0.0 e 1.0 entre dois nomes, após normalização.
    Considera também a ordem trocada das palavras ("id_cliente" x "cliente_id").
    """
    n1, n2 = normalizar_texto(str1), normalizar_texto(str2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    direta = difflib.SequenceMatcher(None, n1, n2).ratio()
    o1, o2 = " ".join(sorted(n1.split())), " ".join(sorted(n2.split()))
    ordenada = difflib.SequenceMatcher(None, o1, o2).ratio()
    return max(direta, ordenada)


def comparar_strings_pt(str1: str, str2: str, limiar: float = 0.85) -> Tuple[bool, float]:
    """True se os nomes forem equivalentes (similaridade >= limiar)."""
    sim = similaridade_pt(str1, str2)
    return sim >= limiar, sim


# ==============================================================================
# 2. ESTRUTURA DE DADOS (OBJETOS EM MEMÓRIA)
# ==============================================================================


@dataclass
class Atributo:
    nome: str
    eh_chave_primaria: bool = False
    eh_chave_estrangeira: bool = False
    eh_multivalorado: bool = False
    eh_composto: bool = False
    subatributos: List[str] = field(default_factory=list)


@dataclass
class Entidade:
    nome: str
    atributos: List[Atributo] = field(default_factory=list)
    eh_fraca: bool = False


@dataclass
class Relacionamento:
    nome: str
    entidade1: str
    entidade2: str
    cardinalidade: str  # conector bruto, ex.: "||--o{"
    eh_fraco: bool = False


@dataclass
class DiagramaER:
    entidades: List[Entidade] = field(default_factory=list)
    relacionamentos: List[Relacionamento] = field(default_factory=list)


# ==============================================================================
# 3. PARSER DE PLANTUML (ALGORITMO 1)
# ==============================================================================


class ErroPlantUML(ValueError):
    """Erro de sintaxe ou de referência no arquivo PlantUML."""


EST_PK = {"pk", "chave", "chave primaria", "primary key", "primarykey"}
EST_FK = {"fk", "chave estrangeira", "foreign key", "foreignkey"}
EST_MULTI = {"multi", "multivalorado", "multivalor", "multivalued"}
EST_COMPOSTO = {"composto", "composite"}
EST_FRACA = {"weak", "fraca", "entidade fraca"}

RE_ESTEREOTIPO = re.compile(r"<<([^>]*)>>")
RE_ENTIDADE = re.compile(r'^entity\s+(?:"([^"]+)"|(\S+))(?:\s+as\s+(\S+))?', re.IGNORECASE)
RE_NOTA = re.compile(
    r'^note\s+(?:right|left|top|bottom)\s+of\s+(?:"([^"]+)"|(\S+))\s*$', re.IGNORECASE
)
RE_RELACAO = re.compile(
    r'^("[^"]+"|[^\s"]+)\s+(\S*(?:--|\.\.)\S*)\s+("[^"]+"|[^\s"]+)(?:\s*:\s*(.*))?$'
)
RE_SEPARADOR = re.compile(r"^[-=._\s]+$")
RE_BLOCO_PUML = re.compile(r"@startuml.*?@enduml", re.IGNORECASE | re.DOTALL)


def extrair_plantuml(texto: str) -> str:
    """
    Isola o código PlantUML de uma resposta de LLM: pega o trecho
    @startuml ... @enduml e, se não houver, remove cercas de markdown (```).
    """
    m = RE_BLOCO_PUML.search(texto)
    if m:
        return m.group(0)
    linhas = [l for l in texto.splitlines() if not l.strip().startswith("```")]
    return "\n".join(linhas).strip()


def _estereotipos(linha: str) -> Set[str]:
    """Conjunto de estereótipos normalizados de uma linha: '<<PK, FK>>' -> {'pk','fk'}."""
    saida = set()
    for bloco in RE_ESTEREOTIPO.findall(linha):
        for parte in re.split(r"[,;]", bloco):
            p = normalizar_texto(parte)
            if p:
                saida.add(p)
    return saida


def _parse_atributo(linha: str) -> Optional[Atributo]:
    """Interpreta uma linha de atributo dentro de um bloco `entity { ... }`."""
    if RE_SEPARADOR.match(linha):
        return None
    est = _estereotipos(linha)
    sem_est = RE_ESTEREOTIPO.sub("", linha).lstrip("*+#-~ \t")
    nome = sem_est.split(":")[0]
    nome = re.sub(r"</?\w+>", "", nome).strip()
    if not nome:
        return None
    return Atributo(
        nome=nome,
        eh_chave_primaria=bool(est & EST_PK),
        eh_chave_estrangeira=bool(est & EST_FK),
        eh_multivalorado=bool(est & EST_MULTI),
        eh_composto=bool(est & EST_COMPOSTO),
    )


def _resolver_entidade(mapa: Dict[str, Entidade], ref: str) -> Optional[Entidade]:
    """Busca por nome/alias exato e, se falhar, pelo nome normalizado."""
    ref = ref.strip().strip('"')
    if ref in mapa:
        return mapa[ref]
    alvo = normalizar_texto(ref)
    for chave, ent in mapa.items():
        if normalizar_texto(chave) == alvo:
            return ent
    return None


class PlantUMLParser:
    @staticmethod
    def parse(codigo_plantuml: str) -> DiagramaER:
        erd = DiagramaER()
        mapa: Dict[str, Entidade] = {}  # nome e alias -> Entidade (entity_map)
        relacoes_brutas: List[Tuple[int, str, str, str, str]] = []
        notas: List[Tuple[int, str, str, List[str]]] = []

        atual: Optional[Entidade] = None
        lendo_entidade = False
        lendo_nota = False
        nota_ref = ""
        nota_inicio = 0
        nota_compostos: List[Tuple[str, List[str]]] = []

        for n, bruta in enumerate(codigo_plantuml.splitlines(), start=1):
            linha = bruta.strip().lstrip("﻿")
            if not linha or linha.startswith("'"):
                continue

            # --- dentro de uma nota: atributos compostos ------------------------
            if lendo_nota:
                if re.match(r"^end\s*note$", linha, re.IGNORECASE):
                    for composto, subs in nota_compostos:
                        notas.append((nota_inicio, nota_ref, composto, subs))
                    lendo_nota = False
                    nota_compostos = []
                elif ":" in linha:
                    composto, resto = linha.split(":", 1)
                    subs = [re.sub(r"</?\w+>", "", s).strip() for s in re.split(r"[,;]", resto)]
                    subs = [s for s in subs if s]
                    if composto.strip():
                        nota_compostos.append((composto.strip(), subs))
                continue

            # --- dentro de uma entidade: atributos ------------------------------
            if lendo_entidade:
                if linha == "}":
                    erd.entidades.append(atual)
                    lendo_entidade = False
                    atual = None
                elif linha != "{":
                    attr = _parse_atributo(linha)
                    if attr is not None:
                        atual.atributos.append(attr)
                continue

            # --- fora de blocos --------------------------------------------------
            if re.match(r"^entity\s", linha, re.IGNORECASE):
                fraca = bool(_estereotipos(linha) & EST_FRACA)
                limpa = RE_ESTEREOTIPO.sub(" ", linha).replace("{", " ").strip()
                m = RE_ENTIDADE.match(limpa)
                if not m:
                    raise ErroPlantUML(f"linha {n}: declaração de entidade inválida: {linha!r}")
                nome = m.group(1) or m.group(2)
                alias = m.group(3)
                atual = Entidade(nome=nome, eh_fraca=fraca)
                mapa[nome] = atual
                if alias:
                    mapa[alias] = atual
                lendo_entidade = True
                continue

            m = RE_NOTA.match(linha)
            if m:
                lendo_nota = True
                nota_ref = m.group(1) or m.group(2)
                nota_inicio = n
                nota_compostos = []
                continue

            m = RE_RELACAO.match(linha)
            if m:
                relacoes_brutas.append((n, m.group(1), m.group(3), m.group(2), (m.group(4) or "").strip()))
                continue
            # demais linhas (@startuml, hide, skinparam, title...) são ignoradas

        if lendo_entidade:
            raise ErroPlantUML(f"entidade '{atual.nome}' não foi fechada com '}}'")
        if lendo_nota:
            raise ErroPlantUML(f"nota iniciada na linha {nota_inicio} não foi fechada com 'end note'")

        # Atributos compostos: localiza o atributo na entidade e anexa os subatributos
        for n, ref, composto, subs in notas:
            ent = _resolver_entidade(mapa, ref)
            if ent is None:
                raise ErroPlantUML(f"linha {n}: nota referencia entidade inexistente '{ref}'")
            alvo = normalizar_texto(composto)
            attr = next((a for a in ent.atributos if normalizar_texto(a.nome) == alvo), None)
            if attr is None:
                attr = Atributo(nome=composto)
                ent.atributos.append(attr)
            attr.eh_composto = True
            attr.subatributos.extend(subs)

        # Relacionamentos (resolvidos no fim, então a ordem no arquivo não importa)
        for n, ref1, ref2, conector, nome in relacoes_brutas:
            e1 = _resolver_entidade(mapa, ref1)
            e2 = _resolver_entidade(mapa, ref2)
            if e1 is None or e2 is None:
                faltando = ref1 if e1 is None else ref2
                raise ErroPlantUML(f"linha {n}: relacionamento usa entidade inexistente '{faltando}'")
            erd.relacionamentos.append(
                Relacionamento(
                    nome=nome,
                    entidade1=e1.nome,
                    entidade2=e2.nome,
                    cardinalidade=conector,
                    eh_fraco=".." in conector,
                )
            )
        return erd


# ==============================================================================
# 4. CARDINALIDADES (notação pé-de-galinha do PlantUML)
# ==============================================================================

_CARD = {"||": "1..1", "o|": "0..1", "|{": "1..N", "o{": "0..N"}


def parse_cardinalidade(conector: str) -> Tuple[str, str]:
    """
    Converte o conector (ex.: '||--o{') em (cardinalidade no lado da entidade1,
    cardinalidade no lado da entidade2). Símbolos desconhecidos viram '?'.
    O lado esquerdo é escrito "espelhado" ('}o' equivale a 'o{').
    """
    esq = re.match(r"[|o}{]*", conector).group(0)
    dire = re.search(r"[|o}{]*$", conector).group(0)
    esq_espelhado = esq[::-1].translate(str.maketrans("}{", "{}"))
    return _CARD.get(esq_espelhado, "?"), _CARD.get(dire, "?")


# ==============================================================================
# 5. AVALIAÇÃO (ALGORITMO 2 ESTENDIDO COM RELACIONAMENTOS)
# ==============================================================================

# Tipos de falha registrados (agrupados em 4 categorias para a análise comparativa)
CATEGORIAS_FALHA = {
    "omissoes": ("omissao_entidade", "omissao_atributo", "omissao_relacionamento"),
    "erros_tipo": ("tipo_entidade", "tipo_atributo"),
    "erros_cardinalidade": ("cardinalidade", "cardinalidade_invertida"),
    "extras": ("extra_entidade", "extra_atributo", "extra_relacionamento"),
}


@dataclass
class Falha:
    tipo: str
    elemento: str
    detalhe: str = ""


@dataclass
class DetalheAtributo:
    atributo_gabarito: str
    atributo_avaliado: Optional[str]
    similaridade: float
    acertos: Dict[str, bool]
    pontos: float


@dataclass
class DetalheEntidade:
    entidade_gabarito: str
    entidade_avaliada: Optional[str]
    similaridade: float
    tipo_correto: Optional[bool]
    pontos: float
    pontos_max: float
    atributos: List[DetalheAtributo] = field(default_factory=list)


@dataclass
class DetalheRelacionamento:
    relacionamento_gabarito: str
    relacionamento_avaliado: Optional[str]
    cardinalidade_gabarito: str
    cardinalidade_avaliada: str
    pontos: float
    pontos_max: float


@dataclass
class ResultadoAvaliacao:
    nota: float = 0.0
    entidades: List[DetalheEntidade] = field(default_factory=list)
    relacionamentos: List[DetalheRelacionamento] = field(default_factory=list)
    falhas: List[Falha] = field(default_factory=list)

    def contagem_falhas(self) -> Dict[str, int]:
        return {
            cat: sum(1 for f in self.falhas if f.tipo in tipos)
            for cat, tipos in CATEGORIAS_FALHA.items()
        }


def _sn(valor: bool) -> str:
    return "sim" if valor else "não"


def _rotulo_rel(r: Relacionamento) -> str:
    return f"{r.entidade1} – {r.entidade2}" + (f" ({r.nome})" if r.nome else "")


def avaliar_er(
    gabarito: DiagramaER,
    avaliado: DiagramaER,
    limiar: float = 0.85,
    peso_entidade: float = PESO_ENTIDADE,
    peso_relacionamento: float = PESO_RELACIONAMENTO,
) -> ResultadoAvaliacao:
    res = ResultadoAvaliacao()
    peso_total = len(gabarito.entidades) * peso_entidade + len(gabarito.relacionamentos) * peso_relacionamento
    if peso_total <= 0:
        return res
    esc = 100.0 / peso_total  # converte "peso" em pontos de nota (0-100)

    # ---------------- entidades e atributos ----------------
    entidades_usadas: Set[int] = set()
    correspondencia: Dict[str, str] = {}  # entidade do gabarito -> entidade avaliada

    for se in gabarito.entidades:
        melhor_i: Optional[int] = None
        melhor_sim = 0.0
        for i, te in enumerate(avaliado.entidades):
            if i in entidades_usadas:
                continue
            sim = similaridade_pt(se.nome, te.nome)
            if sim > melhor_sim:
                melhor_i, melhor_sim = i, sim

        max_ent = peso_entidade * esc
        det = DetalheEntidade(se.nome, None, melhor_sim, None, 0.0, max_ent)
        res.entidades.append(det)

        if melhor_i is None or melhor_sim < limiar:
            res.falhas.append(
                Falha("omissao_entidade", se.nome, f"sem correspondência (melhor similaridade {melhor_sim:.2f})")
            )
            continue

        entidades_usadas.add(melhor_i)
        te = avaliado.entidades[melhor_i]
        correspondencia[se.nome] = te.nome
        det.entidade_avaliada = te.nome
        por_componente = max_ent / (len(se.atributos) + 1)

        # componente: entidade forte/fraca
        det.tipo_correto = se.eh_fraca == te.eh_fraca
        if det.tipo_correto:
            det.pontos += por_componente
        else:
            res.falhas.append(
                Falha(
                    "tipo_entidade",
                    se.nome,
                    f"gabarito: {'fraca' if se.eh_fraca else 'forte'}; avaliado: {'fraca' if te.eh_fraca else 'forte'}",
                )
            )

        # componentes: um por atributo do gabarito
        attrs_usados: Set[int] = set()
        for a in se.atributos:
            melhor_j: Optional[int] = None
            melhor_attr_sim = 0.0
            for j, b in enumerate(te.atributos):
                if j in attrs_usados:
                    continue
                sim = similaridade_pt(a.nome, b.nome)
                if sim > melhor_attr_sim:
                    melhor_j, melhor_attr_sim = j, sim

            da = DetalheAtributo(a.nome, None, melhor_attr_sim, {}, 0.0)
            det.atributos.append(da)
            if melhor_j is None or melhor_attr_sim < limiar:
                res.falhas.append(Falha("omissao_atributo", f"{se.nome}.{a.nome}"))
                continue

            attrs_usados.add(melhor_j)
            b = te.atributos[melhor_j]
            da.atributo_avaliado = b.nome
            da.acertos = {k: ler(a) == ler(b) for k, ler in RESTRICOES_ATRIBUTO.items()}
            da.pontos = por_componente * sum(da.acertos.values()) / len(RESTRICOES_ATRIBUTO)
            det.pontos += da.pontos
            erros = [
                f"{k}: gabarito={_sn(RESTRICOES_ATRIBUTO[k](a))}, avaliado={_sn(RESTRICOES_ATRIBUTO[k](b))}"
                for k, ok in da.acertos.items()
                if not ok
            ]
            if erros:
                res.falhas.append(Falha("tipo_atributo", f"{se.nome}.{a.nome}", "; ".join(erros)))

        for j, b in enumerate(te.atributos):
            if j not in attrs_usados:
                res.falhas.append(Falha("extra_atributo", f"{te.nome}.{b.nome}"))

    for i, te in enumerate(avaliado.entidades):
        if i not in entidades_usadas:
            res.falhas.append(Falha("extra_entidade", te.nome))

    # ---------------- relacionamentos e cardinalidades ----------------
    rels_usados: Set[int] = set()
    max_rel = peso_relacionamento * esc

    for sr in gabarito.relacionamentos:
        r1, r2 = parse_cardinalidade(sr.cardinalidade)
        rotulo = _rotulo_rel(sr)
        card_gab = f"{sr.entidade1}={r1}, {sr.entidade2}={r2}"
        det = DetalheRelacionamento(rotulo, None, card_gab, "", 0.0, max_rel)
        res.relacionamentos.append(det)

        m1 = correspondencia.get(sr.entidade1)
        m2 = correspondencia.get(sr.entidade2)
        if m1 is None or m2 is None:
            res.falhas.append(
                Falha("omissao_relacionamento", rotulo, "entidade(s) do relacionamento sem correspondência")
            )
            continue

        melhor = None  # (chave de ordenação, índice, card lado m1, card lado m2, acertos)
        for k, tr in enumerate(avaliado.relacionamentos):
            if k in rels_usados:
                continue
            c1, c2 = parse_cardinalidade(tr.cardinalidade)
            if (tr.entidade1, tr.entidade2) == (m1, m2):
                a1, a2 = c1, c2
            elif (tr.entidade1, tr.entidade2) == (m2, m1):
                a1, a2 = c2, c1
            else:
                continue
            acertos = (a1 == r1) + (a2 == r2)
            sim_nome = similaridade_pt(sr.nome, tr.nome) if sr.nome and tr.nome else 0.0
            chave = (acertos, sim_nome)
            if melhor is None or chave > melhor[0]:
                melhor = (chave, k, a1, a2, acertos)

        if melhor is None:
            res.falhas.append(Falha("omissao_relacionamento", rotulo, f"não há relacionamento entre {m1} e {m2}"))
            continue

        _, k, a1, a2, acertos = melhor
        rels_usados.add(k)
        tr = avaliado.relacionamentos[k]
        det.relacionamento_avaliado = _rotulo_rel(tr)
        det.cardinalidade_avaliada = f"{sr.entidade1}={a1}, {sr.entidade2}={a2}"
        det.pontos = max_rel * (0.5 + 0.5 * acertos / 2)
        if acertos < 2:
            invertida = a1 == r2 and a2 == r1 and a1 != a2
            res.falhas.append(
                Falha(
                    "cardinalidade_invertida" if invertida else "cardinalidade",
                    rotulo,
                    f"gabarito: {card_gab}; avaliado: {det.cardinalidade_avaliada}",
                )
            )

    for k, tr in enumerate(avaliado.relacionamentos):
        if k not in rels_usados:
            res.falhas.append(Falha("extra_relacionamento", _rotulo_rel(tr)))

    res.nota = sum(d.pontos for d in res.entidades) + sum(d.pontos for d in res.relacionamentos)
    return res


# ==============================================================================
# 6. VERIFICAÇÃO DE COMPILAÇÃO NO PLANTUML
# ==============================================================================


def localizar_plantuml(comando_usuario: Optional[str] = None) -> Optional[List[str]]:
    """Retorna o comando para chamar o PlantUML, ou None se não for encontrado."""
    if comando_usuario:
        return shlex.split(comando_usuario)
    env = os.environ.get("PLANTUML_CMD")
    if env:
        return shlex.split(env)
    exe = shutil.which("plantuml")
    if exe:
        return [exe]
    jar = os.environ.get("PLANTUML_JAR") or ("plantuml.jar" if os.path.exists("plantuml.jar") else None)
    if jar and shutil.which("java"):
        return ["java", "-jar", jar]
    return None


def compilar_plantuml(codigo: str, comando: List[str], timeout: int = 120) -> Tuple[bool, str]:
    """
    Verifica a sintaxe com `plantuml -checkonly` (não gera imagem).
    Código de saída 0 = compilou; 200 = erro de sintaxe; 100 = nenhum diagrama encontrado.
    """
    with tempfile.TemporaryDirectory() as pasta:
        arquivo = os.path.join(pasta, "diagrama.puml")
        with open(arquivo, "w", encoding="utf-8") as f:
            f.write(codigo)
        try:
            proc = subprocess.run(
                comando + ["-checkonly", arquivo], capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"falha ao executar o PlantUML: {e}"
    saida = "\n".join(
        l for l in (proc.stdout + "\n" + proc.stderr).splitlines() if l.strip() and not l.startswith("Picked up")
    )
    return proc.returncode == 0, saida.strip()


# ==============================================================================
# 7. EXECUÇÃO: UM DIAGRAMA OU LOTE (LLM x NÍVEL DE DETALHAMENTO)
# ==============================================================================


@dataclass
class Config:
    limiar: float = 0.85
    comando_compilador: Optional[List[str]] = None
    verificar_compilacao: bool = True
    pontuar_sem_compilar: bool = False
    peso_entidade: float = PESO_ENTIDADE
    peso_relacionamento: float = PESO_RELACIONAMENTO


@dataclass
class Execucao:
    arquivo: str = ""
    llm: str = "-"
    nivel: str = "-"
    compilou: Optional[bool] = None  # None = não verificado
    mensagem_compilacao: str = ""
    interpretavel: Optional[bool] = None  # None = não tentado
    erro: str = ""
    resultado: Optional[ResultadoAvaliacao] = None


def ler_arquivo_puml(caminho: str) -> str:
    with open(caminho, encoding="utf-8-sig") as f:
        return f.read()


def avaliar_diagrama(
    codigo_bruto: str, gabarito: DiagramaER, cfg: Config, arquivo: str = "", llm: str = "-", nivel: str = "-"
) -> Execucao:
    ex = Execucao(arquivo=arquivo, llm=llm, nivel=nivel)
    codigo = extrair_plantuml(codigo_bruto)

    if cfg.verificar_compilacao and cfg.comando_compilador:
        ex.compilou, ex.mensagem_compilacao = compilar_plantuml(codigo, cfg.comando_compilador)
    if ex.compilou is False and not cfg.pontuar_sem_compilar:
        return ex

    try:
        diagrama = PlantUMLParser.parse(codigo)
    except ErroPlantUML as e:
        ex.interpretavel = False
        ex.erro = str(e)
        return ex
    ex.interpretavel = True
    ex.resultado = avaliar_er(gabarito, diagrama, cfg.limiar, cfg.peso_entidade, cfg.peso_relacionamento)
    return ex


EXTENSOES_LOTE = {".puml", ".plantuml", ".pu", ".txt", ".md"}
PREFIXO_RESULTADO = "Resultado_do_"


def listar_execucoes(pasta: str) -> List[Tuple[str, str, str]]:
    """Lista (caminho, llm, nivel) de pasta/<llm>/<nivel>/arquivo."""
    base = Path(pasta)
    itens = []
    for p in sorted(base.rglob("*")):
        # os arquivos Resultado_do_*.txt gerados pelo script não são entrada
        if p.is_file() and p.suffix.lower() in EXTENSOES_LOTE and not p.name.startswith(PREFIXO_RESULTADO):
            partes = p.relative_to(base).parts
            llm = partes[0] if len(partes) >= 2 else "-"
            nivel = partes[1] if len(partes) >= 3 else "-"
            itens.append((str(p), llm, nivel))
    return itens


def avaliar_lote(gabarito: DiagramaER, pasta: str, cfg: Config) -> List[Execucao]:
    return [
        avaliar_diagrama(ler_arquivo_puml(c), gabarito, cfg, arquivo=c, llm=llm, nivel=nivel)
        for c, llm, nivel in listar_execucoes(pasta)
    ]


def avaliar_arquivos(
    caminho_gabarito: str, caminho_avaliado: str, cfg: Optional[Config] = None
) -> Execucao:
    """Atalho para uso como biblioteca."""
    cfg = cfg or Config(verificar_compilacao=False)
    gab = PlantUMLParser.parse(extrair_plantuml(ler_arquivo_puml(caminho_gabarito)))
    return avaliar_diagrama(ler_arquivo_puml(caminho_avaliado), gab, cfg, arquivo=caminho_avaliado)


# ==============================================================================
# 8. RELATÓRIOS
# ==============================================================================

ROTULO_FALHA = {
    "omissao_entidade": "Entidade omitida",
    "omissao_atributo": "Atributo omitido",
    "omissao_relacionamento": "Relacionamento omitido",
    "tipo_entidade": "Tipo de entidade incorreto",
    "tipo_atributo": "Tipo de atributo incorreto",
    "cardinalidade": "Cardinalidade incorreta",
    "cardinalidade_invertida": "Cardinalidade invertida",
    "extra_entidade": "Entidade extra (possível alucinação)",
    "extra_atributo": "Atributo extra (possível alucinação)",
    "extra_relacionamento": "Relacionamento extra (possível alucinação)",
}


def formatar_execucao(ex: Execucao) -> str:
    comp = {True: "sim", False: "NÃO", None: "não verificado"}[ex.compilou]
    linhas = [f"Compila no PlantUML: {comp}"]
    if ex.compilou is False and ex.mensagem_compilacao:
        linhas.append(f"  {ex.mensagem_compilacao}")
    if ex.erro:
        linhas.append(f"Não foi possível interpretar o diagrama: {ex.erro}")
    if ex.resultado is None:
        linhas.append("NOTA: não calculada")
        return "\n".join(linhas)

    res = ex.resultado
    linhas += [f"NOTA FINAL: {res.nota:.2f}%", ""]
    linhas.append("Entidades e atributos")
    for d in res.entidades:
        if d.entidade_avaliada is None:
            linhas.append(f"  [x] {d.entidade_gabarito}: NÃO ENCONTRADA (0.00 / {d.pontos_max:.2f})")
            continue
        linhas.append(
            f"  [+] {d.entidade_gabarito} <- {d.entidade_avaliada} "
            f"(sim. {d.similaridade:.2f}) {d.pontos:.2f} / {d.pontos_max:.2f}"
        )
        linhas.append(f"        tipo forte/fraca: {'ok' if d.tipo_correto else 'ERRO'}")
        for a in d.atributos:
            if a.atributo_avaliado is None:
                linhas.append(f"        - {a.atributo_gabarito}: NÃO ENCONTRADO")
            else:
                r = ", ".join(f"{k}={'ok' if v else 'ERRO'}" for k, v in a.acertos.items())
                linhas.append(
                    f"        - {a.atributo_gabarito} <- {a.atributo_avaliado} "
                    f"(sim. {a.similaridade:.2f}) [{r}] +{a.pontos:.2f}"
                )
    linhas.append("")
    linhas.append("Relacionamentos e cardinalidades")
    for d in res.relacionamentos:
        if d.relacionamento_avaliado is None:
            linhas.append(f"  [x] {d.relacionamento_gabarito}: NÃO ENCONTRADO (0.00 / {d.pontos_max:.2f})")
        else:
            linhas.append(
                f"  [+] {d.relacionamento_gabarito} <- {d.relacionamento_avaliado} "
                f"{d.pontos:.2f} / {d.pontos_max:.2f}"
            )
            linhas.append(f"        gabarito: {d.cardinalidade_gabarito} | avaliado: {d.cardinalidade_avaliada}")
    if res.falhas:
        linhas += ["", "Falhas identificadas"]
        for f in res.falhas:
            extra = f" — {f.detalhe}" if f.detalhe else ""
            linhas.append(f"  * {ROTULO_FALHA[f.tipo]}: {f.elemento}{extra}")
        c = res.contagem_falhas()
        linhas.append(
            f"  Resumo: {c['omissoes']} omissão(ões), {c['erros_tipo']} erro(s) de tipo, "
            f"{c['erros_cardinalidade']} erro(s) de cardinalidade, {c['extras']} elemento(s) extra(s)"
        )
    return "\n".join(linhas)


def caminho_resultado(arquivo_avaliado: str) -> Path:
    """Resultado_do_<nome do arquivo avaliado, sem extensão>.txt, na mesma pasta do arquivo."""
    p = Path(arquivo_avaliado)
    return p.with_name(f"{PREFIXO_RESULTADO}{p.stem}.txt")


def salvar_resultado(ex: Execucao, caminho_gabarito: str, limiar: float) -> Path:
    """Grava o relatório da avaliação em Resultado_do_<arquivo>.txt e devolve o caminho."""
    destino = caminho_resultado(ex.arquivo)
    cabecalho = [
        f"Arquivo avaliado: {ex.arquivo}",
        f"Gabarito: {caminho_gabarito}",
        f"Limiar de similaridade: {limiar}",
        "-" * 60,
        "",
    ]
    destino.write_text("\n".join(cabecalho) + formatar_execucao(ex) + "\n", encoding="utf-8-sig")
    return destino


def _salvar(ex: Execucao, caminho_gabarito: str, limiar: float) -> Optional[Path]:
    try:
        return salvar_resultado(ex, caminho_gabarito, limiar)
    except OSError as e:
        print(f"Aviso: não foi possível salvar o resultado de {ex.arquivo}: {e}", file=sys.stderr)
        return None


def _linha_execucao(ex: Execucao) -> Dict[str, object]:
    c = ex.resultado.contagem_falhas() if ex.resultado else {k: "" for k in CATEGORIAS_FALHA}
    return {
        "arquivo": ex.arquivo,
        "llm": ex.llm,
        "nivel": ex.nivel,
        "compilou": {True: "sim", False: "nao", None: "nao_verificado"}[ex.compilou],
        "interpretavel": {True: "sim", False: "nao", None: ""}[ex.interpretavel],
        "nota": f"{ex.resultado.nota:.2f}" if ex.resultado else "",
        **c,
        "erro": ex.erro or ex.mensagem_compilacao.replace("\n", " | "),
    }


def resumir(execucoes: List[Execucao]) -> List[Dict[str, object]]:
    """Tabela comparativa: uma linha por (LLM, nível de detalhamento)."""
    grupos: Dict[Tuple[str, str], List[Execucao]] = {}
    for ex in execucoes:
        grupos.setdefault((ex.llm, ex.nivel), []).append(ex)

    linhas = []
    for (llm, nivel), exs in sorted(grupos.items()):
        verificadas = [e for e in exs if e.compilou is not None]
        taxa = 100.0 * sum(1 for e in verificadas if e.compilou) / len(verificadas) if verificadas else None
        avaliadas = [e for e in exs if e.resultado is not None]
        notas = [e.resultado.nota for e in avaliadas]
        linha: Dict[str, object] = {
            "llm": llm,
            "nivel": nivel,
            "execucoes": len(exs),
            "taxa_compilacao": taxa,
            "avaliadas": len(avaliadas),
            "nota_media": statistics.mean(notas) if notas else None,
            "desvio_padrao": (statistics.stdev(notas) if len(notas) > 1 else 0.0) if notas else None,
            "nota_min": min(notas) if notas else None,
            "nota_max": max(notas) if notas else None,
        }
        for cat in CATEGORIAS_FALHA:
            vals = [e.resultado.contagem_falhas()[cat] for e in avaliadas]
            linha[f"{cat}_por_diagrama"] = statistics.mean(vals) if vals else None
        linhas.append(linha)
    return linhas


def _fmt(v: object, casas: int = 1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{casas}f}"
    return str(v)


def formatar_resumo(linhas: List[Dict[str, object]]) -> str:
    cab = ["LLM", "Nível", "Exec.", "Compila %", "Aval.", "Nota média", "DP", "Mín", "Máx",
           "Omis.", "Tipo", "Card.", "Extras"]
    corpo = [
        [
            str(l["llm"]), str(l["nivel"]), _fmt(l["execucoes"]), _fmt(l["taxa_compilacao"]), _fmt(l["avaliadas"]),
            _fmt(l["nota_media"]), _fmt(l["desvio_padrao"]), _fmt(l["nota_min"]), _fmt(l["nota_max"]),
            _fmt(l["omissoes_por_diagrama"]), _fmt(l["erros_tipo_por_diagrama"]),
            _fmt(l["erros_cardinalidade_por_diagrama"]), _fmt(l["extras_por_diagrama"]),
        ]
        for l in linhas
    ]
    larg = [max(len(r[i]) for r in [cab] + corpo) for i in range(len(cab))]
    fmt = lambda r: "  ".join(c.ljust(larg[i]) for i, c in enumerate(r))
    return "\n".join(
        [fmt(cab), fmt(["-" * w for w in larg])] + [fmt(r) for r in corpo]
        + ["", "Omis./Tipo/Card./Extras = média de falhas por diagrama avaliado."]
    )


def _gravar_csv(caminho: str, linhas: List[Dict[str, object]]) -> None:
    if not linhas:
        return
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader()
        w.writerows(linhas)


# ==============================================================================
# 9. LINHA DE COMANDO
# ==============================================================================


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Avalia diagramas ER (PlantUML) gerados por LLMs contra um gabarito (PlantUML)."
    )
    ap.add_argument("gabarito", help="arquivo .puml com o modelo de referência")
    ap.add_argument("avaliado", nargs="?", help="arquivo .puml a ser avaliado (modo de um diagrama)")
    ap.add_argument("--lote", metavar="PASTA", help="avalia pasta/<llm>/<nivel>/*.puml")
    ap.add_argument("--saida", metavar="PREFIXO", help="grava PREFIXO_detalhado.csv e PREFIXO_resumo.csv (lote)")
    ap.add_argument("--limiar", type=float, default=0.85, help="similaridade mínima entre nomes (padrão: 0.85)")
    ap.add_argument("--plantuml", metavar="CMD", help='comando do PlantUML, ex.: "java -jar plantuml.jar"')
    ap.add_argument("--sem-compilar", action="store_true", help="não verifica a compilação no PlantUML")
    ap.add_argument("--pontuar-sem-compilar", action="store_true", help="pontua também diagramas que não compilam")
    ap.add_argument("--json", action="store_true", help="saída em JSON (modo de um diagrama)")
    args = ap.parse_args(argv)

    if bool(args.avaliado) == bool(args.lote):
        ap.error("informe o arquivo a avaliar OU --lote PASTA (apenas um dos dois)")

    cfg = Config(
        limiar=args.limiar,
        verificar_compilacao=not args.sem_compilar,
        pontuar_sem_compilar=args.pontuar_sem_compilar,
    )
    if cfg.verificar_compilacao:
        cfg.comando_compilador = localizar_plantuml(args.plantuml)
        if cfg.comando_compilador is None:
            print(
                "Aviso: PlantUML não encontrado; a compilação NÃO será verificada. "
                "Use --plantuml \"java -jar plantuml.jar\" ou defina PLANTUML_JAR.",
                file=sys.stderr,
            )

    try:
        gab = PlantUMLParser.parse(extrair_plantuml(ler_arquivo_puml(args.gabarito)))
    except (OSError, ErroPlantUML) as e:
        print(f"Erro no gabarito: {e}", file=sys.stderr)
        return 2

    if args.lote:
        if not os.path.isdir(args.lote):
            print(f"Pasta não encontrada: {args.lote}", file=sys.stderr)
            return 2
        execucoes = avaliar_lote(gab, args.lote, cfg)
        if not execucoes:
            print("Nenhum arquivo encontrado na pasta.", file=sys.stderr)
            return 2
        for ex in execucoes:
            _salvar(ex, args.gabarito, cfg.limiar)
        resumo = resumir(execucoes)
        print(formatar_resumo(resumo))
        print(f"\nO resultado de cada diagrama foi salvo ao lado dele, em {PREFIXO_RESULTADO}<nome do arquivo>.txt")
        if args.saida:
            _gravar_csv(f"{args.saida}_detalhado.csv", [_linha_execucao(e) for e in execucoes])
            _gravar_csv(f"{args.saida}_resumo.csv", [{k: ("" if v is None else v) for k, v in l.items()} for l in resumo])
            print(f"\nGravado: {args.saida}_detalhado.csv e {args.saida}_resumo.csv")
        return 0

    try:
        ex = avaliar_diagrama(ler_arquivo_puml(args.avaliado), gab, cfg, arquivo=args.avaliado)
    except OSError as e:
        print(f"Erro ao ler o arquivo: {e}", file=sys.stderr)
        return 2
    destino = _salvar(ex, args.gabarito, cfg.limiar)
    if args.json:
        print(json.dumps(asdict(ex), ensure_ascii=False, indent=2))
    else:
        print(formatar_execucao(ex))
    if destino:
        print(f"\nResultado salvo em: {destino}", file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
