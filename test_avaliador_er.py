# -*- coding: utf-8 -*-
"""
Testes de validação do avaliador. Todas as notas esperadas foram calculadas à mão
a partir das regras de pontuação descritas no cabeçalho de avaliador_er.py.

Executar:  python -m unittest -v test_avaliador_er
(para incluir o teste com o PlantUML real:  PLANTUML_JAR=/caminho/plantuml.jar ...)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import avaliador_er as ae

AQUI = Path(__file__).resolve().parent

# Compiladores falsos: deixam os testes determinísticos, sem depender do Java
COMPILA_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
COMPILA_ERRO = [sys.executable, "-c", "import sys; sys.exit(200)"]

REF = """@startuml
entity A {
}
entity B {
}
A ||--o{ B : tem
@enduml"""


def D(texto):
    return ae.PlantUMLParser.parse(texto)


class TestNormalizacao(unittest.TestCase):
    def test_acentos_e_underline(self):
        self.assertEqual(ae.normalizar_texto("Endereço_Cliente"), "endereco cliente")

    def test_similaridade(self):
        self.assertEqual(ae.similaridade_pt("Cliente", "cliente"), 1.0)
        self.assertGreaterEqual(ae.similaridade_pt("Cliente", "Clientes"), 0.85)
        self.assertEqual(ae.similaridade_pt("id_cliente", "cliente_id"), 1.0)  # ordem trocada
        self.assertLess(ae.similaridade_pt("Pedido", "Produto"), 0.85)


class TestCardinalidade(unittest.TestCase):
    def test_conectores(self):
        self.assertEqual(ae.parse_cardinalidade("||--o{"), ("1..1", "0..N"))
        self.assertEqual(ae.parse_cardinalidade("}o..o{"), ("0..N", "0..N"))
        self.assertEqual(ae.parse_cardinalidade("|o--||"), ("0..1", "1..1"))
        self.assertEqual(ae.parse_cardinalidade("}|--|{"), ("1..N", "1..N"))
        self.assertEqual(ae.parse_cardinalidade("||..|{"), ("1..1", "1..N"))
        self.assertEqual(ae.parse_cardinalidade("-->"), ("?", "?"))


class TestParser(unittest.TestCase):
    def test_alias_fraca_composto_e_relacionamento_fraco(self):
        d = ae.PlantUMLParser.parse((AQUI / "gabarito_exemplo.puml").read_text(encoding="utf-8"))
        self.assertEqual([e.nome for e in d.entidades], ["Cliente", "Pedido", "Item do Pedido"])
        self.assertTrue(d.entidades[2].eh_fraca)
        cliente = {a.nome: a for a in d.entidades[0].atributos}
        self.assertTrue(cliente["id_cliente"].eh_chave_primaria)
        self.assertTrue(cliente["telefone"].eh_multivalorado)
        self.assertTrue(cliente["endereco"].eh_composto)
        self.assertEqual(cliente["endereco"].subatributos, ["rua", "numero", "cidade"])
        self.assertEqual(len(d.relacionamentos), 2)
        self.assertEqual(d.relacionamentos[1].entidade2, "Item do Pedido")  # alias resolvido
        self.assertTrue(d.relacionamentos[1].eh_fraco)
        self.assertFalse(d.relacionamentos[0].eh_fraco)

    def test_extrair_de_resposta_de_llm(self):
        resposta = "Claro! Segue:\n```plantuml\n@startuml\nentity A {\n}\n@enduml\n```\nEspero ter ajudado."
        self.assertEqual(ae.extrair_plantuml(resposta), "@startuml\nentity A {\n}\n@enduml")

    def test_erros(self):
        with self.assertRaises(ae.ErroPlantUML):
            D("@startuml\nentity A {\n * id\n@enduml")  # entidade sem fechar
        with self.assertRaises(ae.ErroPlantUML):
            D("@startuml\nentity A {\n}\nA ||--o{ Z : x\n@enduml")  # Z não existe


class TestAvaliacao(unittest.TestCase):
    def test_gabarito_contra_ele_mesmo_da_100(self):
        gab = D((AQUI / "gabarito_exemplo.puml").read_text(encoding="utf-8"))
        res = ae.avaliar_er(gab, gab)
        self.assertAlmostEqual(res.nota, 100.0)
        self.assertEqual(res.falhas, [])

    def test_exemplo_completo_82(self):
        # 5 unidades (3 entidades + 2 relacionamentos) valendo 20 pontos cada:
        #   Cliente 18,67 (composto errado em endereco: 2/3 de 4 pontos)
        #   Pedido 20 | Item do Pedido 13,33 (tipo forte/fraca errado)
        #   rel. Cliente-Pedido 20 | rel. Pedido-Item 10 (cardinalidade invertida)
        ex = ae.avaliar_arquivos(
            str(AQUI / "gabarito_exemplo.puml"), str(AQUI / "aluno_exemplo.puml"), ae.Config(verificar_compilacao=False)
        )
        self.assertAlmostEqual(ex.resultado.nota, 82.0, places=2)
        self.assertEqual(
            ex.resultado.contagem_falhas(),
            {"omissoes": 0, "erros_tipo": 2, "erros_cardinalidade": 1, "extras": 3},
        )
        tipos = sorted(f.tipo for f in ex.resultado.falhas)
        self.assertEqual(
            tipos,
            sorted(
                ["tipo_entidade", "tipo_atributo", "cardinalidade_invertida",
                 "extra_atributo", "extra_entidade", "extra_relacionamento"]
            ),
        )

    def test_entidade_ausente_perde_entidade_e_relacionamento(self):
        # 3 unidades de 33,33: sobra só a entidade A
        res = ae.avaliar_er(D(REF), D("@startuml\nentity A {\n}\n@enduml"))
        self.assertAlmostEqual(res.nota, 100 / 3, places=2)
        self.assertEqual(res.contagem_falhas()["omissoes"], 2)  # entidade B + relacionamento

    def test_cardinalidade_errada_nao_invertida(self):
        aluno = "@startuml\nentity A {\n}\nentity B {\n}\nA }o--o{ B\n@enduml"
        res = ae.avaliar_er(D(REF), D(aluno))
        # 33,33 + 33,33 + 33,33 * (0,5 + 0,5 * 1/2) = 91,67
        self.assertAlmostEqual(res.nota, 91.6667, places=3)
        self.assertEqual([f.tipo for f in res.falhas], ["cardinalidade"])

    def test_relacionamento_escrito_no_sentido_oposto_vale_igual(self):
        aluno = "@startuml\nentity A {\n}\nentity B {\n}\nB }o--|| A : tem\n@enduml"
        self.assertAlmostEqual(ae.avaliar_er(D(REF), D(aluno)).nota, 100.0)

    def test_atributo_simples_marcado_como_identificador_perde_ponto(self):
        gab = D("@startuml\nentity A {\n  nome : varchar\n}\n@enduml")
        alu = D("@startuml\nentity A {\n  * nome : varchar <<PK>>\n}\n@enduml")
        # 1 entidade, 2 componentes de 50: tipo ok (50) + atributo com 2 de 3 acertos (33,33)
        self.assertAlmostEqual(ae.avaliar_er(gab, alu).nota, 83.3333, places=3)

    def test_fk_nao_pontua_mas_conta_como_atributo_extra(self):
        gab = D("@startuml\nentity A {\n  * id : int <<PK>>\n}\n@enduml")
        alu = D("@startuml\nentity A {\n  * id : int <<PK>>\n  id_b : int <<FK>>\n}\n@enduml")
        res = ae.avaliar_er(gab, alu)
        self.assertAlmostEqual(res.nota, 100.0)
        self.assertEqual([f.tipo for f in res.falhas], ["extra_atributo"])

    def test_atributo_nao_pontua_duas_vezes(self):
        gab = D("@startuml\nentity A {\n  nome : x\n  nome_a : x\n}\n@enduml")
        alu = D("@startuml\nentity A {\n  nome : x\n}\n@enduml")
        res = ae.avaliar_er(gab, alu, limiar=0.7)
        self.assertEqual(sum(1 for f in res.falhas if f.tipo == "omissao_atributo"), 1)


class TestExecucao(unittest.TestCase):
    def test_nao_compila_nao_pontua_por_padrao(self):
        cfg = ae.Config(comando_compilador=COMPILA_ERRO)
        ex = ae.avaliar_diagrama(REF, D(REF), cfg)
        self.assertIs(ex.compilou, False)
        self.assertIsNone(ex.resultado)

    def test_pontuar_sem_compilar(self):
        cfg = ae.Config(comando_compilador=COMPILA_ERRO, pontuar_sem_compilar=True)
        ex = ae.avaliar_diagrama(REF, D(REF), cfg)
        self.assertIs(ex.compilou, False)
        self.assertAlmostEqual(ex.resultado.nota, 100.0)

    def test_lote_e_resumo(self):
        with tempfile.TemporaryDirectory() as pasta:
            base = Path(pasta)
            (base / "GPT" / "detalhado").mkdir(parents=True)
            (base / "GPT" / "detalhado" / "r1.puml").write_text(REF, encoding="utf-8")
            (base / "GPT" / "detalhado" / "r2.puml").write_text(
                "@startuml\nentity A {\n}\nentity B {\n}\nA }o--o{ B\n@enduml", encoding="utf-8"
            )
            (base / "GPT" / "detalhado" / "r3.puml").write_text("@startuml\nentity A {\n * id\n@enduml", encoding="utf-8")
            cfg = ae.Config(comando_compilador=COMPILA_OK)
            execs = ae.avaliar_lote(D(REF), pasta, cfg)
            self.assertEqual(len(execs), 3)
            linha = ae.resumir(execs)[0]
            self.assertEqual((linha["llm"], linha["nivel"]), ("GPT", "detalhado"))
            self.assertEqual(linha["execucoes"], 3)
            self.assertEqual(linha["avaliadas"], 2)  # r3 não é interpretável
            self.assertAlmostEqual(linha["taxa_compilacao"], 100.0)
            self.assertAlmostEqual(linha["nota_media"], (100.0 + 91.6667) / 2, places=2)
            self.assertAlmostEqual(linha["erros_cardinalidade_por_diagrama"], 0.5)


class TestResultadoTxt(unittest.TestCase):
    def test_nome_do_arquivo_de_resultado(self):
        self.assertEqual(
            ae.caminho_resultado(str(Path("pasta") / "aluno_exemplo.puml")),
            Path("pasta") / "Resultado_do_aluno_exemplo.txt",
        )

    def test_salva_resultado_ao_lado_do_arquivo(self):
        with tempfile.TemporaryDirectory() as pasta:
            alvo = Path(pasta) / "resposta_gpt.puml"
            alvo.write_text(REF, encoding="utf-8")
            ex = ae.avaliar_diagrama(REF, D(REF), ae.Config(verificar_compilacao=False), arquivo=str(alvo))
            destino = ae.salvar_resultado(ex, "gabarito.puml", 0.85)
            self.assertEqual(destino, Path(pasta) / "Resultado_do_resposta_gpt.txt")
            texto = destino.read_text(encoding="utf-8-sig")
            self.assertIn("NOTA FINAL: 100.00%", texto)
            self.assertIn("Arquivo avaliado:", texto)

    def test_lote_nao_le_os_resultados_como_entrada(self):
        with tempfile.TemporaryDirectory() as pasta:
            base = Path(pasta) / "GPT" / "detalhado"
            base.mkdir(parents=True)
            (base / "exec1.puml").write_text(REF, encoding="utf-8")
            cfg = ae.Config(verificar_compilacao=False)
            for ex in ae.avaliar_lote(D(REF), pasta, cfg):
                ae.salvar_resultado(ex, "gabarito.puml", cfg.limiar)
            self.assertTrue((base / "Resultado_do_exec1.txt").exists())
            self.assertEqual(len(ae.avaliar_lote(D(REF), pasta, cfg)), 1)  # segunda rodada: ainda 1 entrada


@unittest.skipUnless(ae.localizar_plantuml(), "PlantUML não encontrado (defina PLANTUML_JAR)")
class TestPlantUMLReal(unittest.TestCase):
    def test_compilacao_real(self):
        cmd = ae.localizar_plantuml()
        ok, _ = ae.compilar_plantuml((AQUI / "gabarito_exemplo.puml").read_text(encoding="utf-8"), cmd)
        self.assertTrue(ok)
        ok, msg = ae.compilar_plantuml("@startuml\nentity A {\n  * id\n\nA ||--o{\n@enduml", cmd)
        self.assertFalse(ok)
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
