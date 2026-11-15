from django.test import SimpleTestCase

from notifications.personalize import EXAMPLE_NAME, fill_name, first_name


class FirstNameTests(SimpleTestCase):
    def test_pega_so_o_primeiro_nome(self):
        self.assertEqual(first_name("Ana Souza Lima"), "Ana")

    def test_ignora_espacos_sobrando(self):
        self.assertEqual(first_name("  Bia  "), "Bia")

    def test_sem_nome_volta_vazio(self):
        self.assertEqual(first_name(None), "")
        self.assertEqual(first_name(""), "")
        self.assertEqual(first_name("   "), "")


class FillNameTests(SimpleTestCase):
    def test_troca_marcador_pelo_primeiro_nome(self):
        self.assertEqual(fill_name("Olá, {nome}!", "Ana Souza"), "Olá, Ana!")

    def test_troca_todas_as_ocorrencias(self):
        self.assertEqual(
            fill_name("{nome}, oi. Tchau, {nome}.", "Ana"), "Ana, oi. Tchau, Ana."
        )

    def test_aceita_maiusculas_e_espacos_no_marcador(self):
        self.assertEqual(fill_name("Olá, {Nome}! { NOME }", "Ana"), "Olá, Ana! Ana")

    def test_texto_sem_marcador_fica_igual(self):
        self.assertEqual(fill_name("Novidades na loja", "Ana"), "Novidades na loja")
        self.assertEqual(fill_name("Novidades na loja", None), "Novidades na loja")

    def test_nome_com_caracteres_especiais_entra_literal(self):
        self.assertEqual(fill_name("Olá, {nome}!", r"J\1o"), r"Olá, J\1o!")

    def test_sem_nome_some_com_a_virgula(self):
        self.assertEqual(fill_name("Olá, {nome}!", None), "Olá!")
        self.assertEqual(fill_name("Olá, {nome}!", ""), "Olá!")

    def test_sem_nome_some_com_o_espaco(self):
        self.assertEqual(fill_name("Olá {nome}!", None), "Olá!")
        self.assertEqual(fill_name("Oi {nome}, tudo bem?", None), "Oi, tudo bem?")

    def test_sem_nome_no_comeco_da_linha_leva_a_pontuacao(self):
        self.assertEqual(
            fill_name("{nome}, temos novidades.\nLinha 2", None),
            "Temos novidades.\nLinha 2",
        )

    def test_sem_nome_no_comeco_da_linha_deixa_a_frase_com_maiuscula(self):
        self.assertEqual(fill_name("{nome}, veja!", ""), "Veja!")
        self.assertEqual(
            fill_name("{nome}! Chegou drop novo", None), "Chegou drop novo"
        )
        self.assertEqual(fill_name("  {nome}, ébano chegou", None), "Ébano chegou")

    def test_sem_nome_sozinho_na_linha_sobe_a_pontuacao_para_a_saudacao(self):
        self.assertEqual(fill_name("Olá,\n{nome}!", ""), "Olá!")
        self.assertEqual(
            fill_name("Olá,\n{nome}!\nTemos novidades.", None), "Olá!\nTemos novidades."
        )
        self.assertEqual(fill_name("Oi,\n{nome}.", None), "Oi.")
        self.assertEqual(
            fill_name("Olá,\n{nome},\nTemos novidades.", None), "Olá,\nTemos novidades."
        )

    def test_sem_nome_sozinho_na_linha_some_a_linha(self):
        self.assertEqual(fill_name("Topo\n{nome}!\nFim", None), "Topo\nFim")
        self.assertEqual(fill_name("{nome}!\nFim", None), "Fim")
        self.assertEqual(fill_name("Topo\n\n{nome}\nFim", None), "Topo\n\nFim")

    def test_sem_nome_preserva_quebras_de_linha_do_resto(self):
        self.assertEqual(fill_name("Olá, {nome}!\n\nTexto", None), "Olá!\n\nTexto")

    def test_nome_de_exemplo_da_previa(self):
        self.assertEqual(EXAMPLE_NAME, "Maria")
