"""Parser dos textos fixos e leitura do traceparent.

    cd agente
    uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import unittest

from agente import pedido, trace

PEDIDO = "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"


class TestReserva(unittest.TestCase):
    def test_formato_do_enunciado(self) -> None:
        self.assertEqual(
            pedido.reserva(PEDIDO),
            {
                "sala": "sala-garagem",
                "inicio": "2026-11-03T14:00:00-03:00",
                "fim": "2026-11-03T15:00:00-03:00",
                "responsavel": "Marty",
            },
        )

    def test_responsavel_com_espacos_e_espacos_nas_pontas(self) -> None:
        texto = f"  {PEDIDO.replace('Marty', 'Doc  Brown')}  \n"
        self.assertEqual(pedido.reserva(texto)["responsavel"], "Doc  Brown")

    def test_nao_interpreta_o_resto_do_pedido(self) -> None:
        # Sala e datas passam como vieram: quem valida e o servidor MCP.
        self.assertEqual(pedido.reserva(PEDIDO.replace("sala-garagem", "sala-delorean"))["sala"], "sala-delorean")
        self.assertEqual(pedido.reserva(PEDIDO.replace("14:00:00", "amanha"))["inicio"], "2026-11-03Tamanha-03:00")

    def test_fora_do_formato(self) -> None:
        for texto in (
            "",
            "quero a garagem amanha",
            PEDIDO.replace("reservar ", "Reservar "),
            PEDIDO.replace(" responsavel=Marty", ""),
            PEDIDO.replace("responsavel=Marty", "responsavel="),
            "reservar inicio=a sala=b fim=c responsavel=d",
            "escolha=sala-fusca",
        ):
            with self.subTest(texto=texto):
                self.assertIsNone(pedido.reserva(texto))


class TestEscolha(unittest.TestCase):
    def test_escolhas_validas(self) -> None:
        self.assertEqual(pedido.escolha("escolha=sala-fusca"), "sala-fusca")
        self.assertEqual(pedido.escolha(" escolha=recusar \n"), pedido.RECUSAR)

    def test_fora_do_formato(self) -> None:
        for texto in ("escolha=", "escolha=a b", "Escolha=sala-fusca", "sala-fusca", PEDIDO):
            with self.subTest(texto=texto):
                self.assertIsNone(pedido.escolha(texto))


class TestTraceparent(unittest.TestCase):
    def test_mantem_trace_id_e_troca_span_id(self) -> None:
        t = trace.do_header("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        self.assertEqual((t.trace_id, t.flags), ("4bf92f3577b34da6a3ce929d0e0e4736", "01"))
        primeiro, segundo = t.novo_traceparent(), t.novo_traceparent()
        self.assertTrue(primeiro.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertNotEqual(primeiro, segundo)
        self.assertNotIn("00f067aa0ba902b7", primeiro)

    def test_header_invalido_ou_ausente(self) -> None:
        for valor in (None, "", "lixo", "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                      "00-00000000000000000000000000000000-00f067aa0ba902b7-01"):
            with self.subTest(valor=valor):
                self.assertIsNone(trace.do_header(valor))


if __name__ == "__main__":
    unittest.main()
